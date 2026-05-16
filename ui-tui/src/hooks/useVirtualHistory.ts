import type { ScrollBoxHandle } from '@hermes/ink'
import {
  type RefObject,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore
} from 'react'

const ESTIMATE = 4
// Overscan was 40 (= viewport) which is way more than needed when heights
// are well-estimated.  Cutting in half saves ~20 mounted items per scroll
// edge → smaller fiber tree → less buffer-compose work per frame.  HN/CC
// dev (https://news.ycombinator.com/item?id=46699072) confirmed GC pressure
// from large JSX trees was their main perf issue post-rewrite.
const OVERSCAN = 20
// Hard cap on mounted items.  Was 260; profiling showed ~23k live Yoga
// nodes during sustained PageUp catch-up (renderer p99=106ms).  The
// viewport+2*overscan = 80 rows of needed coverage = ~25 items at avg 3
// rows/item, so 120 leaves >4× headroom and never blanks the viewport
// even when items are tiny.
const MAX_MOUNTED = 120
const COLD_START = 30
// Floor on unmeasured row height used when computing coverage — guarantees
// the mounted span physically reaches the viewport bottom regardless of how
// small items actually are (at the cost of over-mounting when items are
// larger; overscan absorbs that).
const PESSIMISTIC = 1
// Tightest safe scrollTop bin for the useSyncExternalStore snapshot. Small
// wheel ticks that don't cross a bin short-circuit React's commit entirely;
// Ink keeps painting via ScrollBox.forceRender + direct scrollTop reads.
// Half of OVERSCAN keeps ≥20 rows of cushion before the mounted range
// would actually need to shift.
const QUANTUM = OVERSCAN >> 1
const FREEZE_RENDERS = 2

const upperBound = (arr: number[], target: number) => {
  let lo = 0
  let hi = arr.length

  while (lo < hi) {
    const mid = (lo + hi) >> 1

    arr[mid]! <= target ? (lo = mid + 1) : (hi = mid)
  }

  return lo
}

export const shouldSetVirtualClamp = ({
  itemCount,
  liveTailActive = false,
  sticky,
  viewportHeight
}: {
  itemCount: number
  liveTailActive?: boolean
  sticky: boolean
  viewportHeight: number
}) => itemCount > 0 && viewportHeight > 0 && !sticky && !liveTailActive

export const ensureVirtualItemHeight = (
  heights: Map<string, number>,
  key: string,
  index: number,
  estimate: number,
  estimateHeight?: (index: number, key: string) => number
) => {
  const cached = heights.get(key)

  if (cached !== undefined) {
    return Math.max(1, Math.floor(cached))
  }

  const seeded = Math.max(1, Math.floor(estimateHeight?.(index, key) ?? estimate))
  heights.set(key, seeded)

  return seeded
}

export function useVirtualHistory(
  scrollRef: RefObject<ScrollBoxHandle | null>,
  items: readonly { key: string }[],
  columns: number,
  { estimate = ESTIMATE, overscan = OVERSCAN, maxMounted = MAX_MOUNTED, coldStartCount = COLD_START } = {}
) {
  const nodes = useRef(new Map<string, unknown>())
  const heights = useRef(new Map(initialHeights))
  const initialHeightsRef = useRef(initialHeights)
  const refs = useRef(new Map<string, (el: unknown) => void>())
  const onHeightsChangeRef = useRef(onHeightsChange)
  // Bump whenever heightCache mutates so offsets rebuild on next read.
  // Ref (not state) — checked during render phase, zero extra commits.
  const offsetVersion = useRef(0)

  // Cached offsets: reused Float64Array keyed on (itemCount, version) so we
  // only rebuild when something actually changed. Previous approach allocated
  // a fresh Array(n+1) every render — at n=10k that's ~80KB/render of GC
  // pressure during streaming.
  const offsetsCache = useRef<{ arr: Float64Array; n: number; version: number }>({
    arr: new Float64Array(0),
    n: -1,
    version: -1
  })

  const [hasScrollRef, setHasScrollRef] = useState(false)
  // Height cache writes happen in layout effects; bump once so offsets and
  // clamp bounds rebuild without waiting for the next scroll/input event.
  const [measuredHeightVersion, bumpMeasuredHeightVersion] = useState(0)
  const metrics = useRef({ sticky: true, top: 0, vp: 0 })
  const lastScrollTopRef = useRef(0)

  // Width change: scale cached heights by oldCols/newCols instead of clearing
  // (clearing forces a pessimistic back-walk mounting ~190 rows at once, each
  // a fresh marked.lexer + syntax highlight ≈ 3ms). Freeze the mount range
  // for 2 renders so warm memos survive; skip one measurement pass so
  // useLayoutEffect doesn't poison the scaled cache with pre-resize Yoga
  // heights.
  const prevColumns = useRef(columns)
  const skipMeasurement = useRef(false)
  const prevRange = useRef<null | readonly [number, number]>(null)
  const freezeRenders = useRef(0)

  onHeightsChangeRef.current = onHeightsChange

  if (initialHeightsRef.current !== initialHeights) {
    initialHeightsRef.current = initialHeights
    heights.current = new Map(initialHeights)
    offsetVersion.current++
  }

  if (prevColumns.current !== columns && prevColumns.current > 0 && columns > 0) {
    const ratio = prevColumns.current / columns

    prevColumns.current = columns

    for (const [k, h] of heights.current) {
      heights.current.set(k, Math.max(1, Math.round(h * ratio)))
    }

    offsetVersion.current++
    skipMeasurement.current = true
    freezeRenders.current = FREEZE_RENDERS
  }

  // Width change: scale cached heights (not clear — clearing forces a
  // pessimistic back-walk mounting ~190 rows at once, each a fresh
  // marked.lexer + syntax highlight ≈ 3ms). Freeze mount range for 2
  // renders so warm memos survive; skip one measurement so useLayoutEffect
  // doesn't poison the scaled cache with pre-resize Yoga heights.
  const prevColumns = useRef(columns)
  const skipMeasurement = useRef(false)
  const prevRange = useRef<null | readonly [number, number]>(null)
  const freezeRenders = useRef(0)

  if (prevColumns.current !== columns && prevColumns.current > 0 && columns > 0) {
    const ratio = prevColumns.current / columns

    prevColumns.current = columns

    for (const [k, h] of heights.current) {
      heights.current.set(k, Math.max(1, Math.round(h * ratio)))
    }

    skipMeasurement.current = true
    freezeRenders.current = FREEZE_RENDERS
  }

  useLayoutEffect(() => {
    setHasScrollRef(Boolean(scrollRef.current))
  }, [scrollRef])

  // Quantized snapshot: same-bin scrolls (most wheel ticks) produce the same
  // number → React.Object.is short-circuits the commit entirely. sticky state
  // is folded in via the sign bit so sticky→broken transitions also trigger.
  // Uses the TARGET (committed + pendingDelta), not committed scrollTop, so
  // scrollBy notifications immediately remount for the destination before
  // Ink's drain frames need the children.
  const subscribe = useCallback(
    (cb: () => void) => (hasScrollRef ? scrollRef.current?.subscribe(cb) : null) ?? NOOP,
    [hasScrollRef, scrollRef]
  )

  useSyncExternalStore(
    subscribe,
    () => {
      const s = scrollRef.current

      if (!s) {
        return NaN
      }

      const target = s.getScrollTop() + s.getPendingDelta()
      const bin = Math.floor(target / QUANTUM)

      return s.isSticky() ? ~bin : bin
    },
    () => NaN
  )

  useEffect(() => {
    const keep = new Set(items.map(i => i.key))
    let dirty = false

    for (const k of heights.current.keys()) {
      if (!keep.has(k)) {
        heights.current.delete(k)
        nodes.current.delete(k)
        refs.current.delete(k)
        dirty = true
      }
    }

    if (dirty) {
      offsetVersion.current++
    }
  }, [items])

  // Offsets: Float64Array reused across renders, invalidated by offsetVersion
  // bumps from heightCache writers (measureRef, resize-scale, GC). Binary
  // search tolerates either monotone source, so no need to rebuild unless
  // something changed.
  const n = items.length

  if (offsetsCache.current.version !== offsetVersion.current || offsetsCache.current.n !== n) {
    const arr = offsetsCache.current.arr.length >= n + 1 ? offsetsCache.current.arr : new Float64Array(n + 1)

    arr[0] = 0

    for (let i = 0; i < n; i++) {
      arr[i + 1] = arr[i]! + ensureVirtualItemHeight(heights.current, items[i]!.key, i, estimate, estimateHeight)
    }

    offsetsCache.current = { arr, n, version: offsetVersion.current }
  }

  const n = items.length
  const total = offsets[n] ?? 0
  const top = Math.max(0, scrollRef.current?.getScrollTop() ?? 0)
  const pendingDelta = scrollRef.current?.getPendingDelta() ?? 0
  const target = Math.max(0, top + pendingDelta)
  const vp = Math.max(0, scrollRef.current?.getViewportHeight() ?? 0)
  const sticky = scrollRef.current?.isSticky() ?? true
  const recentManual = Date.now() - (scrollRef.current?.getLastManualScrollAt() ?? 0) < 1200

  // During a freeze, drop the frozen range if items shrank past its start
  // (/clear, compaction) — clamping would collapse to an empty mount and
  // flash blank. Fall through to the normal path in that case.
  const frozenRange =
    freezeRenders.current > 0 && prevRange.current && prevRange.current[0] < n ? prevRange.current : null

  let start = 0
  let end = n

  if (frozenRange) {
    start = frozenRange[0]
    end = Math.min(frozenRange[1], n)
  } else if (n > 0) {
    if (vp <= 0) {
      start = Math.max(0, n - coldStartCount)
    } else {
      start = Math.max(0, Math.min(n - 1, upperBound(offsets, Math.max(0, top - overscan)) - 1))
      end = Math.max(start + 1, Math.min(n, upperBound(offsets, top + vp + overscan)))
    }
  }

  if (end - start > maxMounted) {
    sticky ? (start = Math.max(0, end - maxMounted)) : (end = Math.min(n, start + maxMounted))
  }

  if (freezeRenders.current > 0) {
    freezeRenders.current--
  } else {
    prevRange.current = [start, end]
  }

  const measureRef = useCallback((key: string) => {
    let fn = refs.current.get(key)

    if (!fn) {
      fn = (el: unknown) => {
        if (el) {
          nodes.current.set(key, el)

          return
        }

        // Measure-at-unmount: the yogaNode is still valid here (reconciler
        // calls ref(null) before removeChild → freeRecursive), so we grab
        // the final height before WASM release. Without this, items
        // scrolled out during fast pan keep a stale estimate in heightCache
        // and offset math drifts until the next mount/remount cycle.
        const existing = nodes.current.get(key) as MeasuredNode | undefined
        const h = Math.ceil(existing?.yogaNode?.getComputedHeight?.() ?? 0)

        if (h > 0 && heights.current.get(key) !== h) {
          heights.current.set(key, h)
          offsetVersion.current++
          onHeightsChangeRef.current?.(heights.current)
        }

        nodes.current.delete(key)
      }

      refs.current.set(key, fn)
    }

    return fn
  }, [])

  useLayoutEffect(() => {
    const s = scrollRef.current
    let dirty = false
    let heightDirty = false

    if (skipMeasurement.current) {
      skipMeasurement.current = false
    } else {
      for (let i = start; i < end; i++) {
        const k = items[i]?.key

        if (!k) {
          continue
        }

        const h = Math.ceil((nodes.current.get(k) as MeasuredNode | undefined)?.yogaNode?.getComputedHeight?.() ?? 0)

        if (h > 0 && heights.current.get(k) !== h) {
          heights.current.set(k, h)
          dirty = true
        }
      }
    }

    if (skipMeasurement.current) {
      skipMeasurement.current = false
    } else {
      for (let i = effStart; i < effEnd; i++) {
        const k = items[i]?.key

        if (!k) {
          continue
        }

        const h = Math.ceil((nodes.current.get(k) as MeasuredNode | undefined)?.yogaNode?.getComputedHeight?.() ?? 0)

        if (h > 0 && heights.current.get(k) !== h) {
          heights.current.set(k, h)
          dirty = true
          heightDirty = true
        }
      }
    }

    if (s) {
      const next = {
        sticky: s.isSticky(),
        top: Math.max(0, s.getScrollTop() + s.getPendingDelta()),
        vp: Math.max(0, s.getViewportHeight())
      }

      if (
        next.sticky !== metrics.current.sticky ||
        next.top !== metrics.current.top ||
        next.vp !== metrics.current.vp
      ) {
        metrics.current = next
        dirty = true
      }
    }

    if (dirty) {
      offsetVersion.current++
      onHeightsChangeRef.current?.(heights.current)
    }

    if (heightDirty) {
      bumpMeasuredHeightVersion(n => n + 1)
    }
  }, [effEnd, effStart, items, liveTailActive, measuredHeightVersion, n, offsets, scrollRef, sticky, total, vp])

  return {
    bottomSpacer: Math.max(0, total - (offsets[effEnd] ?? total)),
    end: effEnd,
    measureRef,
    offsets,
    start: effStart,
    topSpacer: offsets[effStart] ?? 0
  }
}

interface MeasuredNode {
  yogaNode?: { getComputedHeight?: () => number } | null
}

interface VirtualHistoryOptions {
  coldStartCount?: number
  estimate?: number
  estimateHeight?: (index: number, key: string) => number
  initialHeights?: ReadonlyMap<string, number>
  liveTailActive?: boolean
  maxMounted?: number
  onHeightsChange?: (heights: ReadonlyMap<string, number>) => void
  overscan?: number
}
