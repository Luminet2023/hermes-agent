"""Task-scoped terminal environment overrides and resolution helpers."""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict


_task_env_lock = threading.RLock()
_task_env_overrides: Dict[str, Dict[str, Any]] = {}
_DOCKER_MOUNT_RUNTIME_KEY = "docker_mount_runtime"

_IMAGE_KEY_BY_ENV = {
    "docker": "docker_image",
    "singularity": "singularity_image",
    "modal": "modal_image",
    "daytona": "daytona_image",
}

_CONTAINER_ENV_TYPES = frozenset({"docker", "singularity", "modal", "daytona"})


def register_task_env_overrides(task_id: str, overrides: Dict[str, Any]):
    """Merge task-scoped environment overrides for a task."""
    if not task_id:
        task_id = "default"
    updates = dict(overrides or {})
    with _task_env_lock:
        current = dict(_task_env_overrides.get(task_id, {}))
        current.update(updates)
        if current:
            _task_env_overrides[task_id] = current
        else:
            _task_env_overrides.pop(task_id, None)


def get_task_env_overrides(task_id: str) -> Dict[str, Any]:
    """Return a copy of the current task-scoped overrides."""
    if not task_id:
        task_id = "default"
    with _task_env_lock:
        return dict(_task_env_overrides.get(task_id, {}))


def clear_task_env_overrides(task_id: str):
    """Clear all task-scoped overrides for a task."""
    if not task_id:
        task_id = "default"
    with _task_env_lock:
        _task_env_overrides.pop(task_id, None)


def remove_task_env_override_keys(task_id: str, *keys: str) -> Dict[str, Any]:
    """Remove specific override keys and return the updated override state."""
    if not task_id:
        task_id = "default"
    with _task_env_lock:
        current = dict(_task_env_overrides.get(task_id, {}))
        for key in keys:
            current.pop(key, None)
        if current:
            _task_env_overrides[task_id] = current
        else:
            _task_env_overrides.pop(task_id, None)
        return dict(current)


def get_task_docker_mount_runtime(task_id: str) -> Dict[str, Any] | None:
    """Return a deep copy of the current Docker mount runtime for *task_id*."""
    task_id = task_id or "default"
    with _task_env_lock:
        runtime = _task_env_overrides.get(task_id, {}).get(_DOCKER_MOUNT_RUNTIME_KEY)
        if runtime is None:
            return None
        return copy.deepcopy(runtime)


def set_task_docker_mount_runtime(task_id: str, runtime: Dict[str, Any]) -> None:
    """Persist Docker dynamic mount runtime metadata for *task_id*."""
    task_id = task_id or "default"
    with _task_env_lock:
        current = dict(_task_env_overrides.get(task_id, {}))
        current[_DOCKER_MOUNT_RUNTIME_KEY] = copy.deepcopy(runtime)
        _task_env_overrides[task_id] = current


def update_task_docker_mount_runtime(task_id: str, updates: Dict[str, Any]) -> Dict[str, Any] | None:
    """Merge top-level Docker mount runtime updates for *task_id* and return the new runtime."""
    task_id = task_id or "default"
    with _task_env_lock:
        current = dict(_task_env_overrides.get(task_id, {}))
        runtime = current.get(_DOCKER_MOUNT_RUNTIME_KEY)
        if runtime is None:
            return None
        merged_runtime = copy.deepcopy(runtime)
        merged_runtime.update(copy.deepcopy(updates or {}))
        current[_DOCKER_MOUNT_RUNTIME_KEY] = merged_runtime
        _task_env_overrides[task_id] = current
        return copy.deepcopy(merged_runtime)


def clear_task_docker_mount_runtime(task_id: str) -> None:
    """Remove Docker dynamic mount runtime metadata for *task_id*."""
    remove_task_env_override_keys(task_id or "default", _DOCKER_MOUNT_RUNTIME_KEY)


def _load_base_config_for_env_type(env_type: str | None = None) -> Dict[str, Any]:
    """Load terminal config, optionally recomputed for a different env type."""
    from tools.terminal_tool import _build_env_config, _get_env_config

    if env_type:
        return _build_env_config(env_type)
    return _get_env_config()


def get_effective_env_config(task_id: str = "default") -> Dict[str, Any]:
    """Return the terminal config after applying task-scoped overrides."""
    task_id = task_id or "default"
    overrides = get_task_env_overrides(task_id)
    base_config = _load_base_config_for_env_type()
    override_env_type = str(overrides.get("env_type") or "").strip().lower() or None
    if override_env_type and override_env_type != base_config.get("env_type"):
        config = _load_base_config_for_env_type(override_env_type)
    else:
        config = dict(base_config)

    if override_env_type:
        config["env_type"] = override_env_type

    if "cwd" in overrides and overrides.get("cwd"):
        config["cwd"] = overrides["cwd"]

    for image_key in _IMAGE_KEY_BY_ENV.values():
        value = overrides.get(image_key)
        if value:
            config[image_key] = value

    return config


def build_environment_request(task_id: str = "default", *,
                              timeout: int | None = None) -> Dict[str, Any]:
    """Build the normalized environment creation request for a task."""
    config = get_effective_env_config(task_id)
    env_type = config["env_type"]
    image_key = _IMAGE_KEY_BY_ENV.get(env_type)
    image = config.get(image_key, "") if image_key else ""
    effective_timeout = timeout if timeout is not None else config["timeout"]

    container_config = None
    if env_type in _CONTAINER_ENV_TYPES:
        container_config = {
            "container_cpu": config.get("container_cpu", 1),
            "container_memory": config.get("container_memory", 5120),
            "container_disk": config.get("container_disk", 51200),
            "container_persistent": config.get("container_persistent", True),
            "modal_mode": config.get("modal_mode", "auto"),
            "docker_volumes": config.get("docker_volumes", []),
            "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
            "docker_forward_env": config.get("docker_forward_env", []),
            "docker_env": config.get("docker_env", {}),
        }

    ssh_config = None
    if env_type == "ssh":
        ssh_config = {
            "host": config.get("ssh_host", ""),
            "user": config.get("ssh_user", ""),
            "port": config.get("ssh_port", 22),
            "key": config.get("ssh_key", ""),
            "persistent": config.get("ssh_persistent", False),
        }

    local_config = None
    if env_type == "local":
        local_config = {
            "persistent": config.get("local_persistent", False),
        }

    return {
        "config": config,
        "env_type": env_type,
        "image": image,
        "cwd": config["cwd"],
        "timeout": effective_timeout,
        "container_config": container_config,
        "ssh_config": ssh_config,
        "local_config": local_config,
        "host_cwd": config.get("host_cwd"),
    }
