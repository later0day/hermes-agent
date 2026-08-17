"""Tests for gateway/slash_commands.py::_handle_room_command — M1.6.

Uses a thin event stub (no real gateway) + real SQLite-backed stores in
tmp dirs. Boundary tests owned here: M1-B1 (nonexistent member on
create), M1-B3 (too-many-members), M1-B8 (duplicate room name), M1-B9
(delete idempotent + binding cleanup), M1-B14 (member profile missing on
add).
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _fake_source(chat_id: str = "cid-test") -> Any:
    # Use a real SessionSource, not a MagicMock — build_source_binding_key
    # reads several fields (thread_id, etc.) that a MagicMock would
    # auto-vivify into per-instance mock objects, making the derived key
    # unstable across calls (bind writes one key, the test query computes
    # a different one). A real dataclass gives deterministic keys.
    from gateway.session import SessionSource
    from gateway.config import Platform
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        chat_type="group",
        user_id="user-1",
    )


def _fake_event(text: str, chat_id: str = "cid-test") -> Any:
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


# ── /room list ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_list_empty(mixin):
    result = await mixin._handle_room_command(_fake_event("/room list"))
    assert "No rooms defined" in result


@pytest.mark.asyncio
async def test_room_list_shows_rooms(mixin, stores):
    room_store, _ = stores
    room_store.create_room(
        "r1", "Support",
        observer_profile="room_support_observer",
        members=["client_svc", "finance"],
    )
    result = await mixin._handle_room_command(_fake_event("/room list"))
    assert "Support" in result
    assert "client_svc" in result


# ── /room create ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_create_missing_members_flag(mixin, hermes_home):
    result = await mixin._handle_room_command(_fake_event("/room create MyRoom"))
    assert "Usage" in result


@pytest.mark.asyncio
async def test_room_create_nonexistent_member_rejected(mixin, hermes_home):
    """M1-B1."""
    with patch("hermes_cli.profiles.profile_exists", return_value=False):
        result = await mixin._handle_room_command(
            _fake_event("/room create MyRoom --members ghost")
        )
    assert "don't exist" in result or "not exist" in result.lower()


@pytest.mark.asyncio
async def test_room_create_succeeds(mixin, hermes_home):
    created_dir = hermes_home / "profiles" / "room_support_observer"
    created_dir.mkdir(parents=True)
    (created_dir / ".observer").touch()
    def _pexists(name):
        return name != "room_support_observer"  # members exist; observer slot is free

    with patch("hermes_cli.profiles.profile_exists", side_effect=_pexists), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={"description": "客服"}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=created_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=created_dir):
        result = await mixin._handle_room_command(
            _fake_event("/room create Support --members client_svc,finance")
        )
    assert "Support" in result and "created" in result.lower()


@pytest.mark.asyncio
async def test_room_create_duplicate_name_rejected(mixin, stores, hermes_home):
    """M1-B8."""
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="obs", members=["a"])
    with patch("hermes_cli.profiles.profile_exists", return_value=True):
        result = await mixin._handle_room_command(
            _fake_event("/room create Support --members a")
        )
    assert "already exists" in result


@pytest.mark.asyncio
async def test_room_create_too_many_members_rejected(mixin, hermes_home):
    """M1-B3."""
    from gateway.agent_room_store import MAX_ROOM_MEMBERS
    too_many = ",".join(f"p{i}" for i in range(MAX_ROOM_MEMBERS + 1))
    created_dir = hermes_home / "profiles" / "room_overflow_observer"
    created_dir.mkdir(parents=True)
    def _pexists2(name):
        return name != "room_overflow_observer"  # member profiles exist; observer slot free

    with patch("hermes_cli.profiles.profile_exists", side_effect=_pexists2), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=created_dir), \
         patch("gateway.agent_room_bootstrapper.build_observer_profile", return_value=created_dir), \
         patch("gateway.agent_room_bootstrapper.teardown_observer_profile", return_value=True):
        result = await mixin._handle_room_command(
            _fake_event(f"/room create Overflow --members {too_many}")
        )
    assert "Failed" in result or "more than" in result


# ── /room info ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_info_not_found(mixin):
    result = await mixin._handle_room_command(_fake_event("/room info NoSuchRoom"))
    assert "not found" in result


@pytest.mark.asyncio
async def test_room_info_shows_details(mixin, stores):
    room_store, _ = stores
    room_store.create_room(
        "r1", "Support", observer_profile="room_support_observer",
        members=["client_svc", "finance"], description="Customer support",
    )
    result = await mixin._handle_room_command(_fake_event("/room info Support"))
    assert "Support" in result
    assert "client_svc" in result
    assert "Customer support" in result


# ── /room bind + unbind ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_bind_not_found(mixin):
    result = await mixin._handle_room_command(_fake_event("/room bind NoSuchRoom"))
    assert "not found" in result


@pytest.mark.asyncio
async def test_room_bind_succeeds(mixin, stores):
    room_store, binding_store = stores
    room_store.create_room("r1", "Support", observer_profile="room_support_observer", members=["client_svc"])
    result = await mixin._handle_room_command(_fake_event("/room bind Support"))
    assert "bound" in result.lower()
    from gateway.session import build_source_binding_key
    binding = binding_store.get_binding(build_source_binding_key(_fake_source()))
    assert binding is not None
    assert (binding.fallback_extra or {}).get("room_id") == "r1"


@pytest.mark.asyncio
async def test_room_bind_refuses_when_already_bound_to_another_room(mixin, stores):
    """A6: 群↔Room 1-to-1."""
    room_store, _ = stores
    room_store.create_room("r1", "RoomA", observer_profile="obs_a", members=["a"])
    room_store.create_room("r2", "RoomB", observer_profile="obs_b", members=["a"])
    await mixin._handle_room_command(_fake_event("/room bind RoomA"))
    result = await mixin._handle_room_command(_fake_event("/room bind RoomB"))
    assert "already bound" in result.lower() or "unbind" in result.lower()


@pytest.mark.asyncio
async def test_room_unbind_clears_binding(mixin, stores):
    room_store, binding_store = stores
    room_store.create_room("r1", "Support", observer_profile="obs", members=["a"])
    await mixin._handle_room_command(_fake_event("/room bind Support"))
    result = await mixin._handle_room_command(_fake_event("/room unbind"))
    assert "unbound" in result.lower()
    from gateway.session import build_source_binding_key
    assert binding_store.get_binding(build_source_binding_key(_fake_source())) is None


@pytest.mark.asyncio
async def test_room_unbind_when_not_bound(mixin):
    result = await mixin._handle_room_command(_fake_event("/room unbind"))
    assert "not bound" in result.lower()


# ── /room delete ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_delete_not_found(mixin):
    result = await mixin._handle_room_command(_fake_event("/room delete NoSuchRoom"))
    assert "not found" in result


@pytest.mark.asyncio
async def test_room_delete_removes_room_and_clears_bindings(mixin, stores, hermes_home):
    """M1-B9."""
    room_store, binding_store = stores
    room_store.create_room("r1", "Support", observer_profile="room_support_observer", members=["a"])
    await mixin._handle_room_command(_fake_event("/room bind Support"))
    with patch("gateway.agent_room_bootstrapper.teardown_observer_profile", return_value=True):
        result = await mixin._handle_room_command(_fake_event("/room delete Support"))
    assert "Deleted" in result
    assert room_store.get_room_by_name("Support") is None
    from gateway.session import build_source_binding_key
    assert binding_store.get_binding(build_source_binding_key(_fake_source())) is None


# ── /room members ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_members_add(mixin, stores, hermes_home):
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="room_support_observer", members=["a"])
    obs_dir = hermes_home / "profiles" / "room_support_observer"
    obs_dir.mkdir(parents=True)
    (obs_dir / ".observer").touch()
    with patch("hermes_cli.profiles.profile_exists", return_value=True), \
         patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=obs_dir), \
         patch("gateway.agent_room_bootstrapper.regenerate_observer_soul"):
        result = await mixin._handle_room_command(_fake_event("/room members Support add b"))
    assert "added" in result.lower()
    assert room_store.get_room_by_name("Support").members == ("a", "b")


@pytest.mark.asyncio
async def test_room_members_add_m1_b14_profile_not_found(mixin, stores):
    """M1-B14."""
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="obs", members=["a"])
    with patch("hermes_cli.profiles.profile_exists", return_value=False):
        result = await mixin._handle_room_command(_fake_event("/room members Support add ghost"))
    assert "not exist" in result.lower()


@pytest.mark.asyncio
async def test_room_members_remove(mixin, stores, hermes_home):
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="room_support_observer", members=["a", "b"])
    obs_dir = hermes_home / "profiles" / "room_support_observer"
    obs_dir.mkdir(parents=True)
    (obs_dir / ".observer").touch()
    with patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=obs_dir), \
         patch("gateway.agent_room_bootstrapper.regenerate_observer_soul"):
        result = await mixin._handle_room_command(_fake_event("/room members Support remove b"))
    assert "removed" in result.lower()
    assert room_store.get_room_by_name("Support").members == ("a",)


@pytest.mark.asyncio
async def test_room_members_remove_last_member_rejected(mixin, stores):
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="obs", members=["a"])
    result = await mixin._handle_room_command(_fake_event("/room members Support remove a"))
    assert "last member" in result.lower() or "Cannot remove" in result


# ── /room set-default-member ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_default_member_succeeds(mixin, stores, hermes_home):
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="room_support_observer", members=["a", "b"])
    obs_dir = hermes_home / "profiles" / "room_support_observer"
    obs_dir.mkdir(parents=True)
    (obs_dir / ".observer").touch()
    with patch("hermes_cli.profiles.read_profile_meta", return_value={}), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=obs_dir), \
         patch("gateway.agent_room_bootstrapper.regenerate_observer_soul"):
        result = await mixin._handle_room_command(_fake_event("/room set-default-member Support b"))
    assert "b" in result
    assert room_store.get_room_by_name("Support").default_member == "b"


@pytest.mark.asyncio
async def test_set_default_member_non_roster_rejected(mixin, stores):
    room_store, _ = stores
    room_store.create_room("r1", "Support", observer_profile="obs", members=["a", "b"])
    result = await mixin._handle_room_command(_fake_event("/room set-default-member Support ghost"))
    assert "not a member" in result.lower()


# ── run.py command table registration ─────────────────────────────────

def test_room_command_in_run_command_table():
    import inspect
    import gateway.run as run_module
    src = inspect.getsource(run_module)
    assert '"room": self._handle_room_command' in src


def test_room_command_dispatched_in_both_sites():
    import inspect
    import gateway.run as run_module
    src = inspect.getsource(run_module)
    assert src.count("_handle_room_command") >= 2


# ── unknown action ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_action(mixin):
    result = await mixin._handle_room_command(_fake_event("/room frobnicate"))
    assert "Unknown" in result
