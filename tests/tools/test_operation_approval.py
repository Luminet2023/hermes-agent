import os
import threading
import time

import pytest

import tools.approval as approval_module


@pytest.fixture(autouse=True)
def _reset_approval_state():
    approval_module._gateway_queues.clear()
    approval_module._gateway_notify_cbs.clear()
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()
    approval_module._pending.clear()
    for key in (
        "HERMES_YOLO_MODE",
        "HERMES_INTERACTIVE",
        "HERMES_GATEWAY_SESSION",
        "HERMES_EXEC_ASK",
        "HERMES_SESSION_KEY",
    ):
        os.environ.pop(key, None)
    yield
    approval_module._gateway_queues.clear()
    approval_module._gateway_notify_cbs.clear()
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()
    approval_module._pending.clear()
    for key in (
        "HERMES_YOLO_MODE",
        "HERMES_INTERACTIVE",
        "HERMES_GATEWAY_SESSION",
        "HERMES_EXEC_ASK",
        "HERMES_SESSION_KEY",
    ):
        os.environ.pop(key, None)


def test_request_operation_approval_blocks_and_resolves_once():
    session_key = "operation-approval-once"
    notified = []
    approval_module.register_gateway_notify(session_key, lambda data: notified.append(data))

    result_holder = {}

    def _run():
        token = approval_module.set_current_session_key(session_key)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_EXEC_ASK"] = "1"
        os.environ["HERMES_SESSION_KEY"] = session_key
        try:
            result_holder["result"] = approval_module.request_operation_approval(
                "switch task op-task to host/local execution",
                "Allow this task to leave Docker and run on the host.",
                title="Host Environment Approval",
            )
        finally:
            approval_module.reset_current_session_key(token)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not notified and time.time() < deadline:
        time.sleep(0.05)

    assert notified
    assert notified[0]["title"] == "Host Environment Approval"
    assert notified[0]["choices"] == ["once", "deny"]

    approval_module.resolve_gateway_approval(session_key, "once")
    thread.join(timeout=5)

    assert result_holder["result"]["approved"] is True
    assert result_holder["result"]["choice"] == "once"


def test_request_operation_approval_denied():
    session_key = "operation-approval-deny"
    approval_module.register_gateway_notify(session_key, lambda _data: None)

    result_holder = {}

    def _run():
        token = approval_module.set_current_session_key(session_key)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_EXEC_ASK"] = "1"
        os.environ["HERMES_SESSION_KEY"] = session_key
        try:
            result_holder["result"] = approval_module.request_operation_approval(
                "switch task op-task from docker to host/local execution",
                "Allow this task to leave Docker and run on the host.",
                title="Host Environment Approval",
            )
        finally:
            approval_module.reset_current_session_key(token)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not approval_module._gateway_queues.get(session_key) and time.time() < deadline:
        time.sleep(0.05)

    approval_module.resolve_gateway_approval(session_key, "deny")
    thread.join(timeout=5)

    assert result_holder["result"]["approved"] is False
    assert "denied" in result_holder["result"]["message"].lower()
