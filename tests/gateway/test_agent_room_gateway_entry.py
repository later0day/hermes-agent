"""Behavioral tests for the gateway's current inbound hosted-room gate."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.room_mirror_watcher import INBOUND_ROOM_NOT_ROUTED
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id="cid-room-1",
        chat_type="group",
        user_id="user-1",
        thread_id="thread-1",
    )


def _event(text: str = "hello room") -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id="message-1")


def test_unbound_source_falls_through_to_normal_agent_dispatch():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._maybe_route_inbound_to_room = AsyncMock(return_value=INBOUND_ROOM_NOT_ROUTED)
    event = _event()

    handled, reply = asyncio.run(runner._hm_route_inbound_room(event, event.source, False))

    assert (handled, reply) == (False, None)
    runner._maybe_route_inbound_to_room.assert_awaited_once_with(event, event.source)


def test_successful_room_ingest_is_handled_with_a_silent_reply():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._maybe_route_inbound_to_room = AsyncMock(return_value=None)
    event = _event()

    handled, reply = asyncio.run(runner._hm_route_inbound_room(event, event.source, False))

    assert (handled, reply) == (True, None)


def test_room_delivery_failure_is_handled_without_falling_back_to_local_agent():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._maybe_route_inbound_to_room = AsyncMock(return_value="Room delivery failed; please retry")
    event = _event()

    handled, reply = asyncio.run(runner._hm_route_inbound_room(event, event.source, False))

    assert handled is True
    assert reply == "Room delivery failed; please retry"


@pytest.mark.parametrize(
    ("text", "is_internal"),
    [
        ("hello room", True),
        ("/status", False),
        ("   \n\t", False),
    ],
    ids=["internal-event", "slash-command", "blank-message"],
)
def test_non_plain_external_messages_bypass_room_routing(text, is_internal):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._maybe_route_inbound_to_room = AsyncMock()
    event = _event(text)

    handled, reply = asyncio.run(runner._hm_route_inbound_room(event, event.source, is_internal))

    assert (handled, reply) == (False, None)
    runner._maybe_route_inbound_to_room.assert_not_awaited()
