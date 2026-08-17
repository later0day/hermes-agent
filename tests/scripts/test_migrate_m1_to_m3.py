"""Tests for scripts/migrate_m1_to_m3.py — M3.4.

Uses in-memory or tmp_path SQLite databases to simulate the M1 state.db
+ rooms.sqlite + messages.sqlite triad and exercise the migration
paths (dry-run count-only, actual write, orphan rooms, idempotence).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from scripts.migrate_m1_to_m3 import (
    MigrationCounts,
    _classify_message,
    migrate,
)


def _mk_state_db(path: Path, rows: list[dict]) -> None:
    """Build a minimal state.db with sessions + messages for testing."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            session_key TEXT,
            message_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL
        );
    """)
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO sessions(id, session_key, message_count) VALUES (?, ?, ?)",
            (row["session_id"], row["session_key"], 0),
        )
        conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_call_id, "
            "tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["session_id"], row["role"], row["content"], row.get("tool_call_id"),
             row.get("tool_calls"), row.get("tool_name"), row["timestamp"]),
        )
    conn.commit()
    conn.close()


def _mk_rooms_db(path: Path, room_ids: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE agent_rooms (
            room_id TEXT PRIMARY KEY,
            room_name TEXT,
            observer_profile TEXT,
            members_json TEXT,
            description TEXT,
            default_member TEXT,
            created_at REAL,
            updated_at REAL,
            actor TEXT
        )
    """)
    for rid in room_ids:
        conn.execute(
            "INSERT INTO agent_rooms(room_id, room_name, observer_profile, "
            "members_json, description, default_member, created_at, updated_at, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, "test", "obs", "[\"m1\"]", "", "m1", 0.0, 0.0, "test"),
        )
    conn.commit()
    conn.close()


# ─── _classify_message ─────────────────────────────────────────────────

def test_classify_observer_assistant():
    room_id, kind, name = _classify_message("room_observer:room_abc", "assistant")
    assert room_id == "room_abc"
    assert kind == "observer"


def test_classify_observer_user():
    room_id, kind, _ = _classify_message("room_observer:room_abc", "user")
    assert kind == "user"


def test_classify_observer_tool():
    room_id, kind, name = _classify_message("room_observer:room_abc", "tool")
    assert kind == "tool_result"
    assert name == "route_to_member"


def test_classify_member_assistant():
    room_id, kind, name = _classify_message(
        "room_member:room_abc:client_svc", "assistant"
    )
    assert kind == "member"
    assert name == "client_svc"


def test_classify_member_user():
    _, kind, _ = _classify_message("room_member:room_abc:client_svc", "user")
    assert kind == "user"


def test_classify_non_room_session_is_skip():
    room_id, kind, name = _classify_message("cli:xyz", "assistant")
    assert kind == "skip"


def test_classify_system_role_is_skip():
    _, kind, _ = _classify_message("room_observer:x", "system")
    assert kind == "skip"


# ─── migrate() ──────────────────────────────────────────────────────────

def test_dry_run_no_write(tmp_path):
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, ["room_1"])
    _mk_state_db(state_db, [
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "user", "content": "hi", "timestamp": 1.0},
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "assistant", "content": "routing", "timestamp": 2.0},
        {"session_id": "s2", "session_key": "room_member:room_1:m1",
         "role": "assistant", "content": "reply", "timestamp": 3.0},
    ])

    counts = migrate(
        state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=True,
    )
    assert counts.messages_migrated == 3
    assert counts.observer_rows == 1
    assert counts.member_rows == 1
    assert counts.user_rows == 1
    # No write in dry-run — target file may or may not exist but should be empty
    if msgs_db.exists():
        conn = sqlite3.connect(str(msgs_db))
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_room_messages'"
            ).fetchall()
            if tables:
                cnt = conn.execute("SELECT COUNT(*) FROM agent_room_messages").fetchone()[0]
                assert cnt == 0
        finally:
            conn.close()


def test_actual_write_creates_ordered_rows(tmp_path):
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, ["room_1"])
    _mk_state_db(state_db, [
        # Insert out-of-order timestamps — migration should sort them
        {"session_id": "s2", "session_key": "room_member:room_1:m1",
         "role": "assistant", "content": "reply", "timestamp": 3.0},
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "user", "content": "hi", "timestamp": 1.0},
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "assistant", "content": "routing", "timestamp": 2.0},
    ])

    # Initialize target schema
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    _ = AgentRoomMessagesStore(msgs_db); _.close()

    counts = migrate(
        state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=False,
    )
    assert counts.messages_migrated == 3

    # Verify canonical ordering
    conn = sqlite3.connect(str(msgs_db))
    try:
        rows = conn.execute(
            "SELECT sequence, sender_kind, content FROM agent_room_messages "
            "WHERE room_id='room_1' ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[0][2] == "hi"
    assert rows[1][2] == "routing"
    assert rows[2][2] == "reply"


def test_idempotent_rerun(tmp_path):
    """M3-B5: running the migration twice yields the same state — no
    duplicate rows accumulate."""
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, ["room_1"])
    _mk_state_db(state_db, [
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "user", "content": "hi", "timestamp": 1.0},
        {"session_id": "s2", "session_key": "room_member:room_1:m1",
         "role": "assistant", "content": "reply", "timestamp": 2.0},
    ])
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    _ = AgentRoomMessagesStore(msgs_db); _.close()

    migrate(state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=False)
    conn = sqlite3.connect(str(msgs_db))
    first_count = conn.execute("SELECT COUNT(*) FROM agent_room_messages").fetchone()[0]
    conn.close()

    # Re-run
    migrate(state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=False)
    conn = sqlite3.connect(str(msgs_db))
    second_count = conn.execute("SELECT COUNT(*) FROM agent_room_messages").fetchone()[0]
    conn.close()

    assert first_count == second_count == 2


def test_orphan_room_is_skipped(tmp_path):
    """A session key referring to a room_id NOT in agent_rooms is
    reported as orphan and not migrated."""
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, ["room_kept"])  # only room_kept exists
    _mk_state_db(state_db, [
        {"session_id": "s1", "session_key": "room_observer:room_kept",
         "role": "assistant", "content": "kept", "timestamp": 1.0},
        {"session_id": "s2", "session_key": "room_observer:room_orphan",
         "role": "assistant", "content": "orphan", "timestamp": 2.0},
    ])
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    _ = AgentRoomMessagesStore(msgs_db); _.close()

    counts = migrate(
        state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=False,
    )
    assert counts.rooms_orphaned == 1
    assert counts.observer_rows == 1  # only room_kept's row


def test_empty_state_db_no_op(tmp_path):
    """No state.db → no error, no migration."""
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"
    _mk_rooms_db(rooms_db, ["room_1"])

    counts = migrate(
        state_db=tmp_path / "nonexistent.db",
        rooms_db=rooms_db, messages_db=msgs_db, dry_run=True,
    )
    assert counts.messages_migrated == 0


def test_empty_rooms_db_no_op(tmp_path):
    """No known rooms → nothing to migrate even if state.db has data."""
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, [])  # empty
    _mk_state_db(state_db, [
        {"session_id": "s1", "session_key": "room_observer:room_x",
         "role": "assistant", "content": "x", "timestamp": 1.0},
    ])
    counts = migrate(
        state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=True,
    )
    assert counts.rooms_scanned == 0


def test_tool_result_row_migrated_with_call_id(tmp_path):
    """tool_call_id is preserved end-to-end."""
    state_db = tmp_path / "state.db"
    rooms_db = tmp_path / "rooms.sqlite"
    msgs_db = tmp_path / "msgs.sqlite"

    _mk_rooms_db(rooms_db, ["room_1"])
    _mk_state_db(state_db, [
        {"session_id": "s1", "session_key": "room_observer:room_1",
         "role": "tool", "content": '{"action":"route_to_member"}',
         "tool_call_id": "call_xyz", "timestamp": 1.0},
    ])
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    _ = AgentRoomMessagesStore(msgs_db); _.close()

    migrate(state_db=state_db, rooms_db=rooms_db, messages_db=msgs_db, dry_run=False)

    conn = sqlite3.connect(str(msgs_db))
    row = conn.execute(
        "SELECT sender_kind, sender_name, tool_call_id FROM agent_room_messages"
    ).fetchone()
    conn.close()
    assert row[0] == "tool_result"
    assert row[1] == "route_to_member"
    assert row[2] == "call_xyz"
