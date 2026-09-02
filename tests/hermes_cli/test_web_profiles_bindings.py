"""GET /api/profiles/bindings: per-profile summary of static routes + dynamic /agent use bindings."""

from __future__ import annotations

import pytest

from gateway.source_agent_binding import SourceAgentBindingStore


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_bindings_route_is_not_shadowed_and_groups_dynamic_bindings_without_leaking_webhooks():
    store = SourceAgentBindingStore()
    try:
        store.set_binding("source:dingtalk:group:cid:abc==", "worker",
                          fallback_extra={"session_webhook": "https://oapi.example/robot/send?token=SECRET"},
                          actor_user_name="Alice")
        store.set_binding("source:telegram:dm:42", "worker")
    finally:
        store.close()

    response = _client().get("/api/profiles/bindings")

    # A /api/profiles/{name} route registered first would treat "bindings" as a profile name.
    assert response.status_code == 200
    assert "SECRET" not in response.text and "oapi.example" not in response.text
    worker = next(row for row in response.json()["bindings"] if row["profile"] == "worker")
    dynamic = {row["source_binding_key"]: row for row in worker["dynamic"]}
    ding = dynamic["source:dingtalk:group:cid:abc=="]
    # chat ids may themselves contain ':' -- only the leading fixed fields are split off.
    assert (ding["platform"], ding["scope"], ding["chat_id"]) == ("dingtalk", "group", "cid:abc==")
    assert ding["has_fallback_webhook"] is True and ding["created_by_name"] == "Alice"
    assert dynamic["source:telegram:dm:42"]["has_fallback_webhook"] is False
