"""Tests for gateway/run.py M1.7 Room branch.

Verifies _get_room_for_source and the
_process_message_via_room_if_bound passthrough/handled behaviour using
source-introspection (inspect.getsource) plus lightweight unit tests.
No real gateway is instantiated.
"""

from __future__ import annotations

import asyncio
import inspect
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as run_module


def _make_runner(room_store=None, binding_store=None):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock()
    runner.adapters = {}
    if room_store is not None:
        runner._agent_room_store = room_store
    if binding_store is not None:
        runner._source_agent_binding_store = binding_store
    return runner


# ---------------------------------------------------------------------------
# Source-code presence checks (fast, no fixtures)
# ---------------------------------------------------------------------------


def test_room_branch_inserted_before_run_agent():
    """M1.7 requires the Room check to run BEFORE _run_agent so a
    room-bound message never enters a single-profile turn."""
    src = inspect.getsource(run_module)
    room_pos = src.find("_process_message_via_room_if_bound")
    run_agent_pos = src.find("await self._run_agent(")
    assert room_pos != -1, "_process_message_via_room_if_bound not found in run.py"
    assert run_agent_pos != -1, "await self._run_agent( not found in run.py"
    assert room_pos < run_agent_pos, (
        "Room branch must appear before the _run_agent call in _handle_message"
    )


def test_room_handled_returns_early():
    """Room handling must return early (not fall through to _run_agent)."""
    src = inspect.getsource(run_module)
    # Find the room-check block
    start = src.find("_room_result = await self._process_message_via_room_if_bound")
    assert start != -1
    snippet = src[start:start + 500]
    assert "return _room_result" in snippet


def test_get_room_for_source_method_exists():
    src = inspect.getsource(run_module)
    assert "def _get_room_for_source" in src


def test_process_message_via_room_if_bound_method_exists():
    src = inspect.getsource(run_module)
    assert "async def _process_message_via_room_if_bound" in src


# ---------------------------------------------------------------------------
# Mount point 2: room reuses the OFFICIAL execution core (_run_agent_inner)
# via _toolsets_override — NOT a private fork of the agent loop.
#
# The whole Agent Room design rests on "guard-style injection + reuse of the
# official agent turn". These sentinels fail loudly if an upstream merge ever
# removes/renames the _toolsets_override plumbing, which would silently make
# the observer see all 57 tools (route_to_member buried) or the member run
# with its default file/terminal tools. There was ZERO coverage of this path.
# ---------------------------------------------------------------------------


def test_observer_turn_reuses_run_agent_inner():
    """The observer turn MUST go through the official _run_agent_inner, not a
    forked loop — that is the core "reuse, don't reimplement" contract."""
    src = inspect.getsource(run_module)
    # The observer call site passes _toolsets_override=["room_observer"] into
    # _run_agent_inner. Verify both the call and the override token exist.
    assert "async def _run_agent_inner" in src, (
        "official execution core _run_agent_inner missing from run.py"
    )
    assert 'self._run_agent_inner(' in src, (
        "room path must invoke self._run_agent_inner (reuse official core)"
    )
    assert '_toolsets_override=["room_observer"]' in src, (
        "observer turn must lock to the room_observer toolset via "
        "_toolsets_override — otherwise route_to_member is buried in 57 tools"
    )


def test_run_agent_inner_accepts_toolsets_override_kwarg():
    """_run_agent_inner must still consume the _toolsets_override kwarg and
    turn it into a platform_toolsets replacement + tool_search:off. If an
    upstream refactor drops this, the observer lockdown silently no-ops."""
    src = inspect.getsource(run_module)
    assert 'kwargs.pop("_toolsets_override"' in src, (
        "_run_agent_inner no longer pops _toolsets_override — the room "
        "observer/member toolset lockdown would be silently ignored"
    )
    # It replaces platform_toolsets for the resolved platform_key ...
    assert 'pt[platform_key] = list(_toolsets_override)' in src, (
        "_toolsets_override must REPLACE platform_toolsets[platform_key]"
    )
    # ... and disables tool_search so _HERMES_CORE_TOOLS isn't re-added.
    override_block = src[src.find('kwargs.pop("_toolsets_override"'):]
    override_block = override_block[:2000]
    assert 'tool_search' in override_block and '"off"' in override_block, (
        "override must force tool_search:off so tier-1 core tools are not "
        "re-added on top of the locked toolset"
    )


def test_toolsets_override_propagates_to_turn_runner():
    """The kwarg must be forwarded onto the TurnRunner instance, because the
    only reliable point to force-filter agent.tools is TurnRunner.run_sync
    (a separate class, not a closure of _run_agent_inner)."""
    src = inspect.getsource(run_module)
    assert 'turn_runner._toolsets_override = _toolsets_override' in src, (
        "TurnRunner must receive _toolsets_override; without it the tool "
        "force-filter in run_sync never fires"
    )
    # And TurnRunner actually reads it back and rebuilds agent.tools.
    assert 'getattr(self, "_toolsets_override", None)' in src, (
        "TurnRunner.run_sync must read _toolsets_override to lock agent.tools"
    )


def test_toolsets_override_filters_agent_tools_via_resolve_toolset():
    """When _toolsets_override is active the agent's tool list is rebuilt from
    the toolsets registry (resolve_toolset), not left as the default 57."""
    src = inspect.getsource(run_module)
    start = src.find("if _toolsets_override:")
    assert start != -1, "expected an `if _toolsets_override:` guard in run.py"
    # Somewhere under the override path we resolve the toolset to a tool set.
    assert "from toolsets import resolve_toolset" in src, (
        "override path must resolve the toolset via toolsets.resolve_toolset"
    )
    assert "ROUTE_TO_MEMBER_SCHEMA" in src, (
        "observer override must inject route_to_member's schema explicitly "
        "(tool_search defers it, so it may be absent from agent.tools)"
    )


# ---------------------------------------------------------------------------
# _get_room_for_source: unit tests with stubbed stores
# ---------------------------------------------------------------------------


def test_get_room_for_source_returns_none_when_no_binding(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource
    from gateway.config import Platform

    runner = _make_runner(
        room_store=AgentRoomStore(tmp_path / "rooms.sqlite"),
        binding_store=SourceAgentBindingStore(tmp_path / "bindings.sqlite"),
    )
    source = SessionSource(
        platform=Platform.DINGTALK,
        chat_id="cid-1",
        chat_type="group",
        user_id="u1",
    )
    assert runner._get_room_for_source(source) is None


def test_get_room_for_source_returns_none_for_single_profile_binding(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource, build_source_binding_key
    from gateway.config import Platform

    room_store = AgentRoomStore(tmp_path / "rooms.sqlite")
    binding_store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    runner = _make_runner(room_store=room_store, binding_store=binding_store)

    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    # Bind to a single profile (no room_id in fallback_extra)
    binding_store.set_binding(
        build_source_binding_key(source),
        "default",
        fallback_extra={},  # no room_id
    )
    assert runner._get_room_for_source(source) is None


def test_get_room_for_source_returns_room_when_bound(tmp_path):
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource, build_source_binding_key
    from gateway.config import Platform

    room_store = AgentRoomStore(tmp_path / "rooms.sqlite")
    binding_store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    runner = _make_runner(room_store=room_store, binding_store=binding_store)

    # Create a room
    room = room_store.create_room(
        "r1", "Support", observer_profile="room_support_observer",
        members=["client_svc"],
    )
    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    # Bind with room_id
    binding_store.set_binding(
        build_source_binding_key(source),
        "room_support_observer",
        fallback_extra={"room_id": room.room_id},
    )
    result = runner._get_room_for_source(source)
    assert result is not None
    assert result.room_id == "r1"


def test_get_room_for_source_swallows_exceptions(tmp_path):
    """Any lookup error must never crash _handle_message — just fall through."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    # Broken store that raises on every call
    broken_store = MagicMock()
    broken_store.get_binding.side_effect = RuntimeError("db locked")
    runner._source_agent_binding_store = broken_store

    from gateway.session import SessionSource
    from gateway.config import Platform
    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    result = runner._get_room_for_source(source)
    assert result is None  # exception swallowed, falls through


# ---------------------------------------------------------------------------
# _process_message_via_room_if_bound passthrough / handled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_when_no_room(tmp_path):
    """Not room-bound → method returns None → _handle_message continues."""
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource
    from gateway.config import Platform

    runner = _make_runner(
        room_store=AgentRoomStore(tmp_path / "rooms.sqlite"),
        binding_store=SourceAgentBindingStore(tmp_path / "bindings.sqlite"),
    )
    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    result = await runner._process_message_via_room_if_bound(
        event=MagicMock(source=source),
        source=source,
        message_text="hello",
        history=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_handled_returns_non_none_when_room_bound(tmp_path):
    """Room-bound source → router is called → method returns non-None string."""
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource, build_source_binding_key
    from gateway.config import Platform

    room_store = AgentRoomStore(tmp_path / "rooms.sqlite")
    binding_store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")

    room = room_store.create_room(
        "r1", "Support", observer_profile="room_support_observer",
        members=["client_svc"],
    )
    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    binding_store.set_binding(
        build_source_binding_key(source),
        "room_support_observer",
        fallback_extra={"room_id": room.room_id},
    )

    runner = _make_runner(room_store=room_store, binding_store=binding_store)
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.multiplex_profiles = False

    # Mock the router so we don't need real agent turns
    mock_router = MagicMock()
    mock_router.process_message = AsyncMock(return_value={
        "target_member": "client_svc",
        "reply": "test reply",
        "fenced_at": None,
    })
    runner._agent_room_router = mock_router

    result = await runner._process_message_via_room_if_bound(
        event=MagicMock(source=source),
        source=source,
        message_text="help",
        history=[],
    )

    mock_router.process_message.assert_awaited_once()
    # Non-None signals "handled" to _handle_message
    assert result is not None


@pytest.mark.asyncio
async def test_routing_error_falls_through(tmp_path):
    """If the router raises, _process_message_via_room_if_bound returns
    None so _handle_message continues with the normal agent path."""
    from gateway.agent_room_store import AgentRoomStore
    from gateway.source_agent_binding import SourceAgentBindingStore
    from gateway.session import SessionSource, build_source_binding_key
    from gateway.config import Platform

    room_store = AgentRoomStore(tmp_path / "rooms.sqlite")
    binding_store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    room = room_store.create_room(
        "r1", "Support", observer_profile="obs", members=["a"],
    )
    source = SessionSource(
        platform=Platform.DINGTALK, chat_id="cid-1", chat_type="group", user_id="u1",
    )
    binding_store.set_binding(
        build_source_binding_key(source),
        "obs",
        fallback_extra={"room_id": room.room_id},
    )

    runner = _make_runner(room_store=room_store, binding_store=binding_store)
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.multiplex_profiles = False

    mock_router = MagicMock()
    mock_router.process_message = AsyncMock(side_effect=RuntimeError("obs unavailable"))
    runner._agent_room_router = mock_router

    result = await runner._process_message_via_room_if_bound(
        event=MagicMock(source=source),
        source=source,
        message_text="hello",
        history=[],
    )
    # Error swallowed → fallthrough → normal agent path continues
    assert result is None
