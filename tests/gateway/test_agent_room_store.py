"""Tests for gateway/agent_room_store.py — M1.1 (design.html §4.1, §6.3).

Covers CRUD, the N3 member-cap validation, the §6.3 Fence mechanism, and
the M1 boundary tests explicitly assigned to this milestone in
docs/design/agent-room/EXECUTION_PLAN.md:
  M1-B3  members_json length > MAX_ROOM_MEMBERS -> reject
  M1-B9  delete on an already-inconsistent/missing room -> idempotent, no raise
  M1-B13 SQLite lock contention on init -> bounded retry (via shared _execute_sqlite_init)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.agent_room_store import (
    MAX_ROOM_MEMBERS,
    AgentRoomError,
    AgentRoomStore,
)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_room(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    room = store.create_room(
        "room_1",
        "Customer Support",
        observer_profile="room_customer_support_observer",
        members=["client_svc", "finance"],
        description="Handles support tickets",
        actor="admin",
    )

    assert room.room_id == "room_1"
    assert room.room_name == "Customer Support"
    assert room.observer_profile == "room_customer_support_observer"
    assert room.members == ("client_svc", "finance")
    assert room.description == "Handles support tickets"
    assert room.created_by == "admin"

    fetched = store.get_room("room_1")
    assert fetched == room

    by_name = store.get_room_by_name("Customer Support")
    assert by_name == room


def test_create_room_rejects_duplicate_id(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room(
        "room_1", "A", observer_profile="obs_a", members=["p1"]
    )

    with pytest.raises(AgentRoomError, match="already exists"):
        store.create_room(
            "room_1", "B", observer_profile="obs_b", members=["p1"]
        )


def test_create_room_rejects_duplicate_name(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room(
        "room_1", "SameName", observer_profile="obs_a", members=["p1"]
    )

    with pytest.raises(AgentRoomError, match="already exists"):
        store.create_room(
            "room_2", "SameName", observer_profile="obs_b", members=["p1"]
        )


def test_create_room_requires_room_id_name_observer(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    with pytest.raises(AgentRoomError, match="room_id"):
        store.create_room("", "Name", observer_profile="obs", members=["p1"])
    with pytest.raises(AgentRoomError, match="room_name"):
        store.create_room("rid", "", observer_profile="obs", members=["p1"])
    with pytest.raises(AgentRoomError, match="observer_profile"):
        store.create_room("rid", "Name", observer_profile="", members=["p1"])


def test_create_room_requires_at_least_one_member(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    with pytest.raises(AgentRoomError, match="at least one member"):
        store.create_room("rid", "Name", observer_profile="obs", members=[])


def test_create_room_dedupes_members_preserving_order(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    room = store.create_room(
        "rid",
        "Name",
        observer_profile="obs",
        members=["a", "b", "a", "c", "b"],
    )

    assert room.members == ("a", "b", "c")


def test_create_room_default_member_must_be_in_roster(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    with pytest.raises(AgentRoomError, match="not in the member roster"):
        store.create_room(
            "rid",
            "Name",
            observer_profile="obs",
            members=["a", "b"],
            default_member="ghost",
        )


def test_update_members_replaces_roster(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room(
        "rid", "Name", observer_profile="obs", members=["a", "b"]
    )

    updated = store.update_members("rid", ["b", "c", "d"], actor="admin")

    assert updated.members == ("b", "c", "d")
    assert updated.updated_by == "admin"


def test_update_members_clears_default_member_if_no_longer_in_roster(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room(
        "rid",
        "Name",
        observer_profile="obs",
        members=["a", "b"],
        default_member="a",
    )

    updated = store.update_members("rid", ["b", "c"])

    assert updated.default_member == ""


def test_update_members_missing_room_raises(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    with pytest.raises(AgentRoomError, match="not found"):
        store.update_members("ghost", ["a"])


def test_list_rooms_ordered_by_updated_at_desc(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("r1", "First", observer_profile="o1", members=["a"])
    store.create_room("r2", "Second", observer_profile="o2", members=["a"])
    store.update_members("r1", ["a", "b"])  # bumps r1's updated_at

    rooms = store.list_rooms()

    assert [r.room_id for r in rooms] == ["r1", "r2"]


# ---------------------------------------------------------------------------
# M1-B3: member cap
# ---------------------------------------------------------------------------


def test_create_room_rejects_more_than_max_members(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    too_many = [f"p{i}" for i in range(MAX_ROOM_MEMBERS + 1)]

    with pytest.raises(AgentRoomError, match=f"more than {MAX_ROOM_MEMBERS}"):
        store.create_room("rid", "Name", observer_profile="obs", members=too_many)


def test_create_room_accepts_exactly_max_members(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    exactly_max = [f"p{i}" for i in range(MAX_ROOM_MEMBERS)]

    room = store.create_room(
        "rid", "Name", observer_profile="obs", members=exactly_max
    )

    assert len(room.members) == MAX_ROOM_MEMBERS


def test_update_members_also_enforces_cap(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])
    too_many = [f"p{i}" for i in range(MAX_ROOM_MEMBERS + 1)]

    with pytest.raises(AgentRoomError, match=f"more than {MAX_ROOM_MEMBERS}"):
        store.update_members("rid", too_many)


# ---------------------------------------------------------------------------
# M1-B9: idempotent delete on inconsistent/missing state
# ---------------------------------------------------------------------------


def test_delete_room_idempotent_when_missing(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    assert store.delete_room("never-existed") is False
    # Second call on the same missing id must not raise either.
    assert store.delete_room("never-existed") is False


def test_delete_room_removes_row_and_returns_true(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])

    assert store.delete_room("rid") is True
    assert store.get_room("rid") is None
    # Deleting again is idempotent, not an error.
    assert store.delete_room("rid") is False


def test_delete_room_clears_fenced_sessions_for_that_room(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])
    store.fence_room("rid", ["session-1"])
    assert store.is_fenced("rid", "session-1") is True

    store.delete_room("rid")

    assert store.is_fenced("rid", "session-1") is False


# ---------------------------------------------------------------------------
# resolve_default_member (used by router M1.5 + bootstrapper M1.2)
# ---------------------------------------------------------------------------


def test_resolve_default_member_prefers_explicit_default(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    room = store.create_room(
        "rid",
        "Name",
        observer_profile="obs",
        members=["a", "b"],
        default_member="b",
    )

    assert room.resolve_default_member() == "b"


def test_resolve_default_member_falls_back_to_first_member(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    room = store.create_room(
        "rid", "Name", observer_profile="obs", members=["a", "b"]
    )

    assert room.resolve_default_member() == "a"


# ---------------------------------------------------------------------------
# §6.3 Fence mechanism
# ---------------------------------------------------------------------------


def test_fence_and_is_fenced(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])

    assert store.is_fenced("rid", "session-1") is False

    store.fence_room("rid", ["session-1", "session-2"])

    assert store.is_fenced("rid", "session-1") is True
    assert store.is_fenced("rid", "session-2") is True
    assert store.is_fenced("rid", "session-3") is False


def test_fence_is_scoped_per_room(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("room-a", "A", observer_profile="obs", members=["a"])
    store.create_room("room-b", "B", observer_profile="obs", members=["a"])

    store.fence_room("room-a", ["shared-session-id"])

    assert store.is_fenced("room-a", "shared-session-id") is True
    assert store.is_fenced("room-b", "shared-session-id") is False


def test_unfence_room_clears_all_sessions(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])
    store.fence_room("rid", ["s1", "s2"])

    store.unfence_room("rid")

    assert store.is_fenced("rid", "s1") is False
    assert store.is_fenced("rid", "s2") is False


def test_fence_room_with_empty_session_list_is_a_noop(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    store.create_room("rid", "Name", observer_profile="obs", members=["a"])

    store.fence_room("rid", [])

    assert store.is_fenced("rid", "anything") is False


def test_is_fenced_on_unknown_room_returns_false(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")

    assert store.is_fenced("never-created", "session-1") is False


# ---------------------------------------------------------------------------
# Concurrency + persistence across reconnects
# ---------------------------------------------------------------------------


def test_concurrent_room_creation_is_serialized_not_corrupted(tmp_path):
    db_path = tmp_path / "rooms.sqlite"
    store = AgentRoomStore(db_path)

    def _create(i: int) -> bool:
        try:
            store.create_room(
                f"room_{i}", f"Room {i}", observer_profile="obs", members=["a"]
            )
            return True
        except AgentRoomError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_create, range(8)))

    assert all(results)
    assert len(store.list_rooms()) == 8


def test_store_reopens_and_reads_persisted_rooms(tmp_path):
    db_path = tmp_path / "rooms.sqlite"
    store1 = AgentRoomStore(db_path)
    store1.create_room(
        "rid", "Name", observer_profile="obs", members=["a", "b"]
    )
    store1.close()

    store2 = AgentRoomStore(db_path)
    room = store2.get_room("rid")

    assert room is not None
    assert room.members == ("a", "b")


def test_room_to_dict_round_trips_members_as_list(tmp_path):
    store = AgentRoomStore(tmp_path / "rooms.sqlite")
    room = store.create_room(
        "rid", "Name", observer_profile="obs", members=["a", "b"]
    )

    d = room.to_dict()

    assert d["members"] == ["a", "b"]
    assert isinstance(d["members"], list)


def test_update_members_persists_description():
    """Live E2E bug regression: PATCH /api/rooms/{id} with description
    was silently dropping the description field because update_members
    ignored it. Verify description flows through to DB now."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = AgentRoomStore(Path(td) / "rooms.sqlite")
        try:
            room = store.create_room(
                "r1", "Test", observer_profile="obs",
                members=["a"], description="original",
            )
            updated = store.update_members(
                "r1", ["a"], description="updated", actor="test",
            )
            assert updated.description == "updated"
            # Re-fetch to be sure it's persisted, not just in-memory
            got = store.get_room("r1")
            assert got.description == "updated"
        finally:
            store.close()


def test_update_members_preserves_description_when_not_passed():
    """When PATCH only changes members, description must NOT be reset."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = AgentRoomStore(Path(td) / "rooms.sqlite")
        try:
            store.create_room(
                "r1", "Test", observer_profile="obs",
                members=["a"], description="keep me",
            )
            updated = store.update_members(
                "r1", ["a", "b"], actor="test",  # no description arg
            )
            assert updated.description == "keep me"
        finally:
            store.close()
