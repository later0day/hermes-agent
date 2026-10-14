"""The dashboard binds a chat to a profile (and unbinds it) like ``/agent use`` does in the chat.

Before, a chat could only be bound by running ``/agent use`` inside it (the fork's dashboard could bind
from the Profiles page, 1e481b7501). The binding is the chat-level key, so the gateway's own resolver
routes every member of the group to the profile; unbinding removes it. Real router, store and resolver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

CID = "cidTEAMxxxxxxxxxxxxxxxxx=="


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import profiles as profiles_mod, web_server

    monkeypatch.setattr(profiles_mod, "seed_profile_skills", lambda *a, **k: None)
    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {web_server._SESSION_TOKEN}"
        assert c.post("/api/profiles", json={"name": "ops"}).status_code == 200
        yield c


def _routed_profile(user_id: str):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner.__new__(GatewayRunner)
    return runner._binding_profile_for_source(
        SessionSource(platform=Platform.DINGTALK, chat_id=CID, chat_type="group", user_id=user_id))


def test_dashboard_bind_routes_every_group_member_and_unbind_reverts(client):
    resp = client.post("/api/profiles/ops/bindings",
                       json={"platform": "dingtalk", "chat_id": CID, "chat_type": "group", "chat_name": "Ops"})
    assert resp.status_code == 200, resp.text
    key = resp.json()["source_binding_key"]

    assert _routed_profile("alice") == "ops" and _routed_profile("bob") == "ops"
    listed = {b["profile"]: b["dynamic"] for b in client.get("/api/profiles/bindings").json()["bindings"]}
    assert [d["source_binding_key"] for d in listed["ops"]] == [key]

    assert client.delete("/api/profiles/bindings", params={"key": key}).status_code == 200
    assert _routed_profile("alice") is None
    assert client.delete("/api/profiles/bindings", params={"key": key}).status_code == 404


def test_bind_rejects_unknown_profile_and_bad_chat_type(client):
    body = {"platform": "dingtalk", "chat_id": CID, "chat_type": "group"}
    assert client.post("/api/profiles/ghost/bindings", json=body).status_code == 404
    assert client.post("/api/profiles/ops/bindings", json={**body, "chat_type": "weird"}).status_code == 400
