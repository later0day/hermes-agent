"""Tests for gateway/agent_room_held_store.py (Raft AX 改造 1 · held-draft).

Design ref: docs/design/agent-room/ax-alignment.md §3 改造 1.
"""

from __future__ import annotations

import pytest

from gateway.agent_room_held_store import (
    HELD_REASON_FENCED,
    HELD_REASON_NO_WEBHOOK,
    HELD_REASON_ROOM_MOVED,
    HELD_REASON_SEND_FAILED,
    RESOLUTION_REVISE,
    RESOLUTION_SEND_AS_IS,
    RESOLUTION_STAY_SILENT,
    STATUS_HELD,
    STATUS_RESOLVED,
    AgentRoomHeldStore,
)


@pytest.fixture()
def store(tmp_path):
    s = AgentRoomHeldStore(db_path=tmp_path / "held.sqlite")
    yield s
    s.close()


class TestHold:
    def test_hold_persists_and_returns_row_with_id(self, store):
        held = store.hold(
            "room1",
            session_id="room_member:room1:finance",
            member="finance",
            room_version=7,
            payload="the answer",
            held_reason=HELD_REASON_NO_WEBHOOK,
            chat_id="chat-abc",
        )
        assert held.id is not None
        assert held.room_id == "room1"
        assert held.member == "finance"
        assert held.room_version == 7
        assert held.payload == "the answer"
        assert held.held_reason == HELD_REASON_NO_WEBHOOK
        assert held.status == STATUS_HELD
        assert held.chat_id == "chat-abc"
        assert held.resolution is None
        assert held.created_at > 0

    def test_hold_roundtrips_via_get(self, store):
        held = store.hold(
            "room1", session_id="s", member="m", room_version=1,
            payload="p", held_reason=HELD_REASON_FENCED,
        )
        fetched = store.get(held.id)
        assert fetched is not None
        assert fetched.to_dict() == held.to_dict()

    def test_hold_stores_extra_json(self, store):
        held = store.hold(
            "room1", session_id="s", member="m", room_version=1,
            payload="p", held_reason=HELD_REASON_SEND_FAILED,
            extra={"attempt": 3, "err": "timeout"},
        )
        fetched = store.get(held.id)
        assert fetched.extra == {"attempt": 3, "err": "timeout"}

    def test_hold_accepts_room_moved_reason(self, store):
        # Raft AX 改造 1 (version-based hold): the turn-based-gap reason.
        held = store.hold(
            "room1", session_id="s", member="m", room_version=3,
            payload="reply to superseded question",
            held_reason=HELD_REASON_ROOM_MOVED,
        )
        assert store.get(held.id).held_reason == HELD_REASON_ROOM_MOVED

    def test_hold_rejects_unknown_reason(self, store):
        with pytest.raises(ValueError, match="invalid held_reason"):
            store.hold(
                "room1", session_id="s", member="m", room_version=1,
                payload="p", held_reason="bogus",
            )

    def test_hold_requires_room_and_member(self, store):
        with pytest.raises(ValueError):
            store.hold("", session_id="s", member="m", room_version=1,
                       payload="p", held_reason=HELD_REASON_FENCED)
        with pytest.raises(ValueError):
            store.hold("room1", session_id="s", member="", room_version=1,
                       payload="p", held_reason=HELD_REASON_FENCED)


class TestListHeld:
    def test_lists_only_held_by_default_oldest_first(self, store):
        a = store.hold("room1", session_id="s", member="a", room_version=1,
                       payload="pa", held_reason=HELD_REASON_FENCED)
        b = store.hold("room1", session_id="s", member="b", room_version=2,
                       payload="pb", held_reason=HELD_REASON_FENCED)
        store.resolve(a.id, RESOLUTION_STAY_SILENT)
        held = store.list_held("room1")
        assert [h.id for h in held] == [b.id]

    def test_include_resolved(self, store):
        a = store.hold("room1", session_id="s", member="a", room_version=1,
                       payload="pa", held_reason=HELD_REASON_FENCED)
        store.resolve(a.id, RESOLUTION_STAY_SILENT)
        assert store.list_held("room1") == []
        all_rows = store.list_held("room1", include_resolved=True)
        assert [h.id for h in all_rows] == [a.id]
        assert all_rows[0].status == STATUS_RESOLVED

    def test_filters_by_room(self, store):
        store.hold("room1", session_id="s", member="a", room_version=1,
                   payload="pa", held_reason=HELD_REASON_FENCED)
        store.hold("room2", session_id="s", member="b", room_version=1,
                   payload="pb", held_reason=HELD_REASON_FENCED)
        assert len(store.list_held("room1")) == 1
        assert len(store.list_held("room2")) == 1
        assert len(store.list_held()) == 2


class TestResolve:
    def test_resolve_transitions_and_records_path(self, store):
        held = store.hold("room1", session_id="s", member="m", room_version=1,
                          payload="p", held_reason=HELD_REASON_NO_WEBHOOK)
        assert store.resolve(held.id, RESOLUTION_SEND_AS_IS) is True
        fetched = store.get(held.id)
        assert fetched.status == STATUS_RESOLVED
        assert fetched.resolution == RESOLUTION_SEND_AS_IS
        assert fetched.resolved_at is not None

    def test_double_resolve_is_noop(self, store):
        held = store.hold("room1", session_id="s", member="m", room_version=1,
                          payload="p", held_reason=HELD_REASON_FENCED)
        assert store.resolve(held.id, RESOLUTION_REVISE) is True
        # second resolve must NOT re-transition (no re-delivery)
        assert store.resolve(held.id, RESOLUTION_SEND_AS_IS) is False
        assert store.get(held.id).resolution == RESOLUTION_REVISE

    def test_resolve_missing_id_returns_false(self, store):
        assert store.resolve(9999, RESOLUTION_STAY_SILENT) is False

    def test_resolve_rejects_unknown_path(self, store):
        held = store.hold("room1", session_id="s", member="m", room_version=1,
                          payload="p", held_reason=HELD_REASON_FENCED)
        with pytest.raises(ValueError, match="invalid resolution"):
            store.resolve(held.id, "bogus")


class TestDeleteRoom:
    def test_delete_room_removes_all_rows(self, store):
        store.hold("room1", session_id="s", member="a", room_version=1,
                   payload="pa", held_reason=HELD_REASON_FENCED)
        store.hold("room1", session_id="s", member="b", room_version=2,
                   payload="pb", held_reason=HELD_REASON_FENCED)
        store.hold("room2", session_id="s", member="c", room_version=1,
                   payload="pc", held_reason=HELD_REASON_FENCED)
        assert store.delete_room("room1") == 2
        assert store.list_held("room1", include_resolved=True) == []
        assert len(store.list_held("room2", include_resolved=True)) == 1


class TestPersistenceAcrossReopen:
    def test_held_survives_reopen(self, tmp_path):
        path = tmp_path / "held.sqlite"
        s1 = AgentRoomHeldStore(db_path=path)
        held = s1.hold("room1", session_id="s", member="m", room_version=5,
                       payload="survive me", held_reason=HELD_REASON_NO_WEBHOOK)
        s1.close()
        # Simulate a gateway restart — the held reply must still be there.
        s2 = AgentRoomHeldStore(db_path=path)
        rows = s2.list_held("room1")
        assert len(rows) == 1
        assert rows[0].payload == "survive me"
        assert rows[0].room_version == 5
        s2.close()
