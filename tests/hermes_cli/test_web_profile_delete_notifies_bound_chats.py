"""Deleting a profile from the dashboard tells each chat bound to it (``/agent use``) that it was unbound.

``/agent delete`` in chat notifies the other bound chats; a dashboard delete settled the bindings away
silently, so those chats quietly fell back to the default profile. The fork notified them
(a6f96a72c6). Real router, real binding store and profile delete against a temp HERMES_HOME; only the
DingTalk OpenAPI is mocked at the HTTP layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

CID = "cidBOUNDxxxxxxxxxxxxxxxxx=="


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("platforms:\n  dingtalk:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "app-key")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "app-secret")
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    from hermes_cli import web_server

    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {web_server._SESSION_TOKEN}"
        yield c


def _mock_dingtalk(monkeypatch, sent):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "tok"})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"processQueryKey": "pqk"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_dashboard_delete_notifies_and_unbinds_the_bound_chat(client, monkeypatch):
    from gateway.source_agent_binding import SourceAgentBindingStore, source_agent_bindings_db_path
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "seed_profile_skills", lambda *a, **k: None)
    assert client.post("/api/profiles", json={"name": "ops"}).status_code == 200
    store = SourceAgentBindingStore(db_path=source_agent_bindings_db_path())
    store.set_binding("source:dingtalk:group:" + CID, "ops",
                      fallback_target={"platform": "dingtalk", "chat_id": CID, "chat_type": "group"})
    store.close()
    sent: list = []
    _mock_dingtalk(monkeypatch, sent)

    resp = client.delete("/api/profiles/ops")

    assert resp.status_code == 200, resp.text
    assert resp.json()["notified_chats"] == 1
    assert [m["openConversationId"] for m in sent] == [CID]
    assert "ops" in json.loads(sent[0]["msgParam"])["text"]
    store = SourceAgentBindingStore(db_path=source_agent_bindings_db_path())
    try:
        assert store.list_bindings(profile_name="ops") == []
    finally:
        store.close()
