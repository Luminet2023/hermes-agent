import json
import stat
import subprocess
from unittest.mock import MagicMock

import pytest

import tools.privileged_host_action_tool as privileged_tool
from hermes_cli.config import DEFAULT_CONFIG
from toolsets import resolve_toolset


def _config_for_action(
    *,
    enabled: bool = True,
    action_name: str = "restart_service",
    wrapper: str = "/usr/local/libexec/restart-service",
    allowed_args: list[str] | None = None,
    timeout: int = 45,
) -> dict:
    return {
        "terminal": {
            "privileged_host_actions_enabled": enabled,
            "privileged_host_actions": {
                action_name: {
                    "wrapper": wrapper,
                    "allowed_args": list(allowed_args or []),
                    "timeout": timeout,
                },
            },
        },
    }


def _approved_once(*_args, **_kwargs):
    return {"approved": True, "choice": "once"}


def test_default_config_disables_privileged_host_actions():
    terminal_cfg = DEFAULT_CONFIG["terminal"]
    assert terminal_cfg["privileged_host_actions_enabled"] is False
    assert terminal_cfg["privileged_host_actions"] == {}


def test_request_privileged_host_action_runs_allowlisted_wrapper(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(allowed_args=["nginx"]))
    monkeypatch.setattr(privileged_tool, "request_operation_approval", MagicMock(side_effect=_approved_once))
    monkeypatch.setattr(
        privileged_tool,
        "_validate_wrapper_path",
        MagicMock(return_value="/usr/local/libexec/restart-service"),
    )
    mock_run = MagicMock(return_value=subprocess.CompletedProcess(
        ["/usr/local/libexec/restart-service", "nginx"],
        0,
        stdout="done\n",
        stderr="",
    ))
    monkeypatch.setattr(privileged_tool.subprocess, "run", mock_run)

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", ["nginx"]))

    assert result["success"] is True
    assert result["action"] == "restart_service"
    assert result["approval"]["choice"] == "once"
    assert result["exit_code"] == 0
    assert result["stdout"] == "done\n"
    assert result["stderr"] == ""
    privileged_tool.request_operation_approval.assert_called_once_with(
        command="run privileged host action restart_service nginx",
        description=(
            "Allow Hermes to invoke the allowlisted privileged host action "
            "'restart_service'. This is a one-shot approval and does not grant shell access."
        ),
        title="Privileged Host Action Approval",
        choices=["once", "deny"],
    )
    mock_run.assert_called_once_with(
        ["/usr/local/libexec/restart-service", "nginx"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def test_request_privileged_host_action_rejects_unallowlisted_action(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action())
    approval_mock = MagicMock(side_effect=_approved_once)
    run_mock = MagicMock()
    monkeypatch.setattr(privileged_tool, "request_operation_approval", approval_mock)
    monkeypatch.setattr(privileged_tool.subprocess, "run", run_mock)

    result = json.loads(privileged_tool.request_privileged_host_action("unknown_action", []))

    assert result["success"] is False
    assert "not allowlisted" in result["message"].lower()
    approval_mock.assert_not_called()
    run_mock.assert_not_called()


def test_request_privileged_host_action_rejects_non_allowlisted_args(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(allowed_args=["nginx"]))
    approval_mock = MagicMock(side_effect=_approved_once)
    run_mock = MagicMock()
    monkeypatch.setattr(privileged_tool, "request_operation_approval", approval_mock)
    monkeypatch.setattr(
        privileged_tool,
        "_validate_wrapper_path",
        MagicMock(return_value="/usr/local/libexec/restart-service"),
    )
    monkeypatch.setattr(privileged_tool.subprocess, "run", run_mock)

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", ["sshd"]))

    assert result["success"] is False
    assert "not allowlisted" in result["message"].lower()
    approval_mock.assert_not_called()
    run_mock.assert_not_called()


@pytest.mark.parametrize(
    "approval_result, expected_message",
    [
        (
            {"approved": False, "message": "BLOCKED: Request denied by user. Do NOT retry.", "choice": "deny"},
            "denied",
        ),
        (
            {"approved": False, "message": "BLOCKED: Approval request timed out. Do NOT retry."},
            "timed out",
        ),
    ],
)
def test_request_privileged_host_action_does_not_execute_when_approval_fails(
    monkeypatch,
    approval_result,
    expected_message,
):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(allowed_args=["nginx"]))
    monkeypatch.setattr(privileged_tool, "request_operation_approval", lambda *_args, **_kwargs: approval_result)
    monkeypatch.setattr(
        privileged_tool,
        "_validate_wrapper_path",
        MagicMock(return_value="/usr/local/libexec/restart-service"),
    )
    run_mock = MagicMock()
    monkeypatch.setattr(privileged_tool.subprocess, "run", run_mock)

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", ["nginx"]))

    assert result["success"] is False
    assert expected_message in result["message"].lower()
    assert result["approval"] == approval_result
    run_mock.assert_not_called()


def test_request_privileged_host_action_passes_through_wrapper_output(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(allowed_args=["nginx"]))
    monkeypatch.setattr(privileged_tool, "request_operation_approval", _approved_once)
    monkeypatch.setattr(
        privileged_tool,
        "_validate_wrapper_path",
        MagicMock(return_value="/usr/local/libexec/restart-service"),
    )
    monkeypatch.setattr(
        privileged_tool.subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess(
            ["/usr/local/libexec/restart-service", "nginx"],
            7,
            stdout="wrapper-out\n",
            stderr="wrapper-err\n",
        )),
    )

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", ["nginx"]))

    assert result["success"] is False
    assert result["exit_code"] == 7
    assert result["stdout"] == "wrapper-out\n"
    assert result["stderr"] == "wrapper-err\n"


def test_request_privileged_host_action_handles_wrapper_timeout(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(allowed_args=["nginx"], timeout=12))
    monkeypatch.setattr(privileged_tool, "request_operation_approval", _approved_once)
    monkeypatch.setattr(
        privileged_tool,
        "_validate_wrapper_path",
        MagicMock(return_value="/usr/local/libexec/restart-service"),
    )
    monkeypatch.setattr(
        privileged_tool.subprocess,
        "run",
        MagicMock(side_effect=subprocess.TimeoutExpired(
            cmd=["/usr/local/libexec/restart-service", "nginx"],
            timeout=12,
            output="partial-out",
            stderr="partial-err",
        )),
    )

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", ["nginx"]))

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["stdout"] == "partial-out"
    assert result["stderr"] == "partial-err"
    assert "timed out" in result["message"].lower()


def test_request_privileged_host_action_rejects_when_disabled(monkeypatch):
    monkeypatch.setattr(privileged_tool, "load_config", lambda: _config_for_action(enabled=False))
    approval_mock = MagicMock(side_effect=_approved_once)
    run_mock = MagicMock()
    monkeypatch.setattr(privileged_tool, "request_operation_approval", approval_mock)
    monkeypatch.setattr(privileged_tool.subprocess, "run", run_mock)

    result = json.loads(privileged_tool.request_privileged_host_action("restart_service", []))

    assert result["success"] is False
    assert "disabled" in result["message"].lower()
    approval_mock.assert_not_called()
    run_mock.assert_not_called()


@pytest.mark.parametrize(
    "wrapper_path, fake_mode, fake_uid, executable, expected_message",
    [
        ("relative/wrapper", stat.S_IFREG | 0o755, 0, True, "absolute path"),
        ("/usr/local/libexec/wrapper", stat.S_IFREG | 0o755, 1000, True, "owned by root"),
        ("/usr/local/libexec/wrapper", stat.S_IFREG | 0o775, 0, True, "group- or world-writable"),
        ("/usr/local/libexec/wrapper", stat.S_IFREG | 0o644, 0, False, "executable"),
    ],
)
def test_validate_wrapper_path_enforces_security_invariants(
    monkeypatch,
    wrapper_path,
    fake_mode,
    fake_uid,
    executable,
    expected_message,
):
    class _FakeStat:
        st_mode = fake_mode
        st_uid = fake_uid

    monkeypatch.setattr(privileged_tool.os.path, "isabs", lambda path: path.startswith("/"))
    monkeypatch.setattr(privileged_tool.os, "stat", lambda _path: _FakeStat())
    monkeypatch.setattr(privileged_tool.os, "access", lambda _path, _mode: executable)

    with pytest.raises((ValueError, PermissionError), match=expected_message):
        privileged_tool._validate_wrapper_path(wrapper_path)


def test_local_only_tool_exposure():
    assert "request_privileged_host_action" in resolve_toolset("terminal")
    assert "request_privileged_host_action" in resolve_toolset("hermes-cli")
    assert "request_privileged_host_action" in resolve_toolset("hermes-acp")
    assert "request_privileged_host_action" not in resolve_toolset("hermes-api-server")
    assert "request_privileged_host_action" not in resolve_toolset("hermes-telegram")
    assert "request_privileged_host_action" not in resolve_toolset("hermes-discord")
    assert "request_privileged_host_action" not in resolve_toolset("hermes-slack")
    assert "request_privileged_host_action" not in resolve_toolset("hermes-qqbot")
