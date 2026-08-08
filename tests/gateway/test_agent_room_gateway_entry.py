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
