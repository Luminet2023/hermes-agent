import json

import pytest

import tools.code_execution_tool as code_execution_tool
import tools.file_tools as file_tools
import tools.host_access_tools as host_access_tools
import tools.task_env_state as task_env_state
import tools.terminal_tool as terminal_tool


@pytest.fixture(autouse=True)
def _reset_task_state():
    task_env_state._task_env_overrides.clear()
    file_tools.clear_file_ops_cache()
    terminal_tool._active_environments.clear()
    terminal_tool._last_activity.clear()
    terminal_tool._creation_locks.clear()
    yield
    task_env_state._task_env_overrides.clear()
    file_tools.clear_file_ops_cache()
    terminal_tool._active_environments.clear()
    terminal_tool._last_activity.clear()
    terminal_tool._creation_locks.clear()


def _approved_once(*_args, **_kwargs):
    return {"approved": True, "choice": "once"}


def test_request_host_env_switches_task_to_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)

    cleaned = []
    monkeypatch.setattr(
        host_access_tools,
        "_cleanup_task_environment",
        lambda task_id: cleaned.append(task_id),
    )

    result = json.loads(host_access_tools.request_host_env("task-switch"))

    assert result["success"] is True
    assert result["changed"] is True
    assert result["from_env_type"] == "docker"
    assert result["to_env_type"] == "local"
    assert cleaned == ["task-switch"]
    assert task_env_state.get_effective_env_config("task-switch")["env_type"] == "local"
    overrides = task_env_state.get_task_env_overrides("task-switch")
    assert overrides["previous_env_type"] == "docker"
    assert overrides["env_type"] == "local"


def test_request_host_env_noop_when_already_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")

    result = json.loads(host_access_tools.request_host_env("task-local"))

    assert result["success"] is True
    assert result["changed"] is False
    assert result["from_env_type"] == "local"
    assert result["to_env_type"] == "local"
    assert task_env_state.get_task_env_overrides("task-local") == {}


def test_restore_sandbox_env_restores_previous_backend_and_cwd(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)

    task_env_state.register_task_env_overrides("task-restore", {"cwd": "/workspace/project"})
    json.loads(host_access_tools.request_host_env("task-restore"))

    result = json.loads(host_access_tools.restore_sandbox_env("task-restore"))

    assert result["success"] is True
    assert result["to_env_type"] == "docker"
    assert result["restored_from_previous"] is True
    assert task_env_state.get_effective_env_config("task-restore")["env_type"] == "docker"
    overrides = task_env_state.get_task_env_overrides("task-restore")
    assert overrides == {"cwd": "/workspace/project"}


def test_restore_sandbox_env_noop_when_already_on_default_backend(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    result = json.loads(host_access_tools.restore_sandbox_env("task-default"))

    assert result["success"] is True
    assert result["changed"] is False
    assert result["to_env_type"] == "docker"
    assert task_env_state.get_task_env_overrides("task-default") == {}


def test_terminal_file_tools_and_execute_code_share_same_backend_after_switch(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True})

    created_env_types = []

    class _FakeEnv:
        def __init__(self, env_type: str):
            self.env_type = env_type
            self.cwd = "/tmp"

        def cleanup(self):
            return None

        def execute(self, _command, **_kwargs):
            return {"output": self.env_type, "returncode": 0}

    def _fake_create_environment(env_type, **_kwargs):
        created_env_types.append(env_type)
        return _FakeEnv(env_type)

    monkeypatch.setattr(terminal_tool, "_create_environment", _fake_create_environment)

    terminal_result_before = json.loads(
        terminal_tool.terminal_tool("echo backend", task_id="task-consistency")
    )
    assert terminal_result_before["output"] == "docker"

    file_tools._get_file_ops("task-consistency")

    remote_calls = []
    monkeypatch.setattr(
        code_execution_tool,
        "_execute_remote",
        lambda code, task_id, enabled_tools: remote_calls.append((code, task_id)) or "__REMOTE__",
    )
    assert code_execution_tool.execute_code("print('x')", task_id="task-consistency") == "__REMOTE__"

    host_access_tools.request_host_env("task-consistency")

    terminal_result_after = json.loads(
        terminal_tool.terminal_tool("echo backend", task_id="task-consistency")
    )
    assert terminal_result_after["output"] == "local"

    ops_local = file_tools._get_file_ops("task-consistency")
    assert ops_local.env.env_type == "local"

    def _raise_local_path(*_args, **_kwargs):
        raise RuntimeError("LOCAL_PATH_REACHED")

    monkeypatch.setattr(code_execution_tool, "generate_hermes_tools_module", _raise_local_path)
    execute_code_result = json.loads(
        code_execution_tool.execute_code("print('x')", task_id="task-consistency")
    )
    assert execute_code_result["status"] == "error"
    assert execute_code_result["error"] == "LOCAL_PATH_REACHED"

    assert created_env_types == ["docker", "local"]
    assert len(remote_calls) == 1


def test_file_tools_cache_invalidates_on_switch_and_restore(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    created_env_types = []

    class _FakeEnv:
        def __init__(self, env_type: str):
            self.env_type = env_type
            self.cwd = "/tmp"

        def cleanup(self):
            return None

    def _fake_create_environment(env_type, **_kwargs):
        created_env_types.append(env_type)
        return _FakeEnv(env_type)

    monkeypatch.setattr(terminal_tool, "_create_environment", _fake_create_environment)

    ops_docker = file_tools._get_file_ops("task-file-cache")
    host_access_tools.request_host_env("task-file-cache")
    ops_local = file_tools._get_file_ops("task-file-cache")
    host_access_tools.restore_sandbox_env("task-file-cache")
    ops_docker_restored = file_tools._get_file_ops("task-file-cache")

    assert created_env_types == ["docker", "local", "docker"]
    assert ops_local is not ops_docker
    assert ops_docker_restored is not ops_local


def test_request_host_env_uses_strict_cleanup_for_dynamic_mounts(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)

    cleanup_calls = []
    monkeypatch.setattr(
        terminal_tool,
        "cleanup_vm",
        lambda task_id, strict_mount_cleanup=False: cleanup_calls.append((task_id, strict_mount_cleanup)),
    )
    monkeypatch.setattr(file_tools, "clear_file_ops_cache", lambda *_args, **_kwargs: None)

    result = json.loads(host_access_tools.request_host_env("task-strict-cleanup"))

    assert result["success"] is True
    assert cleanup_calls == [("task-strict-cleanup", True)]


def test_request_host_env_cleanup_failure_keeps_overrides_and_cache(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", _approved_once)
    monkeypatch.setattr(
        terminal_tool,
        "cleanup_vm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mount cleanup failed")),
    )

    with file_tools._file_ops_lock:
        sentinel = object()
        file_tools._file_ops_cache["task-cleanup-fail"] = sentinel

    result = json.loads(host_access_tools.request_host_env("task-cleanup-fail"))

    assert result["success"] is False
    assert result["changed"] is False
    assert "mount cleanup failed" in result["message"]
    assert task_env_state.get_task_env_overrides("task-cleanup-fail") == {}
    with file_tools._file_ops_lock:
        assert file_tools._file_ops_cache["task-cleanup-fail"] is sentinel


@pytest.mark.parametrize(
    "approval_result",
    [
        {"approved": False, "message": "BLOCKED: Request denied by user. Do NOT retry.", "choice": "deny"},
        {"approved": False, "message": "BLOCKED: Approval request timed out. Do NOT retry."},
    ],
)
def test_request_host_env_denied_or_timed_out_keeps_overrides_and_cache(monkeypatch, approval_result):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(host_access_tools, "request_operation_approval", lambda *_args, **_kwargs: approval_result)

    task_env_state.register_task_env_overrides("task-denied", {"cwd": "/workspace/original"})
    with file_tools._file_ops_lock:
        sentinel = object()
        file_tools._file_ops_cache["task-denied"] = sentinel

    result = json.loads(host_access_tools.request_host_env("task-denied"))

    assert result["success"] is False
    assert result["changed"] is False
    assert task_env_state.get_task_env_overrides("task-denied") == {"cwd": "/workspace/original"}
    with file_tools._file_ops_lock:
        assert file_tools._file_ops_cache["task-denied"] is sentinel
