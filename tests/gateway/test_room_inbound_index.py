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


def test_index_tracks_inbound_subscriptions_and_routes_bound_chats(db, monkeypatch):
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
