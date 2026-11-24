"""Folder/site approval fields and denial reasons survive both serve response paths."""
import pytest

from tests.tui_gateway.test_protocol import server as _server

server = _server


@pytest.mark.parametrize("kind, target", [
    ("access", {"path": "/srv/Project  Files", "access": "read_write", "risk": "Write risk"}),
    ("network", {"host": "downloads.example", "label": "Downloads", "risk": "Network risk"}),
])
def test_approval_response_forwards_only_folder_and_site_denial_reasons(server, monkeypatch, kind, target):
    from tools import approval

    server._sessions["ui-1"] = {"session_key": "agent-1", "history": []}
    data = {"request_id": "req-card", "kind": kind, **target}
    monkeypatch.setattr(approval, "list_gateway_approvals", lambda key: [data])
    calls = []
    monkeypatch.setattr(approval, "resolve_gateway_approval",
                        lambda key, choice, **kwargs: calls.append((key, choice, kwargs)) or 1)

    response = server.handle_request({"id": "r-card", "method": "approval.respond", "params": {
        "session_id": "ui-1", "request_id": "req-card", "choice": "deny", "reason": "  Not allowed  "}})

    assert response["result"] == {"resolved": 1}
    assert calls == [("agent-1", "deny", {"resolve_all": False, "request_id": "req-card", "reason": "Not allowed"})]


def test_approval_response_command_reason_does_not_widen_command_contract(server, monkeypatch):
    from tools import approval

    server._sessions["ui-1"] = {"session_key": "agent-1", "history": []}
    monkeypatch.setattr(approval, "list_gateway_approvals",
                        lambda key: [{"request_id": "req-command", "kind": "command", "command": "rm -rf build"}])
    calls = []
    monkeypatch.setattr(approval, "resolve_gateway_approval",
                        lambda key, choice, **kwargs: calls.append((key, choice, kwargs)) or 1)

    response = server.handle_request({"id": "r-command", "method": "approval.respond", "params": {
        "session_id": "ui-1", "request_id": "req-command", "choice": "deny", "reason": "ignore this"}})

    assert response["result"] == {"resolved": 1}
    assert calls == [("agent-1", "deny", {"resolve_all": False, "request_id": "req-command"})]


@pytest.mark.parametrize("kind, target", [
    ("access", {"path": "/srv/Project  Files", "access": "read", "risk": "Read risk"}),
    ("network", {"host": "downloads.example", "label": "Downloads", "risk": "Network risk"}),
])
def test_live_approval_answer_forwards_folder_and_site_reason(server, monkeypatch, kind, target):
    from tools import approval
    from tui_gateway import server_requests

    server._sessions["ui-card"] = {"session_key": "agent-card", "history": []}
    answers = []
    monkeypatch.setattr(server_requests, "send_async",
                        lambda method, sid, payload, on_result: answers.append((payload, on_result)) or (lambda _why: None))
    calls = []
    monkeypatch.setattr(approval, "resolve_gateway_approval",
                        lambda key, choice, **kwargs: calls.append((key, choice, kwargs)) or 1)

    server._emit_approval_request("ui-card", {"request_id": "live-card", "kind": kind, **target})
    answers[0][1]({"choice": "deny", "reason": "  Not allowed  "})

    assert answers[0][0]["kind"] == kind
    assert {field: answers[0][0][field] for field in target} == target
    assert calls == [("agent-card", "deny", {"resolve_all": False, "request_id": "live-card", "reason": "Not allowed"})]


def test_live_command_answer_never_gains_a_reason(server, monkeypatch):
    from tools import approval
    from tui_gateway import server_requests

    server._sessions["ui-command"] = {"session_key": "agent-command", "history": []}
    answers = []
    monkeypatch.setattr(server_requests, "send_async",
                        lambda method, sid, payload, on_result: answers.append(on_result) or (lambda _why: None))
    calls = []
    monkeypatch.setattr(approval, "resolve_gateway_approval",
                        lambda key, choice, **kwargs: calls.append((key, choice, kwargs)) or 1)

    server._emit_approval_request("ui-command", {"request_id": "live-command", "kind": "command",
                                                "command": "rm -rf build"})
    answers[0]({"choice": "deny", "reason": "not for commands"})

    assert calls == [("agent-command", "deny", {"resolve_all": False, "request_id": "live-command"})]
