"""Dynamic Docker host mount helpers for running task sandboxes."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.config import load_config
from tools.environments.base import get_sandbox_dir
from tools.path_security import has_traversal_component, validate_within_dir
from tools.task_env_state import (
    clear_task_docker_mount_runtime,
    get_task_docker_mount_runtime,
    set_task_docker_mount_runtime,
    update_task_docker_mount_runtime,
)

logger = logging.getLogger(__name__)

_DEFAULT_HELPER_TIMEOUT_SECONDS = 60
_HUB_CONTAINER_PATH = "/__hermes_host_mounts"
_HELPER_MODES = frozenset({"host-nsenter", "privileged-helper-container"})
_TASK_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_mount_runtime_lock = threading.RLock()


class DockerMountError(RuntimeError):
    """Raised when dynamic Docker mount preparation or cleanup fails."""


@dataclass(frozen=True)
class DockerMountHelperConfig:
    mode: str
    wrapper: str
    helper_image: str
    helper_prepare_command: str
    timeout: int


@dataclass(frozen=True)
class DockerDynamicMountSettings:
    enabled: bool
    mounts_root: Path
    allowed_roots: tuple[Path, ...]
    helper: DockerMountHelperConfig


def _normalize_timeout(raw_timeout: Any) -> int:
    if raw_timeout in (None, ""):
        return _DEFAULT_HELPER_TIMEOUT_SECONDS
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("docker_mount_helper.timeout must be a positive integer.") from exc
    if timeout <= 0:
        raise ValueError("docker_mount_helper.timeout must be a positive integer.")
    return timeout


def _normalize_helper_mode(raw_mode: Any) -> str:
    mode = str(raw_mode or "host-nsenter").strip().lower() or "host-nsenter"
    if mode not in _HELPER_MODES:
        allowed = ", ".join(sorted(_HELPER_MODES))
        raise ValueError(f"docker_mount_helper.mode must be one of: {allowed}.")
    return mode


def _validate_wrapper_path(wrapper: Any) -> str:
    wrapper_path = str(wrapper or "").strip()
    if not wrapper_path:
        raise ValueError("docker_mount_helper.wrapper must be configured.")
    if not os.path.isabs(wrapper_path):
        raise ValueError("docker_mount_helper.wrapper must be an absolute path.")

    try:
        wrapper_stat = os.stat(wrapper_path)
    except OSError as exc:
        raise ValueError(f"docker_mount_helper.wrapper is not accessible: {exc}") from exc

    if not stat.S_ISREG(wrapper_stat.st_mode):
        raise ValueError("docker_mount_helper.wrapper must point to a regular file.")

    if getattr(wrapper_stat, "st_uid", None) != 0:
        raise PermissionError("docker_mount_helper.wrapper must be owned by root.")

    if wrapper_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(
            "docker_mount_helper.wrapper must not be group- or world-writable."
        )

    if not os.access(wrapper_path, os.X_OK):
        raise PermissionError("docker_mount_helper.wrapper must be executable.")

    return wrapper_path


def _normalize_mounts_root(raw_root: Any) -> Path:
    if raw_root in (None, ""):
        return (get_sandbox_dir() / "docker-mounts").resolve()
    root = Path(os.path.expanduser(str(raw_root))).resolve()
    if not root.is_absolute():
        raise ValueError("docker_dynamic_mounts_root must be an absolute path.")
    return root


def _normalize_allowed_roots(raw_allowed_roots: Any) -> tuple[Path, ...]:
    if raw_allowed_roots in (None, ""):
        return ()
    if not isinstance(raw_allowed_roots, list):
        raise ValueError("docker_dynamic_mounts_allowed_roots must be a list.")

    normalized: list[Path] = []
    for item in raw_allowed_roots:
        if not isinstance(item, str):
            raise ValueError("docker_dynamic_mounts_allowed_roots must contain strings.")
        root_text = item.strip()
        if not root_text:
            continue
        root_path = Path(os.path.expanduser(root_text)).resolve()
        if not root_path.is_absolute():
            raise ValueError("docker_dynamic_mounts_allowed_roots entries must be absolute paths.")
        normalized.append(root_path)
    return tuple(normalized)


def load_docker_mount_settings() -> DockerDynamicMountSettings:
    """Load and normalize Docker dynamic mount settings from config.yaml."""
    config = load_config()
    terminal_config = config.get("terminal", {}) or {}

    helper_config = terminal_config.get("docker_mount_helper", {}) or {}
    helper = DockerMountHelperConfig(
        mode=_normalize_helper_mode(helper_config.get("mode")),
        wrapper=_validate_wrapper_path(helper_config.get("wrapper"))
        if terminal_config.get("docker_dynamic_mounts_enabled", False)
        else str(helper_config.get("wrapper") or "").strip(),
        helper_image=str(helper_config.get("helper_image") or "").strip(),
        helper_prepare_command=str(helper_config.get("helper_prepare_command") or "").strip(),
        timeout=_normalize_timeout(helper_config.get("timeout")),
    )

    return DockerDynamicMountSettings(
        enabled=bool(terminal_config.get("docker_dynamic_mounts_enabled", False)),
        mounts_root=_normalize_mounts_root(terminal_config.get("docker_dynamic_mounts_root")),
        allowed_roots=_normalize_allowed_roots(
            terminal_config.get("docker_dynamic_mounts_allowed_roots", [])
        ),
        helper=helper,
    )


def _safe_task_token(task_id: str) -> str:
    raw = str(task_id or "default")
    normalized = _TASK_ID_SAFE_RE.sub("-", raw).strip("-.") or "default"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[:48]}-{digest}"


def _helper_from_runtime(runtime: dict[str, Any]) -> DockerMountHelperConfig:
    return DockerMountHelperConfig(
        mode=_normalize_helper_mode(runtime.get("helper_mode")),
        wrapper=_validate_wrapper_path(runtime.get("helper_wrapper")),
        helper_image=str(runtime.get("helper_image") or "").strip(),
        helper_prepare_command=str(runtime.get("helper_prepare_command") or "").strip(),
        timeout=_normalize_timeout(runtime.get("helper_timeout")),
    )


def _helper_command(
    subcommand: str,
    *,
    helper: DockerMountHelperConfig,
    task_id: str,
    task_root: Path,
    hub_host_path: Path,
    slot_host_path: Path | None = None,
    source_path: Path | None = None,
    source_kind: str | None = None,
    target_container_id: str | None = None,
    container_mount_path: str | None = None,
) -> list[str]:
    command = [
        helper.wrapper,
        subcommand,
        "--mode", helper.mode,
        "--task-id", task_id,
        "--task-root", str(task_root),
        "--hub-path", str(hub_host_path),
    ]
    if slot_host_path is not None:
        command.extend(["--slot-path", str(slot_host_path)])
    if source_path is not None:
        command.extend(["--source-path", str(source_path)])
    if source_kind:
        command.extend(["--source-kind", source_kind])
    if target_container_id:
        command.extend(["--target-container-id", target_container_id])
    if container_mount_path:
        command.extend(["--container-mount-path", container_mount_path])
    if helper.helper_image:
        command.extend(["--helper-image", helper.helper_image])
    if helper.helper_prepare_command:
        command.extend(["--helper-prepare-command", helper.helper_prepare_command])
    return command


def _run_helper_command(
    subcommand: str,
    *,
    helper: DockerMountHelperConfig,
    task_id: str,
    task_root: Path,
    hub_host_path: Path,
    slot_host_path: Path | None = None,
    source_path: Path | None = None,
    source_kind: str | None = None,
    target_container_id: str | None = None,
    container_mount_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _helper_command(
        subcommand,
        helper=helper,
        task_id=task_id,
        task_root=task_root,
        hub_host_path=hub_host_path,
        slot_host_path=slot_host_path,
        source_path=source_path,
        source_kind=source_kind,
        target_container_id=target_container_id,
        container_mount_path=container_mount_path,
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=helper.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerMountError(
            f"Docker mount helper '{subcommand}' timed out after {helper.timeout}s."
        ) from exc
    except OSError as exc:
        raise DockerMountError(f"Failed to execute Docker mount helper: {exc}") from exc

    if completed.returncode != 0:
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise DockerMountError(
            f"Docker mount helper '{subcommand}' failed: {detail}"
        )
    return completed


def _task_paths(task_id: str, mounts_root: Path) -> tuple[Path, Path]:
    task_root = mounts_root / _safe_task_token(task_id)
    hub_host_path = task_root / "hub"
    return task_root, hub_host_path


def _prepare_slot_placeholder(slot_host_path: Path, source_kind: str) -> None:
    slot_host_path.parent.mkdir(parents=True, exist_ok=True)
    if source_kind == "dir":
        slot_host_path.mkdir(parents=True, exist_ok=True)
        return

    slot_host_path.parent.mkdir(parents=True, exist_ok=True)
    if slot_host_path.exists() and slot_host_path.is_dir():
        raise DockerMountError(
            f"Dynamic mount slot path already exists as a directory: {slot_host_path}"
        )
    slot_host_path.touch(exist_ok=True)


def _cleanup_slot_placeholder(slot_host_path: Path) -> None:
    try:
        if slot_host_path.is_dir():
            shutil.rmtree(slot_host_path, ignore_errors=True)
        else:
            slot_host_path.unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to remove dynamic mount slot placeholder %s", slot_host_path, exc_info=True)


def prepare_task_mount_runtime(task_id: str) -> dict[str, Any] | None:
    """Prepare a per-task host mount hub and persist runtime metadata."""
    with _mount_runtime_lock:
        existing = get_task_docker_mount_runtime(task_id)
        if existing is not None:
            return existing

        settings = load_docker_mount_settings()
        if not settings.enabled:
            return None

        task_root, hub_host_path = _task_paths(task_id, settings.mounts_root)
        task_root.mkdir(parents=True, exist_ok=True)
        hub_host_path.mkdir(parents=True, exist_ok=True)

        try:
            _run_helper_command(
                "prepare-hub",
                helper=settings.helper,
                task_id=task_id,
                task_root=task_root,
                hub_host_path=hub_host_path,
            )
        except Exception:
            shutil.rmtree(task_root, ignore_errors=True)
            raise

        runtime = {
            "task_id": task_id,
            "task_mount_root": str(task_root),
            "hub_host_path": str(hub_host_path),
            "hub_container_path": _HUB_CONTAINER_PATH,
            "container_id": None,
            "helper_mode": settings.helper.mode,
            "helper_wrapper": settings.helper.wrapper,
            "helper_image": settings.helper.helper_image,
            "helper_prepare_command": settings.helper.helper_prepare_command,
            "helper_timeout": settings.helper.timeout,
            "mounts": {},
            "next_mount_index": 1,
        }
        set_task_docker_mount_runtime(task_id, runtime)
        return runtime


def register_task_container_id(task_id: str, container_id: str | None) -> dict[str, Any] | None:
    """Persist the Docker container ID for an active task mount runtime."""
    normalized_container_id = str(container_id or "").strip() or None
    return update_task_docker_mount_runtime(
        task_id,
        {"container_id": normalized_container_id},
    )


def build_hub_mount_arg(runtime: dict[str, Any]) -> str:
    """Build the fixed Docker --mount specification for the task hub."""
    return (
        f"type=bind,src={runtime['hub_host_path']},dst={runtime['hub_container_path']},"
        "bind-propagation=rshared"
    )


def validate_requested_host_path(host_path: str) -> Path:
    """Resolve and validate a requested host path against configured allow-roots."""
    settings = load_docker_mount_settings()
    if not settings.enabled:
        raise DockerMountError("Dynamic Docker host mounts are disabled.")
    if not settings.allowed_roots:
        raise DockerMountError("No docker_dynamic_mounts_allowed_roots are configured.")

    requested = str(host_path or "").strip()
    if not requested:
        raise DockerMountError("host_path is required.")
    if "\x00" in requested:
        raise DockerMountError("host_path may not contain NUL bytes.")
    if has_traversal_component(requested):
        raise DockerMountError("host_path may not contain '..' traversal components.")

    candidate = Path(os.path.expanduser(requested))
    if not candidate.is_absolute():
        raise DockerMountError("host_path must be an absolute path.")

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DockerMountError(f"host_path is not accessible: {exc}") from exc

    if not (resolved.is_dir() or resolved.is_file()):
        raise DockerMountError("Only existing files and directories can be mounted.")

    for allowed_root in settings.allowed_roots:
        if validate_within_dir(resolved, allowed_root) is None:
            return resolved

    allowed = ", ".join(str(root) for root in settings.allowed_roots)
    raise DockerMountError(
        f"host_path is outside the configured allowed roots: {allowed}"
    )


def mount_host_path(task_id: str, host_path: str, *, readonly: bool = True) -> dict[str, Any]:
    """Bind a validated host path into an already-running task mount hub."""
    with _mount_runtime_lock:
        runtime = get_task_docker_mount_runtime(task_id)
        if runtime is None:
            raise DockerMountError(
                "Dynamic mount hub is not initialized for this task. "
                "Recreate the Docker sandbox with docker_dynamic_mounts_enabled."
            )

        resolved_host_path = validate_requested_host_path(host_path)
        mounts = runtime.get("mounts", {}) or {}

        for mount_id, metadata in mounts.items():
            if metadata.get("resolved_host_path") != str(resolved_host_path):
                continue
            if bool(metadata.get("readonly", True)) != bool(readonly):
                raise DockerMountError(
                    "This host path is already mounted with different readonly semantics."
                )
            return {
                "host_path": host_path,
                "readonly": bool(readonly),
                "mount_id": mount_id,
                "container_path": metadata["container_path"],
                "helper_mode": runtime["helper_mode"],
                "changed": False,
            }

        mount_index = int(runtime.get("next_mount_index", 1))
        mount_id = f"mount-{mount_index:04d}"
        task_root = Path(runtime["task_mount_root"])
        hub_host_path = Path(runtime["hub_host_path"])
        slot_host_path = hub_host_path / mount_id
        container_mount_path = f"{runtime['hub_container_path']}/{mount_id}"
        source_kind = "dir" if resolved_host_path.is_dir() else "file"
        helper = _helper_from_runtime(runtime)
        container_id = str(runtime.get("container_id") or "").strip()

        if readonly and not container_id:
            raise DockerMountError(
                "Dynamic readonly mounts require an active Docker container ID. "
                "Recreate the Docker sandbox and try again."
            )

        _prepare_slot_placeholder(slot_host_path, source_kind)
        try:
            _run_helper_command(
                "bind",
                helper=helper,
                task_id=task_id,
                task_root=task_root,
                hub_host_path=hub_host_path,
                slot_host_path=slot_host_path,
                source_path=resolved_host_path,
                source_kind=source_kind,
            )
            if readonly:
                try:
                    _run_helper_command(
                        "remount-readonly",
                        helper=helper,
                        task_id=task_id,
                        task_root=task_root,
                        hub_host_path=hub_host_path,
                        slot_host_path=slot_host_path,
                        target_container_id=container_id,
                        container_mount_path=container_mount_path,
                    )
                except DockerMountError:
                    try:
                        _run_helper_command(
                            "unbind",
                            helper=helper,
                            task_id=task_id,
                            task_root=task_root,
                            hub_host_path=hub_host_path,
                            slot_host_path=slot_host_path,
                        )
                    except DockerMountError:
                        logger.warning(
                            "Readonly remount failed and rollback unbind also failed for %s",
                            slot_host_path,
                            exc_info=True,
                        )
                    raise
        except Exception:
            _cleanup_slot_placeholder(slot_host_path)
            raise

        runtime["next_mount_index"] = mount_index + 1
        runtime.setdefault("mounts", {})[mount_id] = {
            "host_path": host_path,
            "resolved_host_path": str(resolved_host_path),
            "readonly": bool(readonly),
            "container_path": container_mount_path,
            "slot_host_path": str(slot_host_path),
            "source_kind": source_kind,
        }
        set_task_docker_mount_runtime(task_id, runtime)

        return {
            "host_path": host_path,
            "readonly": bool(readonly),
            "mount_id": mount_id,
            "container_path": container_mount_path,
            "helper_mode": runtime["helper_mode"],
            "changed": True,
        }


def cleanup_task_mounts(task_id: str, *, strict: bool = False) -> bool:
    """Clean all dynamic mounts and mount-hub state for *task_id*."""
    with _mount_runtime_lock:
        runtime = get_task_docker_mount_runtime(task_id)
        if runtime is None:
            return True

        task_root = Path(runtime["task_mount_root"])
        hub_host_path = Path(runtime["hub_host_path"])

        try:
            helper = _helper_from_runtime(runtime)
            _run_helper_command(
                "cleanup-task",
                helper=helper,
                task_id=task_id,
                task_root=task_root,
                hub_host_path=hub_host_path,
            )
        except Exception as exc:
            if strict:
                raise DockerMountError(f"Failed to clean dynamic Docker mounts: {exc}") from exc
            logger.warning(
                "Best-effort cleanup failed for dynamic Docker mounts on task %s: %s",
                task_id,
                exc,
            )
            return False

        clear_task_docker_mount_runtime(task_id)
        shutil.rmtree(task_root, ignore_errors=True)
        return True
