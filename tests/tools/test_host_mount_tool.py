import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.host_mount_tool as host_mount_tool
import tools.task_env_state as task_env_state
import tools.terminal_tool as terminal_tool
from tools.environments.docker import DockerEnvironment


@pytest.fixture(autouse=True)
def _reset_task_state():
    task_env_state._task_env_overrides.clear()
    terminal_tool._active_environments.clear()
    terminal_tool._last_activity.clear()
    yield
    task_env_state._task_env_overrides.clear()
    terminal_tool._active_environments.clear()
    terminal_tool._last_activity.clear()


def _settings(*, enabled=True, mode="host-nsenter"):
    return SimpleNamespace(enabled=enabled, helper=SimpleNamespace(mode=mode))


def _attach_active_docker_env(task_id: str):
    env = DockerEnvironment.__new__(DockerEnvironment)
    terminal_tool._active_environments[task_id] = env
    return env


def _approved_once(*_args, **_kwargs):
    return {"approved": True, "choice": "once"}


def test_request_host_mount_returns_metadata_after_approval(monkeypatch, tmp_path):
    task_id = "task-mount"
    source_dir = tmp_path / "allowed"
    source_dir.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    _attach_active_docker_env(task_id)
    task_env_state.set_task_docker_mount_runtime(task_id, {"helper_mode": "host-nsenter", "mounts": {}})

    approval_mock = MagicMock(side_effect=_approved_once)
    monkeypatch.setattr(host_mount_tool, "load_docker_mount_settings", lambda: _settings())
    monkeypatch.setattr(host_mount_tool, "validate_requested_host_path", lambda path: source_dir)
    monkeypatch.setattr(host_mount_tool, "request_operation_approval", approval_mock)
    monkeypatch.setattr(
        host_mount_tool,
        "mount_host_path",
        lambda _task_id, _path, readonly=True: {
            "host_path": str(source_dir),
            "readonly": readonly,
            "mount_id": "mount-0001",
            "container_path": "/__hermes_host_mounts/mount-0001",
            "helper_mode": "host-nsenter",
            "changed": True,
        },
    )

    result = json.loads(host_mount_tool.request_host_mount(str(source_dir), task_id=task_id))

    assert result == {
        "success": True,
        "task_id": task_id,
        "approval": {"approved": True, "choice": "once"},
        "host_path": str(source_dir),
        "readonly": True,
        "mount_id": "mount-0001",
        "container_path": "/__hermes_host_mounts/mount-0001",
        "helper_mode": "host-nsenter",
        "changed": True,
    }
    approval_mock.assert_called_once_with(
        command=f"mount host path {source_dir} into Docker sandbox for task {task_id} (readonly)",
        description=(
            f"Allow Hermes to expose the allowlisted host path '{source_dir}' to the current "
            "Docker sandbox as a read-only bind mount. This is a one-shot approval."
        ),
        title="Host Mount Approval",
        choices=["once", "deny"],
    )


def test_request_host_mount_noop_when_path_already_mounted(monkeypatch, tmp_path):
    task_id = "task-noop"
    source_dir = tmp_path / "allowed"
    source_dir.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    _attach_active_docker_env(task_id)
    task_env_state.set_task_docker_mount_runtime(
        task_id,
        {
            "helper_mode": "host-nsenter",
            "mounts": {
                "mount-0001": {
                    "resolved_host_path": str(source_dir.resolve()),
                    "readonly": True,
                    "container_path": "/__hermes_host_mounts/mount-0001",
                }
            },
        },
    )

    approval_mock = MagicMock()
    monkeypatch.setattr(host_mount_tool, "load_docker_mount_settings", lambda: _settings())
    monkeypatch.setattr(host_mount_tool, "validate_requested_host_path", lambda path: source_dir.resolve())
    monkeypatch.setattr(host_mount_tool, "request_operation_approval", approval_mock)

    result = json.loads(host_mount_tool.request_host_mount(str(source_dir), task_id=task_id))

    assert result["success"] is True
    assert result["changed"] is False
    assert result["mount_id"] == "mount-0001"
    assert result["approval"]["approved"] is True
    approval_mock.assert_not_called()


def test_request_host_mount_feature_disabled_rejects(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_mount_tool, "load_docker_mount_settings", lambda: _settings(enabled=False))
    result = json.loads(
        host_mount_tool.request_host_mount(
            "/tmp/blocked",
            task_id="task-disabled",
        )
    )

    assert result["success"] is False
    assert result["changed"] is False
    assert "disabled" in result["message"].lower()


def test_request_host_mount_local_backend_is_noop_without_approval(monkeypatch, tmp_path):
    source_dir = tmp_path / "local-ok"
    source_dir.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    approval_mock = MagicMock()
    monkeypatch.setattr(host_mount_tool, "_validate_local_host_path", lambda path: source_dir.resolve())
    monkeypatch.setattr(host_mount_tool, "request_operation_approval", approval_mock)

    result = json.loads(
        host_mount_tool.request_host_mount(
            str(source_dir),
            task_id="task-local",
        )
    )

    assert result["success"] is True
    assert result["approval"] == {"required": False, "approved": True, "choice": "once"}
    assert result["mount_id"] is None
    assert result["container_path"] == str(source_dir.resolve())
    assert result["helper_mode"] == "local-direct"
    assert result["changed"] is False
    approval_mock.assert_not_called()


def test_request_host_mount_local_backend_rejects_critical_paths(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    approval_mock = MagicMock()
    monkeypatch.setattr(
        host_mount_tool,
        "_validate_local_host_path",
        MagicMock(side_effect=host_mount_tool.DockerMountError("Refusing to expose critical host path in local mode: /etc")),
    )
    monkeypatch.setattr(host_mount_tool, "request_operation_approval", approval_mock)

    result = json.loads(
        host_mount_tool.request_host_mount(
            "/etc",
            task_id="task-local",
        )
    )

    assert result["success"] is False
    assert result["helper_mode"] == "local-direct"
    assert "critical host path" in result["message"]
    approval_mock.assert_not_called()


def test_request_host_mount_rejects_non_docker_non_local_backend(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setattr(host_mount_tool, "load_docker_mount_settings", lambda: _settings(enabled=True))

    result = json.loads(
        host_mount_tool.request_host_mount(
            "/tmp/blocked",
            task_id="task-ssh",
        )
    )

    assert result["success"] is False
    assert "only supports docker sandboxes or local/host tasks" in result["message"].lower()


@pytest.mark.parametrize(
    "approval_result",
    [
        {"approved": False, "message": "BLOCKED: Request denied by user. Do NOT retry.", "choice": "deny"},
        {"approved": False, "message": "BLOCKED: Approval request timed out. Do NOT retry."},
    ],
)
def test_request_host_mount_deny_or_timeout_has_no_side_effects(monkeypatch, tmp_path, approval_result):
    task_id = "task-denied"
    source_dir = tmp_path / "allowed"
    source_dir.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    _attach_active_docker_env(task_id)
    task_env_state.set_task_docker_mount_runtime(task_id, {"helper_mode": "host-nsenter", "mounts": {}})

    mount_mock = MagicMock()
    monkeypatch.setattr(host_mount_tool, "load_docker_mount_settings", lambda: _settings(enabled=True))
    monkeypatch.setattr(host_mount_tool, "validate_requested_host_path", lambda path: source_dir.resolve())
    monkeypatch.setattr(host_mount_tool, "request_operation_approval", lambda *_args, **_kwargs: approval_result)
    monkeypatch.setattr(host_mount_tool, "mount_host_path", mount_mock)

    result = json.loads(host_mount_tool.request_host_mount(str(source_dir), task_id=task_id))

    assert result["success"] is False
    assert result["changed"] is False
    assert task_env_state.get_task_docker_mount_runtime(task_id)["mounts"] == {}
    mount_mock.assert_not_called()
