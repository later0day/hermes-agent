"""Tests for gateway/agent_room_messages_store.py — M3.2."""

from __future__ import annotations

import threading

import pytest

from gateway.agent_room_messages_store import AgentRoomMessagesStore, RoomMessage


@pytest.fixture
def store(tmp_path):
    s = AgentRoomMessagesStore(tmp_path / "msgs.sqlite")
    yield s
    s.close()


def test_append_returns_row_with_sequence(store):
    m = store.append(
        "room1", sender_kind="user", sender_name="alice", content="hi"
    )
    assert m.room_id == "room1"
    assert m.sequence == 1
    assert m.sender_kind == "user"
    assert m.content == "hi"
    assert m.timestamp > 0


def test_sequence_is_monotonic_per_room(store):
    for i in range(5):
        m = store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
        assert m.sequence == i + 1


def test_sequence_is_per_room_independent(store):
    store.append("A", sender_kind="user", sender_name="a", content="x")
    store.append("B", sender_kind="user", sender_name="b", content="y")
    m = store.append("A", sender_kind="user", sender_name="a", content="z")
    assert m.sequence == 2  # A has 2 messages
    m2 = store.append("B", sender_kind="user", sender_name="b", content="q")
    assert m2.sequence == 2  # B independently at 2


def test_list_messages_in_sequence_order(store):
    for i in range(3):
        store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
    msgs = store.list_messages("r1")
    assert len(msgs) == 3
    assert [m.content for m in msgs] == ["m0", "m1", "m2"]
    assert [m.sequence for m in msgs] == [1, 2, 3]


def test_list_messages_since_seq(store):
    for i in range(5):
        store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
    msgs = store.list_messages("r1", since_seq=2)
    assert len(msgs) == 3
    assert msgs[0].sequence == 3


def test_list_messages_limit(store):
    for i in range(5):
        store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
    msgs = store.list_messages("r1", limit=2)
    assert len(msgs) == 2


def test_tool_calls_roundtrip(store):
    tc = [{"id": "1", "function": {"name": "x", "arguments": "{}"}}]
    m = store.append(
        "r1", sender_kind="observer", sender_name="obs",
        content="", tool_calls=tc,
    )
    msgs = store.list_messages("r1")
    assert msgs[0].tool_calls == tc


def test_tool_result_stored(store):
    m = store.append(
        "r1", sender_kind="tool_result", sender_name="route_to_member",
        content='{"member":"x"}', tool_call_id="call_1",
    )
    msgs = store.list_messages("r1")
    assert msgs[0].tool_call_id == "call_1"
    assert msgs[0].sender_kind == "tool_result"


def test_count(store):
    assert store.count("r1") == 0
    store.append("r1", sender_kind="user", sender_name="a", content="x")
    store.append("r1", sender_kind="user", sender_name="a", content="y")
    assert store.count("r1") == 2


def test_max_sequence(store):
    # Empty room → version 0 (Raft AX held-draft snapshot marker).
    assert store.max_sequence("r1") == 0
    store.append("r1", sender_kind="user", sender_name="a", content="x")
    m2 = store.append("r1", sender_kind="user", sender_name="a", content="y")
    assert store.max_sequence("r1") == m2.sequence == 2
    # Independent per room.
    store.append("r2", sender_kind="user", sender_name="a", content="z")
    assert store.max_sequence("r2") == 1
    # Deleting the tail row lowers the version.
    store.delete_message("r1", m2.sequence)
    assert store.max_sequence("r1") == 1


def test_delete_room_clears_all(store):
    for i in range(3):
        store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
    n = store.delete_room("r1")
    assert n == 3
    assert store.count("r1") == 0
    # seq resets — new append gets seq=1 again
    m = store.append("r1", sender_kind="user", sender_name="a", content="fresh")
    assert m.sequence == 1


def test_delete_room_idempotent(store):
    assert store.delete_room("nonexistent") == 0


def test_invalid_sender_kind_rejected(store):
    with pytest.raises(ValueError):
        store.append("r1", sender_kind="ghost", sender_name="x", content="")


def test_empty_room_id_rejected(store):
    with pytest.raises(ValueError):
        store.append("", sender_kind="user", sender_name="x", content="hi")


def test_concurrent_appends_produce_unique_sequences(store):
    """M3-B2 hint: two threads appending simultaneously must not collide."""
    errors = []
    results = []

    def append_100(name):
        try:
            for _ in range(100):
                m = store.append("r1", sender_kind="user", sender_name=name, content=name)
                results.append(m.sequence)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=append_100, args=("t1",))
    t2 = threading.Thread(target=append_100, args=("t2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"errors: {errors}"
    assert len(results) == 200
    assert len(set(results)) == 200, "sequences must be unique"
    assert min(results) == 1
    assert max(results) == 200


def test_all_kinds_supported(store):
    for kind, name in [
        ("user", "alice"),
        ("observer", "room_x_observer"),
        ("member", "client_svc"),
        ("tool_result", "route_to_member"),
    ]:
        m = store.append("r1", sender_kind=kind, sender_name=name, content="x")
        assert m.sender_kind == kind
    msgs = store.list_messages("r1")
    kinds = [m.sender_kind for m in msgs]
    assert kinds == ["user", "observer", "member", "tool_result"]


def test_to_dict_shape(store):
    m = store.append("r1", sender_kind="user", sender_name="a", content="hi")
    d = m.to_dict()
    assert d["room_id"] == "r1"
    assert d["sequence"] == 1
    assert d["sender_kind"] == "user"
    assert "timestamp" in d


def test_delete_message_removes_single_row(store):
    for i in range(5):
        store.append("r1", sender_kind="user", sender_name="a", content=f"m{i}")
    # Delete the middle one (seq=3)
    assert store.delete_message("r1", 3) is True
    msgs = store.list_messages("r1")
    assert len(msgs) == 4
    assert [m.sequence for m in msgs] == [1, 2, 4, 5]


def test_delete_message_idempotent(store):
    store.append("r1", sender_kind="user", sender_name="a", content="x")
    assert store.delete_message("r1", 1) is True
    assert store.delete_message("r1", 1) is False  # already gone
    assert store.delete_message("r1", 999) is False  # never existed


def test_delete_message_does_not_affect_sequence_counter(store):
    store.append("r1", sender_kind="user", sender_name="a", content="1")
    store.append("r1", sender_kind="user", sender_name="a", content="2")
    store.delete_message("r1", 1)
    # Next append must still get seq=3, not seq=1
    m = store.append("r1", sender_kind="user", sender_name="a", content="3")
    assert m.sequence == 3
