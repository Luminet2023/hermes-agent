"""Dynamic Docker host mount tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hermes_cli.config import load_config
from hermes_constants import get_hermes_home
from tools.approval import request_operation_approval
from tools.environments.docker import DockerEnvironment
from tools.environments.docker_mounts import (
    DockerMountError,
    load_docker_mount_settings,
    mount_host_path,
    validate_requested_host_path,
)
from tools.path_security import has_traversal_component, validate_within_dir
from tools.registry import registry
from tools.task_env_state import get_effective_env_config, get_task_docker_mount_runtime
from tools.terminal_tool import get_active_env

_HOME = Path.home().resolve()
_HERMES_HOME = get_hermes_home().resolve()
_LOCAL_CRITICAL_EXACT_PATHS = frozenset(
    {
        (_HOME / ".ssh" / "authorized_keys").resolve(),
        (_HOME / ".ssh" / "id_rsa").resolve(),
        (_HOME / ".ssh" / "id_ed25519").resolve(),
        (_HOME / ".ssh" / "config").resolve(),
        (_HOME / ".bashrc").resolve(),
        (_HOME / ".zshrc").resolve(),
        (_HOME / ".profile").resolve(),
        (_HOME / ".bash_profile").resolve(),
        (_HOME / ".zprofile").resolve(),
        (_HOME / ".netrc").resolve(),
        (_HOME / ".pgpass").resolve(),
        (_HOME / ".npmrc").resolve(),
        (_HOME / ".pypirc").resolve(),
        (_HERMES_HOME / ".env").resolve(),
        Path("/etc/sudoers").resolve(),
        Path("/etc/passwd").resolve(),
        Path("/etc/shadow").resolve(),
        Path("/var/run/docker.sock").resolve(),
        Path("/run/docker.sock").resolve(),
    }
)
_LOCAL_CRITICAL_PREFIXES = tuple(
    path.resolve()
    for path in (
        _HOME / ".ssh",
        _HOME / ".aws",
        _HOME / ".gnupg",
        _HOME / ".kube",
        _HOME / ".docker",
        _HOME / ".azure",
        _HOME / ".config" / "gh",
        Path("/etc"),
        Path("/boot"),
        Path("/usr/lib/systemd"),
        Path("/private/etc"),
        Path("/private/var"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path("/run"),
        Path("/var/run"),
    )
)


def _json_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _effective_task_id(task_id: str | None) -> str:
    return str(task_id or "default")


def _normalize_readonly(raw_readonly: Any) -> bool:
    if isinstance(raw_readonly, bool):
        return raw_readonly
    if raw_readonly in (None, ""):
        return True
    if isinstance(raw_readonly, str):
        normalized = raw_readonly.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError("readonly must be a boolean.")


def _approval_preview(task_id: str, host_path: str, readonly: bool) -> str:
    readonly_label = "readonly" if readonly else "read-write"
    return f"mount host path {host_path} into Docker sandbox for task {task_id} ({readonly_label})"


def _approval_description(host_path: str, readonly: bool) -> str:
    mode = "read-only" if readonly else "read-write"
    return (
        f"Allow Hermes to expose the allowlisted host path '{host_path}' to the current "
        f"Docker sandbox as a {mode} bind mount. This is a one-shot approval."
    )


def _is_same_path_or_descendant(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _load_optional_local_allowed_roots() -> tuple[Path, ...]:
    terminal_config = (load_config().get("terminal", {}) or {})
    raw_allowed_roots = terminal_config.get("docker_dynamic_mounts_allowed_roots", [])
    if raw_allowed_roots in (None, ""):
        return ()
    if not isinstance(raw_allowed_roots, list):
        raise DockerMountError("docker_dynamic_mounts_allowed_roots must be a list.")

    normalized: list[Path] = []
    for item in raw_allowed_roots:
        if not isinstance(item, str):
            raise DockerMountError("docker_dynamic_mounts_allowed_roots must contain strings.")
        root_text = item.strip()
        if not root_text:
            continue
        root_path = Path(os.path.expanduser(root_text))
        if not root_path.is_absolute():
            raise DockerMountError("docker_dynamic_mounts_allowed_roots entries must be absolute paths.")
        normalized.append(root_path.resolve())
    return tuple(normalized)


def _validate_local_host_path(host_path: str) -> Path:
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
        raise DockerMountError("Only existing files and directories can be exposed.")

    if resolved in _LOCAL_CRITICAL_EXACT_PATHS:
        raise DockerMountError(
            f"Refusing to expose critical host path in local mode: {resolved}"
        )

    for critical_root in _LOCAL_CRITICAL_PREFIXES:
        if _is_same_path_or_descendant(resolved, critical_root):
            raise DockerMountError(
                f"Refusing to expose critical host path in local mode: {resolved}"
            )

    allowed_roots = _load_optional_local_allowed_roots()
    if not allowed_roots:
        return resolved

    for allowed_root in allowed_roots:
        if validate_within_dir(resolved, allowed_root) is None:
            return resolved

    allowed = ", ".join(str(root) for root in allowed_roots)
    raise DockerMountError(
        f"host_path is outside the configured allowed roots: {allowed}"
    )


def request_host_mount(
    host_path: Any,
    readonly: Any = True,
    *,
    task_id: str | None = None,
) -> str:
    """Expose an extra host path to the current task.

    Docker sandboxes require one-shot approval and an actual bind mount.
    Local/host tasks already run on the host, so the tool becomes a no-op
    access declaration after path validation and critical-path checks.
    """
    effective_task_id = _effective_task_id(task_id)
    requested_path = str(host_path or "").strip()
    try:
        readonly_flag = _normalize_readonly(readonly)
    except ValueError as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": None,
            "mount_id": None,
            "container_path": None,
            "helper_mode": None,
            "changed": False,
            "message": str(exc),
        })

    current_env_type = get_effective_env_config(effective_task_id).get("env_type")
    if current_env_type == "local":
        try:
            resolved_host_path = _validate_local_host_path(requested_path)
        except DockerMountError as exc:
            return _json_response({
                "success": False,
                "task_id": effective_task_id,
                "approval": {"required": False, "approved": False},
                "host_path": requested_path,
                "readonly": readonly_flag,
                "mount_id": None,
                "container_path": None,
                "helper_mode": "local-direct",
                "changed": False,
                "message": str(exc),
            })

        return _json_response({
            "success": True,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": True, "choice": "once"},
            "host_path": str(resolved_host_path),
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": str(resolved_host_path),
            "helper_mode": "local-direct",
            "changed": False,
            "message": (
                "Task is already running on the host/local backend; the path is directly accessible. "
                "No additional mount or approval was required, and readonly is not additionally enforced in local mode."
            ),
        })

    try:
        settings = load_docker_mount_settings()
    except (ValueError, PermissionError) as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": None,
            "changed": False,
            "message": str(exc),
        })

    if not settings.enabled:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": settings.helper.mode,
            "changed": False,
            "message": "Dynamic Docker host mounts are disabled.",
        })

    if current_env_type != "docker":
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": settings.helper.mode,
            "changed": False,
            "message": (
                "request_host_mount only supports Docker sandboxes or local/host tasks, "
                f"not {current_env_type!r}."
            ),
        })

    active_env = get_active_env(effective_task_id)
    if not isinstance(active_env, DockerEnvironment):
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": settings.helper.mode,
            "changed": False,
            "message": "No running Docker sandbox is active for this task.",
        })

    runtime = get_task_docker_mount_runtime(effective_task_id)
    if runtime is None:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": settings.helper.mode,
            "changed": False,
            "message": (
                "The current Docker sandbox was not started with a dynamic mount hub. "
                "Recreate the sandbox after enabling docker_dynamic_mounts_enabled."
            ),
        })

    try:
        resolved_host_path = validate_requested_host_path(requested_path)
    except DockerMountError as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": False},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": runtime.get("helper_mode"),
            "changed": False,
            "message": str(exc),
        })

    for mount_id, metadata in (runtime.get("mounts", {}) or {}).items():
        if metadata.get("resolved_host_path") != str(resolved_host_path):
            continue
        if bool(metadata.get("readonly", True)) != readonly_flag:
            return _json_response({
                "success": False,
                "task_id": effective_task_id,
                "approval": {"required": False, "approved": False},
                "host_path": requested_path,
                "readonly": readonly_flag,
                "mount_id": mount_id,
                "container_path": metadata.get("container_path"),
                "helper_mode": runtime.get("helper_mode"),
                "changed": False,
                "message": "This host path is already mounted with different readonly semantics.",
            })
        return _json_response({
            "success": True,
            "task_id": effective_task_id,
            "approval": {"required": False, "approved": True, "choice": "once"},
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": mount_id,
            "container_path": metadata.get("container_path"),
            "helper_mode": runtime.get("helper_mode"),
            "changed": False,
            "message": "This host path is already mounted for the current task.",
        })

    approval = request_operation_approval(
        command=_approval_preview(effective_task_id, requested_path, readonly_flag),
        description=_approval_description(requested_path, readonly_flag),
        title="Host Mount Approval",
        choices=["once", "deny"],
    )
    if not approval.get("approved"):
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": approval,
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": runtime.get("helper_mode"),
            "changed": False,
            "message": approval.get("message"),
        })

    try:
        mount_result = mount_host_path(
            effective_task_id,
            requested_path,
            readonly=readonly_flag,
        )
    except DockerMountError as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "approval": approval,
            "host_path": requested_path,
            "readonly": readonly_flag,
            "mount_id": None,
            "container_path": None,
            "helper_mode": runtime.get("helper_mode"),
            "changed": False,
            "message": str(exc),
        })

    return _json_response({
        "success": True,
        "task_id": effective_task_id,
        "approval": approval,
        "host_path": mount_result["host_path"],
        "readonly": mount_result["readonly"],
        "mount_id": mount_result["mount_id"],
        "container_path": mount_result["container_path"],
        "helper_mode": mount_result["helper_mode"],
        "changed": mount_result["changed"],
    })


REQUEST_HOST_MOUNT_SCHEMA = {
    "name": "request_host_mount",
    "description": (
        "Expose a host file or directory to the current task. Docker sandboxes "
        "use one-shot approval and a runtime bind mount; local/host tasks perform "
        "a no-op access validation without prompting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host_path": {
                "type": "string",
                "description": "Absolute allowlisted host path to bind into the running Docker sandbox.",
            },
            "readonly": {
                "type": "boolean",
                "description": "When true, require a read-only bind mount. Defaults to true.",
                "default": True,
            },
        },
        "required": ["host_path"],
    },
}


def _handle_request_host_mount(args: dict[str, Any], **kw) -> str:
    return request_host_mount(
        host_path=args.get("host_path"),
        readonly=args.get("readonly", True),
        task_id=kw.get("task_id"),
    )


registry.register(
    name="request_host_mount",
    toolset="terminal",
    schema=REQUEST_HOST_MOUNT_SCHEMA,
    handler=_handle_request_host_mount,
    emoji="🪢",
)
