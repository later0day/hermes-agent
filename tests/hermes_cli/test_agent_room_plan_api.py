"""Tests for POST /api/rooms/plan + POST /api/rooms/plan/confirm — M2.4."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from gateway.agent_room_planner import PlannedMember, RoomPlan


async def _client():
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
    )


_VALID_PLAN = RoomPlan(
    rationale="Need support and billing",
    members=[
        PlannedMember(profile="existing_svc", is_new=False, name="existing_svc",
                      description="Customer service", reason="matches"),
        PlannedMember(profile=None, is_new=True, name="billing",
                      description="Billing", reason="no billing profile"),
    ],
    room_description="Support billing room",
)


# ---------------------------------------------------------------------------
# POST /api/rooms/plan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_returns_plan(tmp_path):
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        async with await _client() as c:
            r = await c.post("/api/rooms/plan", json={"requirement": "I need support"})

    assert r.status_code == 200
    body = r.json()
    assert "plan" in body
    assert len(body["plan"]["members"]) == 2


@pytest.mark.asyncio
async def test_plan_unactionable_returns_422(tmp_path):
    empty_plan = RoomPlan(rationale="requirement too vague")
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=empty_plan):
        async with await _client() as c:
            r = await c.post("/api/rooms/plan", json={"requirement": "stuff"})

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_plan_stores_pending():
    """Plan stored in _pending_room_plans under the session token key."""
    from hermes_cli import web_server as ws
    ws._pending_room_plans.clear()

    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        async with await _client() as c:
            await c.post("/api/rooms/plan", json={"requirement": "test"})

    assert len(ws._pending_room_plans) == 1


# ---------------------------------------------------------------------------
# POST /api/rooms/plan/confirm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_no_pending_plan():
    from hermes_cli import web_server as ws
    ws._pending_room_plans.clear()
    async with await _client() as c:
        r = await c.post("/api/rooms/plan/confirm", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_confirm_creates_room(tmp_path):
    from hermes_cli import web_server as ws
    from gateway.agent_room_store import AgentRoomStore

    ws._pending_room_plans.clear()
    db = tmp_path / "rooms.sqlite"
    store = AgentRoomStore(db)

    obs_dir = tmp_path / "obs"
    obs_dir.mkdir()
    (obs_dir / ".observer").touch()

    # Plant the pending plan
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        async with await _client() as c:
            await c.post("/api/rooms/plan", json={"requirement": "test"})

    with patch("hermes_cli.web_server._room_store", return_value=store), \
         patch("hermes_cli.profiles.profile_exists", return_value=False), \
         patch("hermes_cli.profiles.create_profile", return_value=tmp_path / "x"), \
         patch("hermes_cli.profiles.write_profile_meta"), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=obs_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=obs_dir):
        async with await _client() as c:
            r = await c.post("/api/rooms/plan/confirm", json={})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "room" in body
    assert "billing" in body["new_profiles"]
    store.close()


@pytest.mark.asyncio
async def test_confirm_clears_pending():
    """Second confirm returns 404 after first succeeds."""
    from hermes_cli import web_server as ws

    ws._pending_room_plans.clear()

    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        async with await _client() as c:
            await c.post("/api/rooms/plan", json={"requirement": "test"})

    obs_dir = MagicMock()
    obs_dir.__truediv__ = MagicMock(return_value=obs_dir)
    obs_dir.touch = MagicMock()
    obs_dir.is_file = MagicMock(return_value=True)

    from gateway.agent_room_store import AgentRoomStore
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        store = AgentRoomStore(pathlib.Path(td) / "r.sqlite")
        with patch("hermes_cli.web_server._room_store", return_value=store), \
             patch("hermes_cli.profiles.profile_exists", return_value=False), \
             patch("hermes_cli.profiles.create_profile"), \
             patch("hermes_cli.profiles.write_profile_meta"), \
             patch("hermes_cli.profiles.get_profile_dir"), \
             patch("gateway.agent_room_bootstrapper.build_observer_profile"):
            async with await _client() as c:
                r1 = await c.post("/api/rooms/plan/confirm", json={})
            async with await _client() as c:
                r2 = await c.post("/api/rooms/plan/confirm", json={})
        store.close()

    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_plan_route_registered():
    import inspect
    from hermes_cli import web_server
    src = inspect.getsource(web_server)
    assert '"/api/rooms/plan"' in src
    assert '"/api/rooms/plan/confirm"' in src
