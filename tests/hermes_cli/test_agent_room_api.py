"""Tests for /api/rooms REST endpoints — M1.8.

Uses httpx.AsyncClient against the FastAPI app in test mode. All
file-system operations (profile creation, bootstrapper) are mocked so
the tests don't touch the real Hermes home.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


async def _client():
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
    )


# ---------------------------------------------------------------------------
# GET /api/rooms — list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_rooms_empty(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    with patch("hermes_cli.web_server._room_store",
               return_value=AgentRoomStore(tmp_path / "rooms.sqlite")):
        async with await _client() as c:
            r = await c.get("/api/rooms")
    assert r.status_code == 200
    assert r.json()["rooms"] == []


@pytest.mark.asyncio
async def test_list_rooms_returns_created_rooms(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "Support", observer_profile="obs", members=["a"])

    with patch("hermes_cli.web_server._room_store", return_value=store):
        async with await _client() as c:
            r = await c.get("/api/rooms")
    assert r.status_code == 200
    assert len(r.json()["rooms"]) == 1
    assert r.json()["rooms"][0]["room_name"] == "Support"


# ---------------------------------------------------------------------------
# GET /api/rooms/{room_id} — detail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_room_not_found(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    with patch("hermes_cli.web_server._room_store",
               return_value=AgentRoomStore(tmp_path / "rooms.sqlite")):
        async with await _client() as c:
            r = await c.get("/api/rooms/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_room_returns_details(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "Support", observer_profile="obs", members=["a", "b"])

    with patch("hermes_cli.web_server._room_store", return_value=store):
        async with await _client() as c:
            r = await c.get("/api/rooms/r1")
    assert r.status_code == 200
    body = r.json()["room"]
    assert body["room_name"] == "Support"
    assert "a" in body["members"]


# ---------------------------------------------------------------------------
# POST /api/rooms — create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_rooms_nonexistent_member_rejected(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    with patch("hermes_cli.web_server._room_store",
               return_value=AgentRoomStore(tmp_path / "rooms.sqlite")), \
         patch("hermes_cli.profiles.profile_exists", return_value=False):
        async with await _client() as c:
            r = await c.post("/api/rooms", json={
                "name": "Test", "members": ["ghost"]
            })
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower() or "Profiles" in r.json()["detail"]


@pytest.mark.asyncio
async def test_post_rooms_creates_and_returns_room(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from pathlib import Path

    fake_dir = tmp_path / "profiles" / "room_test_observer"
    fake_dir.mkdir(parents=True)
    (fake_dir / ".observer").touch()

    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    with patch("hermes_cli.web_server._room_store", return_value=store), \
         patch("hermes_cli.profiles.profile_exists", side_effect=lambda n: n != "room_test_observer"), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=fake_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=fake_dir):
        async with await _client() as c:
            r = await c.post("/api/rooms", json={
                "name": "Test", "members": ["client_svc", "finance"],
                "description": "Customer support"
            })

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["room"]["room_name"] == "Test"


# ---------------------------------------------------------------------------
# PATCH /api/rooms/{room_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_room_updates_members(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from pathlib import Path

    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "Support", observer_profile="room_support_observer",
                      members=["a", "b"])
    obs_dir = tmp_path / "profiles" / "room_support_observer"
    obs_dir.mkdir(parents=True)
    (obs_dir / ".observer").touch()

    with patch("hermes_cli.web_server._room_store", return_value=store), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=obs_dir), \
         patch("gateway.agent_room_bootstrapper.regenerate_observer_soul"):
        async with await _client() as c:
            r = await c.patch("/api/rooms/r1", json={"members": ["a", "b", "c"]})

    assert r.status_code == 200
    assert "c" in r.json()["room"]["members"]


@pytest.mark.asyncio
async def test_patch_room_not_found(tmp_path):
    from gateway.agent_room_store import AgentRoomStore

    with patch("hermes_cli.web_server._room_store",
               return_value=AgentRoomStore(tmp_path / "rooms.sqlite")):
        async with await _client() as c:
            r = await c.patch("/api/rooms/ghost", json={"members": ["a"]})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/rooms/{room_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_room(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore

    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "Support", observer_profile="obs", members=["a"])
    binding_store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")

    with patch("hermes_cli.web_server._room_store", return_value=store), \
         patch("hermes_cli.web_server._room_binding_store", return_value=binding_store), \
         patch("gateway.agent_room_bootstrapper.teardown_observer_profile", return_value=True):
        async with await _client() as c:
            r = await c.delete("/api/rooms/r1")

    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # Reopen store (endpoint closed it in finally block)
    from gateway.agent_room_store import AgentRoomStore as _ReopenStore
    check = _ReopenStore(tmp_path / "rooms.sqlite")
    assert check.get_room("r1") is None
    check.close()


@pytest.mark.asyncio
async def test_delete_room_not_found(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore

    with patch("hermes_cli.web_server._room_store",
               return_value=AgentRoomStore(tmp_path / "rooms.sqlite")), \
         patch("hermes_cli.web_server._room_binding_store",
               return_value=SourceAgentBindingStore(tmp_path / "bindings.sqlite")):
        async with await _client() as c:
            r = await c.delete("/api/rooms/ghost")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/rooms/{room_id}/bind  +  /unbind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bind_and_unbind(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore

    db_rooms = tmp_path / "rooms.sqlite"
    db_bindings = tmp_path / "bindings.sqlite"
    source_key = "source:dingtalk:group:cid-1:u1"

    seed = AgentRoomStore(db_rooms)
    seed.create_room("r1", "Support", observer_profile="obs", members=["a"])
    seed.close()

    with patch("hermes_cli.web_server._room_store",
               side_effect=lambda: AgentRoomStore(db_rooms)),          patch("hermes_cli.web_server._room_binding_store",
               side_effect=lambda: SourceAgentBindingStore(db_bindings)):
        async with await _client() as c:
            r = await c.post("/api/rooms/r1/bind",
                             json={"source_binding_key": source_key})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        check = SourceAgentBindingStore(db_bindings)
        binding = check.get_binding(source_key)
        assert (binding.fallback_extra or {}).get("room_id") == "r1"
        check.close()

        async with await _client() as c:
            r = await c.post("/api/rooms/r1/unbind",
                             json={"source_binding_key": source_key})
        assert r.status_code == 200
        assert r.json()["unbound"] is True

        check2 = SourceAgentBindingStore(db_bindings)
        assert check2.get_binding(source_key) is None
        check2.close()


@pytest.mark.asyncio
async def test_bind_invalid_source_key(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore

    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "Support", observer_profile="obs", members=["a"])

    with patch("hermes_cli.web_server._room_store", return_value=store), \
         patch("hermes_cli.web_server._room_binding_store",
               return_value=SourceAgentBindingStore(tmp_path / "bindings.sqlite")):
        async with await _client() as c:
            r = await c.post("/api/rooms/r1/bind",
                             json={"source_binding_key": "bad-key"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Route registration check (source introspection)
# ---------------------------------------------------------------------------

def test_all_seven_endpoints_registered():
    import inspect
    from hermes_cli import web_server
    src = inspect.getsource(web_server)
    routes = [
        '"/api/rooms"',
        '"/api/rooms/{room_id}"',
        '"/api/rooms/{room_id}/bind"',
        '"/api/rooms/{room_id}/unbind"',
    ]
    for route in routes:
        assert route in src, f"Missing route {route} in web_server.py"
