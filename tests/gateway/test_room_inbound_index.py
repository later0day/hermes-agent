"""C2 inbound routing must not add I/O before ``_handle_message`` claims the session.

The inbound hook runs on every plain message ahead of the session claim, where upstream allows no awaits (a
second message in that window would start a duplicate agent). Unbound chats are therefore rejected from an
in-memory index; only an indexed (bound) source pays for the authoritative SQLite lookup.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway import hosted_rooms
from gateway import room_mirror_db as mir
from gateway.config import Platform
from gateway.room_mirror_watcher import INBOUND_ROOM_NOT_ROUTED, GatewayRoomMirrorMixin


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: path)
    return path


def _source(chat_id="123"):
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id=chat_id, thread_id="", user_id="u1")


def _event(text="hello"):
    return SimpleNamespace(text=text, message_id="m1")


def test_unbound_chat_is_rejected_from_memory_without_touching_sqlite(db, monkeypatch):
    lookups = []  # recorded, not raised: the hook deliberately swallows lookup errors (fail to normal dispatch)
    monkeypatch.setattr(mir, "inbound_binding_for_source", lambda *a, **k: lookups.append(k))
    bridge = GatewayRoomMirrorMixin()

    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source())) is INBOUND_ROOM_NOT_ROUTED
    assert lookups == [], "unbound chat must not reach the SQLite binding lookup"


def _room(db, room_id="room-1"):
    return hosted_rooms.create_room(db, room_id=room_id, name="R", authority_gateway_id="g", now=1, members=[
        {"member_id": f"m-{p}", "profile": p, "handle": p, "target": {"kind": "local", "profile": p}}
        for p in ("plan", "backend")])


def test_index_tracks_inbound_subscriptions_and_routes_bound_chats(db, monkeypatch):
    _room(db)
    mir.create_sub(db, room_id="room-1", platform="telegram", chat_id="123", thread_id="", inbound=True)
    mir.create_sub(db, room_id="room-1", platform="telegram", chat_id="999", thread_id="")  # mirror only
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()

    assert bridge._room_inbound_sources == frozenset({("telegram", "123", "")})

    sent = []
    service = SimpleNamespace(send=lambda **kw: sent.append(kw) or {"seq": 1})
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)

    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event("do it"), _source("123"))) is None
    assert sent and sent[0]["room_id"] == "room-1" and sent[0]["payload"]["text"] == "do it"
    # A mirror-only (no --inbound) chat never ingests.
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("999"))) is INBOUND_ROOM_NOT_ROUTED

    mir.remove_sub(db, room_id="room-1", platform="telegram", chat_id="123", thread_id="")
    bridge._reload_room_inbound_sources()
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("123"))) is INBOUND_ROOM_NOT_ROUTED


def test_routes_platform_ids_that_are_not_identifier_safe(db, monkeypatch):
    """DingTalk chat/message ids carry ``+ / =`` (WeChat ids an ``@``); routing must not reject them,
    and a redelivered message must keep the same room event id while a new message gets another."""
    chat_id = "cidUKHyy+TSBvzQY6P34TpjPA=="
    _room(db)
    mir.create_sub(db, room_id="room-1", platform="dingtalk", chat_id=chat_id, thread_id="", inbound=True)
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()
    sent = []

    def _send(*, room_id, event_id, payload, actor_id):
        # The real log append validates event_id and actor.id as identifiers.
        sent.append({"event_id": event_id, "actor_id": actor_id})
        return hosted_rooms.append_event(
            db, room_id=room_id, event_id=event_id, kind="message.user",
            actor={"kind": "user", "id": actor_id}, authority_gateway_id="g", authority_epoch=1,
            payload=payload, now=1)

    service = SimpleNamespace(send=_send)
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)
    source = SimpleNamespace(platform=Platform.DINGTALK, chat_id=chat_id, thread_id="",
                             user_id="$:LWCP_v1:$NoDPZEnaHdl5WfqKihxdt899LnbQ0x+0")

    for message_id in ("msgQY+tEb7fn/oGUZQorA", "msgQY+tEb7fn/oGUZQorA", "msgOther+id=="):
        event = SimpleNamespace(text="add mul", message_id=message_id)
        assert asyncio.run(bridge._maybe_route_inbound_to_room(event, source)) is None

    first, redelivered, other = (call["event_id"] for call in sent)
    assert first == redelivered != other
    assert len({call["actor_id"] for call in sent}) == 1  # one stable identity per IM user


def test_disbanding_a_room_hands_its_inbound_chat_back_to_the_agent(db):
    """A disbanded room must stop capturing its full-duplex chat at once (any disband entry point, any
    process): routing to it only answers "couldn't deliver" and walls the chat off from its agent."""
    room = _room(db)
    mir.create_sub(db, room_id="room-1", platform="telegram", chat_id="123", thread_id="", inbound=True)
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()
    assert bridge._room_inbound_sources == frozenset({("telegram", "123", "")})

    hosted_rooms.disband_room(db, room_id="room-1", expected_gateway_id=room["authority_gateway_id"],
                              expected_epoch=1)
    bridge._reload_room_inbound_sources()

    assert bridge._room_inbound_sources == frozenset()
    assert mir.inbound_binding_for_source(db, platform="telegram", chat_id="123") is None
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("123"))) is INBOUND_ROOM_NOT_ROUTED
