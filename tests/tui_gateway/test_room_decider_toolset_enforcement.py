"""F1 decider whitelist must hold on every path that hands an agent its toolset.

``_room_decider_toolset_filter`` strips a hosted-room decider down to the orchestration-only allow-set when its
agent is built. Two paths could silently undo that: a live reload (``reload.mcp`` / plugin activation) that
re-resolves each session's toolset from config, and a decider bound to the *default* profile, whose home
basename (``.hermes``) is not the profile id ``default``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_constants
import tui_gateway.server as srv
from gateway import hosted_rooms
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session
from tools import mcp_tool_agent as _mcp_agent
from tools import mcp_tool_discovery as _mcp_discovery
from tools import mcp_tool_lifecycle as _mcp_lifecycle
from tui_gateway.hosted_room_service import HostedRoomService

FULL_ENGINEER = ["bot_room", "browser", "clarify", "code_execution", "delegation", "file", "terminal", "todo", "web"]
ORCHESTRATION_ONLY = ["bot_room", "clarify", "todo"]


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    """A store handle on the test home, closed at teardown."""
    handle = open_persistence(profile_scope(home=tmp_path))
    yield handle
    handle.close()


def _decider_room(store, *, decider: str, worker: str, placement="profile"):
    service = HostedRoomService(
        SimpleNamespace(_sessions={}, _sessions_lock=threading.RLock()),
        store=store,
        placement=placement,
    )
    service.local_profiles = lambda: (decider, worker)
    return service.create_room(
        room_id="room-1", name="Decider room",
        members=[{"member_id": decider, "profile": decider, "handle": decider, "role": "decider"},
                 {"member_id": worker, "profile": worker, "handle": worker}])


def test_default_profile_decider_is_restricted(tmp_path: Path, store):
    """A decider on the default profile carries ``profile_home=<root>``; its basename is ``.hermes``, so the
    filter must resolve the profile id through ``profile_name_for_home`` or the whitelist fails open."""
    _decider_room(store, decider="default", worker="ops")
    session = {"source": "bot_room", "title": "Group: room-1",
               "profile_home": str(hermes_constants.get_default_hermes_root())}

    assert srv._room_decider_toolset_filter(
        session, list(FULL_ENGINEER), persistence=store, placement="profile"
    ) == ORCHESTRATION_ONLY


def test_live_reload_keeps_decider_restricted_and_worker_full(tmp_path: Path, monkeypatch, request):
    """``reload.mcp`` re-resolves every live session's toolset from config; the decider must stay
    orchestration-only while a worker in the same room gets its full config toolset back."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for profile in ("lead", "backend"):
        home = tmp_path / "profiles" / profile
        home.mkdir(parents=True)
        profile_store = open_persistence(profile_scope(home=home))
        request.addfinalizer(profile_store.close)
        _decider_room(profile_store, decider="lead", worker="backend", placement="shared")
    seen: dict[str, list[str] | None] = {}
    monkeypatch.setattr(_mcp_lifecycle, "shutdown_mcp_servers", lambda: None)
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", lambda: None)
    monkeypatch.setattr(_mcp_agent, "refresh_agent_mcp_tools",
                        lambda agent, **kw: seen.__setitem__(agent.name, kw.get("enabled_override")) or set())
    monkeypatch.setattr(srv, "_load_enabled_toolsets", lambda platform=None: list(FULL_ENGINEER))
    monkeypatch.setattr(srv, "_compute_mcp_rev", lambda: "rev-a")
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: True)
    monkeypatch.setattr(srv, "_session_info", lambda agent, session=None: {})
    monkeypatch.setattr(srv, "_mcp_reload_gen", 0)
    monkeypatch.setattr(srv, "_mcp_reload_loaded_rev", "")

    def _member_session(profile: str) -> dict:
        return {"agent": SimpleNamespace(name=profile, platform="bot_room"), "history": [],
                "history_lock": threading.RLock(), "running": False, "source": "bot_room",
                "title": "Group: room-1", "profile_home": str(tmp_path / "profiles" / profile)}

    monkeypatch.setattr(srv, "_sessions", {"D": _member_session("lead"), "W": _member_session("backend")})

    resp = srv._methods["reload.mcp"](1, {"session_id": "D", "confirm": True})

    assert resp["result"]["status"] == "reloaded"
    assert seen["lead"] == ORCHESTRATION_ONLY
    assert seen["backend"] == FULL_ENGINEER


def test_resumed_member_sessions_resolve_room_from_persisted_title(tmp_path: Path, monkeypatch, request):
    """After a backend restart the room driver RESUMES member sessions: the live record carries neither
    ``pending_title`` nor ``title`` (only the DB row does). The worker must keep its tools and the decider
    must stay restricted -- reading only the live record failed every resumed worker closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _resumed(profile: str, key: str) -> tuple[dict, object]:
        home = tmp_path / "profiles" / profile
        home.mkdir(parents=True)
        db = open_persistence(profile_scope(home=home))
        request.addfinalizer(db.close)
        _decider_room(db, decider="lead", worker="backend")
        seed_session(db, key, "bot_room", title="Group: room-1")
        return {"source": "bot_room", "session_key": key, "profile_home": str(home)}, db

    backend, backend_store = _resumed("backend", "s-be")
    lead, lead_store = _resumed("lead", "s-lead")
    assert srv._room_decider_toolset_filter(
        backend, list(FULL_ENGINEER), persistence=backend_store, placement="profile"
    ) == FULL_ENGINEER
    assert srv._room_decider_toolset_filter(
        lead, list(FULL_ENGINEER), persistence=lead_store, placement="profile"
    ) == ORCHESTRATION_ONLY


def test_legacy_group_fence_holds_for_resumed_room_sessions(tmp_path: Path, monkeypatch, request):
    """A direct prompt into a hosted room's session must be fenced off even when the session was resumed and
    its live record carries no title (the fence read only ``session["title"]``, which is never populated)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = tmp_path / "profiles" / "backend"
    home.mkdir(parents=True)
    db = open_persistence(profile_scope(home=home))
    request.addfinalizer(db.close)
    room = _decider_room(db, decider="lead", worker="backend")
    hosted_rooms.create_room(
        db,
        placement="shared",
        room_id="room-1",
        name="Decider room",
        members=room["members"],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )
    seed_session(db, "s-room", "bot_room", title="Group: room-1")
    seed_session(db, "s-chat", "desktop")

    def _fence(key: str):
        return srv._legacy_group_fence_error(  # the production binding (methods are rebound onto server)
            1, {"source": "bot_room", "session_key": key, "profile_home": str(home)}, {})

    assert _fence("s-room") is not None and "error" in _fence("s-room")
    assert _fence("s-chat") is None
