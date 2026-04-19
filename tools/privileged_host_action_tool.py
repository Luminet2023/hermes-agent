"""Restricted privileged host action tool."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from typing import Any

from hermes_cli.config import load_config
from tools.approval import request_operation_approval
from tools.registry import registry

_DEFAULT_TIMEOUT_SECONDS = 30


def _json_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _base_response(
    action: str,
    *,
    success: bool,
    approval: dict[str, Any] | None = None,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "action": action,
        "approval": approval or {"required": False, "approved": False},
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    if message:
        payload["message"] = message
    return payload


def _normalize_action_name(action: Any) -> str:
    return str(action or "").strip()


def _normalize_requested_args(args: Any) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list):
        raise ValueError("args must be a list of strings.")

    normalized: list[str] = []
    for item in args:
        if not isinstance(item, str):
            raise ValueError("args must be a list of strings.")
        if "\x00" in item:
            raise ValueError("args may not contain NUL bytes.")
        normalized.append(item)
    return normalized


def _normalize_allowed_args(raw_allowed_args: Any) -> list[str]:
    if raw_allowed_args in (None, ""):
        return []
    if not isinstance(raw_allowed_args, list):
        raise ValueError("allowed_args must be a list of strings.")

    allowed_args: list[str] = []
    for item in raw_allowed_args:
        if not isinstance(item, str):
            raise ValueError("allowed_args must be a list of strings.")
        allowed_args.append(item)
    return allowed_args


def _normalize_timeout(raw_timeout: Any) -> int:
    if raw_timeout in (None, ""):
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be an integer.") from exc
    if timeout <= 0:
        raise ValueError("timeout must be a positive integer.")
    return timeout


def _load_action_settings(action: str) -> dict[str, Any]:
    config = load_config()
    terminal_config = config.get("terminal", {}) or {}
    enabled = bool(terminal_config.get("privileged_host_actions_enabled", False))
    if not enabled:
        raise ValueError("Privileged host actions are disabled.")

    action_configs = terminal_config.get("privileged_host_actions", {}) or {}
    if not isinstance(action_configs, dict):
        raise ValueError("terminal.privileged_host_actions must be a mapping.")

    action_config = action_configs.get(action)
    if not isinstance(action_config, dict):
        raise ValueError(f"Action '{action}' is not allowlisted.")

    return action_config


def _validate_wrapper_path(wrapper: Any) -> str:
    wrapper_path = str(wrapper or "").strip()
    if not wrapper_path:
        raise ValueError("wrapper must be configured for this action.")
    if not os.path.isabs(wrapper_path):
        raise ValueError("wrapper must be an absolute path.")

    try:
        wrapper_stat = os.stat(wrapper_path)
    except OSError as exc:
        raise ValueError(f"wrapper is not accessible: {exc}") from exc

    if not stat.S_ISREG(wrapper_stat.st_mode):
        raise ValueError("wrapper must point to a regular file.")

    owner_uid = getattr(wrapper_stat, "st_uid", None)
    if owner_uid != 0:
        raise PermissionError("wrapper must be owned by root.")

    if wrapper_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("wrapper must not be group- or world-writable.")

    if not os.access(wrapper_path, os.X_OK):
        raise PermissionError("wrapper must be executable.")

    return wrapper_path


def _validate_requested_args_against_allowlist(
    requested_args: list[str],
    allowed_args: list[str],
) -> None:
    rejected = [arg for arg in requested_args if arg not in allowed_args]
    if rejected:
        rendered = ", ".join(repr(arg) for arg in rejected)
        raise ValueError(f"Arguments are not allowlisted for this action: {rendered}")


def _approval_preview(action: str, requested_args: list[str]) -> str:
    rendered = " ".join(shlex.quote(part) for part in [action, *requested_args])
    return f"run privileged host action {rendered}".strip()


def _approval_description(action: str) -> str:
    return (
        f"Allow Hermes to invoke the allowlisted privileged host action '{action}'. "
        "This is a one-shot approval and does not grant shell access."
    )


def request_privileged_host_action(action: Any, args: Any = None) -> str:
    """Execute an allowlisted privileged host wrapper after one-shot approval."""
    action_name = _normalize_action_name(action)
    if not action_name:
        return _json_response(_base_response(
            "",
            success=False,
            message="action is required.",
        ))

    try:
        requested_args = _normalize_requested_args(args)
        action_config = _load_action_settings(action_name)
        allowed_args = _normalize_allowed_args(action_config.get("allowed_args", []))
        timeout = _normalize_timeout(action_config.get("timeout"))
        wrapper_path = _validate_wrapper_path(action_config.get("wrapper"))
        _validate_requested_args_against_allowlist(requested_args, allowed_args)
    except (ValueError, PermissionError) as exc:
        return _json_response(_base_response(
            action_name,
            success=False,
            message=str(exc),
        ))

    approval = request_operation_approval(
        command=_approval_preview(action_name, requested_args),
        description=_approval_description(action_name),
        title="Privileged Host Action Approval",
        choices=["once", "deny"],
    )
    if not approval.get("approved"):
        return _json_response(_base_response(
            action_name,
            success=False,
            approval=approval,
            message=approval.get("message"),
        ))

    command = [wrapper_path, *requested_args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return _json_response(_base_response(
            action_name,
            success=False,
            approval=approval,
            stdout=stdout,
            stderr=stderr,
            message=f"Privileged host action timed out after {timeout}s.",
        ))
    except OSError as exc:
        return _json_response(_base_response(
            action_name,
            success=False,
            approval=approval,
            message=f"Failed to execute wrapper: {exc}",
        ))

    success = completed.returncode == 0
    message = None if success else f"Wrapper exited with code {completed.returncode}."
    return _json_response(_base_response(
        action_name,
        success=success,
        approval=approval,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        message=message,
    ))


REQUEST_PRIVILEGED_HOST_ACTION_SCHEMA = {
    "name": "request_privileged_host_action",
    "description": (
        "Request one-shot approval to run a tightly-scoped allowlisted "
        "privileged host wrapper with validated arguments."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Allowlisted privileged host action name.",
            },
            "args": {
                "type": "array",
                "description": "Exact argument tokens to pass to the allowlisted wrapper.",
                "items": {"type": "string"},
            },
        },
        "required": ["action"],
    },
}


def _handle_request_privileged_host_action(args: dict[str, Any], **_kw) -> str:
    return request_privileged_host_action(
        action=args.get("action"),
        args=args.get("args"),
    )


registry.register(
    name="request_privileged_host_action",
    toolset="terminal",
    schema=REQUEST_PRIVILEGED_HOST_ACTION_SCHEMA,
    handler=_handle_request_privileged_host_action,
    emoji="🔐",
)
