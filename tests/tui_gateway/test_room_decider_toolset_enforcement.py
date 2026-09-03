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

import hermes_constants
import tui_gateway.server as srv
from tools import mcp_tool_agent as _mcp_agent
from tools import mcp_tool_discovery as _mcp_discovery
from tools import mcp_tool_lifecycle as _mcp_lifecycle
from tui_gateway.hosted_room_service import HostedRoomService

FULL_ENGINEER = ["bot_room", "browser", "clarify", "code_execution", "delegation", "file", "terminal", "todo", "web"]
ORCHESTRATION_ONLY = ["bot_room", "clarify", "delegation", "todo"]


def _decider_room(tmp_path: Path, monkeypatch, *, decider: str, worker: str) -> Path:
    db = tmp_path / "state.db"
    service = HostedRoomService(SimpleNamespace(_sessions={}, _sessions_lock=threading.RLock()), db_path=db)
    service.local_profiles = lambda: (decider, worker)
    service.create_room(
        room_id="room-1", name="Decider room",
        members=[{"member_id": decider, "profile": decider, "handle": decider, "role": "decider"},
                 {"member_id": worker, "profile": worker, "handle": worker}])
    monkeypatch.setattr("gateway.hosted_rooms.default_db_path", lambda: db)
    return db


def test_default_profile_decider_is_restricted(tmp_path: Path, monkeypatch):
    """A decider on the default profile carries ``profile_home=<root>``; its basename is ``.hermes``, so the
    filter must resolve the profile id through ``profile_name_for_home`` or the whitelist fails open."""
    _decider_room(tmp_path, monkeypatch, decider="default", worker="ops")
    session = {"source": "bot_room", "title": "Group: room-1",
               "profile_home": str(hermes_constants.get_default_hermes_root())}

    assert srv._room_decider_toolset_filter(session, list(FULL_ENGINEER)) == ORCHESTRATION_ONLY


def test_live_reload_keeps_decider_restricted_and_worker_full(tmp_path: Path, monkeypatch):
    """``reload.mcp`` re-resolves every live session's toolset from config; the decider must stay
    orchestration-only while a worker in the same room gets its full config toolset back."""
    _decider_room(tmp_path, monkeypatch, decider="lead", worker="backend")
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
