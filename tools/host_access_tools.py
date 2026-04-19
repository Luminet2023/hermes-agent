"""Task-scoped host environment switch tools."""

from __future__ import annotations

import json
import os
from typing import Any

from tools.approval import request_operation_approval
from tools.registry import registry
from tools.task_env_state import (
    clear_task_env_overrides,
    get_effective_env_config,
    get_task_env_overrides,
    register_task_env_overrides,
)


def _json_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _effective_task_id(task_id: str | None) -> str:
    return str(task_id or "default")


def _base_env_type() -> str:
    from tools.terminal_tool import _build_env_config

    return _build_env_config(os.getenv("TERMINAL_ENV", "local")).get("env_type", "local")


def _base_local_cwd() -> str:
    from tools.terminal_tool import _build_env_config

    return _build_env_config("local").get("cwd", os.getcwd())


def _apply_task_override_state(task_id: str, overrides: dict[str, Any]) -> None:
    normalized = {key: value for key, value in (overrides or {}).items() if value is not None}
    clear_task_env_overrides(task_id)
    if normalized:
        register_task_env_overrides(task_id, normalized)


def _cleanup_task_environment(task_id: str) -> None:
    from tools.file_tools import clear_file_ops_cache
    from tools.terminal_tool import cleanup_vm

    cleanup_vm(task_id, strict_mount_cleanup=True)
    clear_file_ops_cache(task_id)


def request_host_env(task_id: str | None = None) -> str:
    """Switch the current task from its current backend to the host/local backend."""
    effective_task_id = _effective_task_id(task_id)
    current_config = get_effective_env_config(effective_task_id)
    current_env_type = current_config.get("env_type", "local")

    if current_env_type == "local":
        return _json_response({
            "success": True,
            "task_id": effective_task_id,
            "from_env_type": "local",
            "to_env_type": "local",
            "approval": {"required": False, "approved": True, "choice": "once"},
            "changed": False,
            "message": "Task is already using the host environment.",
        })

    approval = request_operation_approval(
        command=f"switch task {effective_task_id} from {current_env_type} to host/local execution",
        description=(
            f"Allow this task to leave the {current_env_type} sandbox and run on the host. "
            "The current task environment will be cleaned up first."
        ),
        title="Host Environment Approval",
    )
    if not approval.get("approved"):
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "from_env_type": current_env_type,
            "to_env_type": "local",
            "approval": approval,
            "changed": False,
            "message": approval.get("message"),
        })

    overrides = get_task_env_overrides(effective_task_id)
    next_overrides = dict(overrides)
    previous_cwd_present = "cwd" in overrides
    previous_cwd = overrides.get("cwd")

    next_overrides["previous_env_type"] = current_env_type
    next_overrides["previous_cwd_present"] = previous_cwd_present
    if previous_cwd_present:
        next_overrides["previous_cwd"] = previous_cwd
    else:
        next_overrides.pop("previous_cwd", None)

    next_overrides["env_type"] = "local"
    next_overrides["cwd"] = _base_local_cwd()

    try:
        _cleanup_task_environment(effective_task_id)
    except Exception as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "from_env_type": current_env_type,
            "to_env_type": "local",
            "approval": approval,
            "changed": False,
            "message": f"Failed to clean the current task environment: {exc}",
        })
    _apply_task_override_state(effective_task_id, next_overrides)

    return _json_response({
        "success": True,
        "task_id": effective_task_id,
        "from_env_type": current_env_type,
        "to_env_type": "local",
        "approval": approval,
        "changed": True,
    })


def restore_sandbox_env(task_id: str | None = None) -> str:
    """Restore the task back to its previous or default backend."""
    effective_task_id = _effective_task_id(task_id)
    overrides = get_task_env_overrides(effective_task_id)
    current_config = get_effective_env_config(effective_task_id)
    current_env_type = current_config.get("env_type", "local")
    default_env_type = _base_env_type()
    previous_env_type = str(overrides.get("previous_env_type") or "").strip() or None
    target_env_type = previous_env_type or default_env_type

    bookkeeping_keys = {"previous_env_type", "previous_cwd", "previous_cwd_present"}
    if current_env_type == target_env_type and not (bookkeeping_keys & set(overrides)):
        return _json_response({
            "success": True,
            "task_id": effective_task_id,
            "from_env_type": current_env_type,
            "to_env_type": target_env_type,
            "approval": {"required": False, "approved": True, "choice": "once"},
            "changed": False,
            "message": "Task is already using the restored backend.",
        })

    next_overrides = dict(overrides)
    next_overrides.pop("previous_env_type", None)
    previous_cwd_present = bool(next_overrides.pop("previous_cwd_present", False))
    previous_cwd = next_overrides.pop("previous_cwd", None)

    if previous_cwd_present:
        next_overrides["cwd"] = previous_cwd
    else:
        next_overrides.pop("cwd", None)

    if target_env_type != default_env_type:
        next_overrides["env_type"] = target_env_type
    else:
        next_overrides.pop("env_type", None)

    try:
        _cleanup_task_environment(effective_task_id)
    except Exception as exc:
        return _json_response({
            "success": False,
            "task_id": effective_task_id,
            "from_env_type": current_env_type,
            "to_env_type": target_env_type,
            "approval": {"required": False, "approved": True, "choice": "once"},
            "changed": False,
            "message": f"Failed to clean the current task environment: {exc}",
        })
    _apply_task_override_state(effective_task_id, next_overrides)

    return _json_response({
        "success": True,
        "task_id": effective_task_id,
        "from_env_type": current_env_type,
        "to_env_type": target_env_type,
        "approval": {"required": False, "approved": True, "choice": "once"},
        "changed": current_env_type != target_env_type,
        "restored_from_previous": bool(previous_env_type),
    })


REQUEST_HOST_ENV_SCHEMA = {
    "name": "request_host_env",
    "description": (
        "Request approval to switch the current task from its active sandbox "
        "backend to the host/local backend."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


RESTORE_SANDBOX_ENV_SCHEMA = {
    "name": "restore_sandbox_env",
    "description": (
        "Restore the current task back to its previous or default backend "
        "after a host/local phase."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _handle_request_host_env(_args: dict[str, Any], **kw) -> str:
    return request_host_env(task_id=kw.get("task_id"))


def _handle_restore_sandbox_env(_args: dict[str, Any], **kw) -> str:
    return restore_sandbox_env(task_id=kw.get("task_id"))


registry.register(
    name="request_host_env",
    toolset="terminal",
    schema=REQUEST_HOST_ENV_SCHEMA,
    handler=_handle_request_host_env,
    emoji="🧷",
)

registry.register(
    name="restore_sandbox_env",
    toolset="terminal",
    schema=RESTORE_SANDBOX_ENV_SCHEMA,
    handler=_handle_restore_sandbox_env,
    emoji="🔒",
)
