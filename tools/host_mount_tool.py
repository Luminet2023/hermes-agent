"""Dynamic Docker host mount tool."""

from __future__ import annotations

import json
from typing import Any

from tools.approval import request_operation_approval
from tools.environments.docker import DockerEnvironment
from tools.environments.docker_mounts import (
    DockerMountError,
    load_docker_mount_settings,
    mount_host_path,
    validate_requested_host_path,
)
from tools.registry import registry
from tools.task_env_state import get_effective_env_config, get_task_docker_mount_runtime
from tools.terminal_tool import get_active_env


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


def request_host_mount(
    host_path: Any,
    readonly: Any = True,
    *,
    task_id: str | None = None,
) -> str:
    """Request one-shot approval to expose an extra host path to a running Docker sandbox."""
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

    current_env_type = get_effective_env_config(effective_task_id).get("env_type")
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
            "message": f"request_host_mount only works for Docker tasks, not {current_env_type!r}.",
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
        "Request one-shot approval to expose an allowlisted host file or directory "
        "to the currently running Docker sandbox for this task."
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
