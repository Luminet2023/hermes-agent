import { Typography } from "@nous-research/ui";
import { useI18n } from "@/i18n/context";
import { LOCALE_META } from "@/i18n";
import type { Locale } from "@/i18n";

/**
 * Language picker — shows the current language's flag + endonym, opens a
 * dropdown of all supported locales when clicked.  Persists choice to
 * localStorage via the I18n context.
 *
 * Replaces the older two-state EN↔ZH toggle now that we ship 16 locales
 * (en, zh, zh-hant, ja, de, es, fr, tr, uk, af, ko, it, ga, pt, ru, hu).
 */
export function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape so the dropdown doesn't trap the user.
  useEffect(() => {
    if (!open) return;

    function onPointerDown(e: PointerEvent) {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }

    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const current = LOCALE_META[locale];
  const allLocales = Object.entries(LOCALE_META) as Array<[Locale, typeof current]>;

  return (
    <button
      type="button"
      onClick={toggle}
      className="group relative inline-flex items-center gap-1.5 px-2 py-1 text-xs text-muted-foreground hover:text-foreground transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      title={t.language.switchTo}
      aria-label={t.language.switchTo}
    >
      {/* Show the *current* language's flag — tooltip advertises the click action */}
      <span className="text-base leading-none">
        {locale === "en" ? "🇬🇧" : "🇨🇳"}
      </span>
      <Typography
        mondwest
        className="hidden sm:inline tracking-wide uppercase text-[0.65rem]"
      >
        {locale === "en" ? "EN" : "中文"}
      </Typography>
    </button>
  );
}
