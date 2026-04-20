import subprocess
from pathlib import Path

import pytest

from tools.environments import docker_mounts
from tools.task_env_state import (
    _task_env_overrides,
    get_task_docker_mount_runtime,
)


@pytest.fixture(autouse=True)
def _reset_task_state():
    _task_env_overrides.clear()
    yield
    _task_env_overrides.clear()


def _mount_config(
    tmp_path,
    *,
    enabled=True,
    mode="host-nsenter",
    helper_image="",
    helper_prepare_command="",
):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir(exist_ok=True)
    mount_root = tmp_path / "docker-mounts"
    return {
        "terminal": {
            "docker_dynamic_mounts_enabled": enabled,
            "docker_dynamic_mounts_root": str(mount_root),
            "docker_dynamic_mounts_allowed_roots": [str(allowed_root)],
            "docker_mount_helper": {
                "mode": mode,
                "wrapper": "/usr/local/bin/docker-mount-helper",
                "helper_image": helper_image,
                "helper_prepare_command": helper_prepare_command,
                "timeout": 30,
            },
        }
    }


def test_prepare_task_mount_runtime_uses_host_nsenter_helper(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(docker_mounts, "load_config", lambda: _mount_config(tmp_path))
    monkeypatch.setattr(docker_mounts, "_validate_wrapper_path", lambda wrapper: str(wrapper))
    monkeypatch.setattr(
        docker_mounts.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    runtime = docker_mounts.prepare_task_mount_runtime("task-prepare")

    assert runtime["helper_mode"] == "host-nsenter"
    assert runtime["hub_container_path"] == "/__hermes_host_mounts"
    assert calls
    assert calls[0][:2] == ["/usr/local/bin/docker-mount-helper", "prepare-hub"]
    assert "--mode" in calls[0]
    assert "host-nsenter" in calls[0]
    assert "--hub-path" in calls[0]


def test_mount_host_path_supports_privileged_helper_container_mode(monkeypatch, tmp_path):
    calls = []
    config = _mount_config(
        tmp_path,
        mode="privileged-helper-container",
        helper_image="alpine:3.20",
        helper_prepare_command="apk add util-linux",
    )
    allowed_root = Path(config["terminal"]["docker_dynamic_mounts_allowed_roots"][0])
    source_dir = allowed_root / "project"
    source_dir.mkdir()

    monkeypatch.setattr(docker_mounts, "load_config", lambda: config)
    monkeypatch.setattr(docker_mounts, "_validate_wrapper_path", lambda wrapper: str(wrapper))
    monkeypatch.setattr(
        docker_mounts.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    docker_mounts.prepare_task_mount_runtime("task-helper")
    result = docker_mounts.mount_host_path("task-helper", str(source_dir), readonly=False)

    assert result["helper_mode"] == "privileged-helper-container"
    assert result["mount_id"] == "mount-0001"
    assert result["container_path"] == "/__hermes_host_mounts/mount-0001"
    assert [cmd[1] for cmd in calls] == ["prepare-hub", "bind"]
    bind_cmd = calls[1]
    assert "--mode" in bind_cmd
    assert "privileged-helper-container" in bind_cmd
    assert "--helper-image" in bind_cmd
    assert "alpine:3.20" in bind_cmd
    assert "--helper-prepare-command" in bind_cmd
    assert "apk add util-linux" in bind_cmd


def test_mount_host_path_rolls_back_when_readonly_remount_fails(monkeypatch, tmp_path):
    calls = []
    config = _mount_config(tmp_path)
    allowed_root = Path(config["terminal"]["docker_dynamic_mounts_allowed_roots"][0])
    source_dir = allowed_root / "docs"
    source_dir.mkdir()

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        subcommand = cmd[1]
        if subcommand == "remount-readonly":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="readonly failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_mounts, "load_config", lambda: config)
    monkeypatch.setattr(docker_mounts, "_validate_wrapper_path", lambda wrapper: str(wrapper))
    monkeypatch.setattr(docker_mounts.subprocess, "run", _run)

    docker_mounts.prepare_task_mount_runtime("task-readonly")
    docker_mounts.register_task_container_id("task-readonly", "container-123")

    with pytest.raises(docker_mounts.DockerMountError, match="readonly failed"):
        docker_mounts.mount_host_path("task-readonly", str(source_dir), readonly=True)

    runtime = get_task_docker_mount_runtime("task-readonly")
    assert runtime is not None
    assert runtime["mounts"] == {}
    assert runtime["next_mount_index"] == 1
    assert [cmd[1] for cmd in calls] == [
        "prepare-hub",
        "bind",
        "remount-readonly",
        "unbind",
    ]
    remount_cmd = calls[2]
    assert "--target-container-id" in remount_cmd
    assert "container-123" in remount_cmd
    assert "--container-mount-path" in remount_cmd
    assert "/__hermes_host_mounts/mount-0001" in remount_cmd


def test_readonly_mount_requires_active_container_id(monkeypatch, tmp_path):
    calls = []
    config = _mount_config(tmp_path)
    allowed_root = Path(config["terminal"]["docker_dynamic_mounts_allowed_roots"][0])
    source_dir = allowed_root / "readonly"
    source_dir.mkdir()

    monkeypatch.setattr(docker_mounts, "load_config", lambda: config)
    monkeypatch.setattr(docker_mounts, "_validate_wrapper_path", lambda wrapper: str(wrapper))
    monkeypatch.setattr(
        docker_mounts.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    docker_mounts.prepare_task_mount_runtime("task-no-container-id")

    with pytest.raises(docker_mounts.DockerMountError, match="container ID"):
        docker_mounts.mount_host_path("task-no-container-id", str(source_dir), readonly=True)

    assert [cmd[1] for cmd in calls] == ["prepare-hub"]


def test_cleanup_task_mounts_invokes_cleanup_and_clears_state(monkeypatch, tmp_path):
    calls = []
    config = _mount_config(tmp_path)

    monkeypatch.setattr(docker_mounts, "load_config", lambda: config)
    monkeypatch.setattr(docker_mounts, "_validate_wrapper_path", lambda wrapper: str(wrapper))
    monkeypatch.setattr(
        docker_mounts.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    runtime = docker_mounts.prepare_task_mount_runtime("task-cleanup")
    task_root = Path(runtime["task_mount_root"])
    assert task_root.exists()

    assert docker_mounts.cleanup_task_mounts("task-cleanup", strict=True) is True
    assert get_task_docker_mount_runtime("task-cleanup") is None
    assert not task_root.exists()
    assert [cmd[1] for cmd in calls] == ["prepare-hub", "cleanup-task"]
