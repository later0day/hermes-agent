"""Tests for /room plan + /room confirm slash commands — M2.3.

Covers the full plan → confirm → create lifecycle with mocked planner
and mocked profile creation. Key constraint verified: no profile or
room is created until /room confirm is called (M2 DoD).
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gateway.agent_room_planner import PlannedMember, RoomPlan


def _fake_source(chat_id: str = "cid-plan-test") -> Any:
    from gateway.session import SessionSource
    from gateway.config import Platform
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        chat_type="group",
        user_id="user-plan",
    )


def _fake_event(text: str, chat_id: str = "cid-plan-test") -> Any:
    ev = MagicMock()
    ev.text = text
    ev.source = _fake_source(chat_id)
    ev.raw_message = None
    return ev


class _FakeMixin:
    from gateway.slash_commands import GatewaySlashCommandsMixin
    _handle_room_command = GatewaySlashCommandsMixin._handle_room_command
    _fence_room_active_sessions = GatewaySlashCommandsMixin._fence_room_active_sessions

    def __init__(self, store, binding_store):
        self._agent_room_store = store
        self._source_agent_binding_store = binding_store


@pytest.fixture
def stores(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    return (
        AgentRoomStore(tmp_path / "rooms.sqlite"),
        SourceAgentBindingStore(tmp_path / "bindings.sqlite"),
    )


@pytest.fixture
def mixin(stores):
    return _FakeMixin(*stores)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "profiles").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home))
    yield home
    reset_hermes_home_override(token)


_VALID_PLAN = RoomPlan(
    rationale="Need support and billing",
    members=[
        PlannedMember(profile="existing_svc", is_new=False, name="existing_svc",
                      description="Customer service", reason="matches support"),
        PlannedMember(profile=None, is_new=True, name="new_billing",
                      description="Billing handler", reason="no billing profile"),
    ],
    room_description="Support and billing room",
)


# ---------------------------------------------------------------------------
# /room plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_plan_no_requirement(mixin, hermes_home):
    result = await mixin._handle_room_command(_fake_event("/room plan"))
    assert "Usage" in result


@pytest.mark.asyncio
async def test_room_plan_returns_plan(mixin, hermes_home):
    """Plan is displayed but nothing is created."""
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        result = await mixin._handle_room_command(
            _fake_event("/room plan I need support and billing")
        )

    assert "Room Plan" in result
    assert "existing_svc" in result
    assert "new_billing" in result
    assert "Y" in result


@pytest.mark.asyncio
async def test_room_plan_not_actionable(mixin, hermes_home):
    """Vague requirement → not actionable → error message."""
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room",
               return_value=RoomPlan(rationale="requirement too vague")):
        result = await mixin._handle_room_command(
            _fake_event("/room plan stuff")
        )
    assert "failed" in result.lower() or "too vague" in result.lower()


@pytest.mark.asyncio
async def test_room_plan_stores_pending_plan(mixin, hermes_home):
    """Pending plan stored on self._pending_room_plans keyed by chat_id."""
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        await mixin._handle_room_command(
            _fake_event("/room plan I need support and billing")
        )

    assert hasattr(mixin, "_pending_room_plans")
    assert "cid-plan-test" in mixin._pending_room_plans
    assert mixin._pending_room_plans["cid-plan-test"] is _VALID_PLAN


@pytest.mark.asyncio
async def test_room_plan_no_creation_until_confirm(mixin, stores, hermes_home):
    """M2 DoD: /room plan must NOT create any profile or room."""
    room_store, _ = stores
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN), \
         patch("hermes_cli.profiles.create_profile") as m_create, \
         patch("gateway.agent_room_bootstrapper.build_observer_profile") as m_build:
        await mixin._handle_room_command(
            _fake_event("/room plan I need support and billing")
        )

    m_create.assert_not_called()
    m_build.assert_not_called()
    assert len(room_store.list_rooms()) == 0


# ---------------------------------------------------------------------------
# /room confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_confirm_no_pending_plan(mixin, hermes_home):
    result = await mixin._handle_room_command(_fake_event("/room confirm"))
    assert "No pending room plan" in result


@pytest.mark.asyncio
async def test_room_confirm_creates_room(mixin, stores, hermes_home):
    """Full lifecycle: plan → confirm → room + profiles created."""
    room_store, _ = stores

    # Phase 1: plan
    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        await mixin._handle_room_command(
            _fake_event("/room plan I need support and billing")
        )

    # Phase 2: confirm
    fake_obs_dir = hermes_home / "profiles" / "room_support_and_billing_room_observer"
    fake_obs_dir.mkdir(parents=True)
    (fake_obs_dir / ".observer").touch()

    with patch("hermes_cli.profiles.profile_exists", return_value=False), \
         patch("hermes_cli.profiles.create_profile", return_value=hermes_home / "profiles" / "x"), \
         patch("hermes_cli.profiles.write_profile_meta"), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=fake_obs_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=fake_obs_dir):
        result = await mixin._handle_room_command(
            _fake_event("/room confirm")
        )

    assert "Room" in result and "created" in result.lower()
    rooms = room_store.list_rooms()
    assert len(rooms) == 1
    # Verify members
    assert "existing_svc" in rooms[0].members
    assert "new_billing" in rooms[0].members


@pytest.mark.asyncio
async def test_room_confirm_clears_pending(mixin, stores, hermes_home):
    """After confirm, pending plan is cleared — second confirm fails."""
    room_store, _ = stores

    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        await mixin._handle_room_command(_fake_event("/room plan need help"))

    fake_obs_dir = hermes_home / "profiles" / "room_support_and_billing_room_observer"
    fake_obs_dir.mkdir(parents=True)
    (fake_obs_dir / ".observer").touch()

    with patch("hermes_cli.profiles.profile_exists", return_value=False), \
         patch("hermes_cli.profiles.create_profile", return_value=hermes_home / "profiles" / "x"), \
         patch("hermes_cli.profiles.write_profile_meta"), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=fake_obs_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=fake_obs_dir):
        await mixin._handle_room_command(_fake_event("/room confirm"))

    # Second confirm should say "No pending room plan"
    result = await mixin._handle_room_command(_fake_event("/room confirm"))
    assert "No pending room plan" in result


@pytest.mark.asyncio
async def test_room_confirm_rollback_on_failure(mixin, stores, hermes_home):
    """If observer build fails, created profiles are rolled back."""
    room_store, _ = stores

    with patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("gateway.agent_room_planner.plan_room", return_value=_VALID_PLAN):
        await mixin._handle_room_command(_fake_event("/room plan need help"))

    with patch("hermes_cli.profiles.profile_exists", return_value=False), \
         patch("hermes_cli.profiles.create_profile", return_value=hermes_home / "profiles" / "x"), \
         patch("hermes_cli.profiles.write_profile_meta"), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=hermes_home / "profiles" / "x"), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile",
               side_effect=RuntimeError("disk full")), \
         patch("hermes_cli.profiles.delete_profile") as m_delete:
        result = await mixin._handle_room_command(_fake_event("/room confirm"))

    assert "Failed" in result
    # delete_profile called for rollback
    m_delete.assert_called()
    assert len(room_store.list_rooms()) == 0
