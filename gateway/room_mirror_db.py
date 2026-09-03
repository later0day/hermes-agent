"""Durable room→chat mirror subscriptions (C2 outbound, read-only mirror).

A self-contained sidecar store that lets a hosted room's member messages be
pushed into an IM chat group. It deliberately does NOT touch ``hosted_rooms``:
it opens the same ``state.db`` file, creates its own ``room_notify_subs`` table
with ``CREATE TABLE IF NOT EXISTS`` (``hosted_rooms._schema_is_current`` uses
subset checks and ignores extra tables), and only ever *reads* the room event
log via :func:`gateway.hosted_rooms.read_events`.

The cursor discipline is a direct clone of the battle-tested kanban notifier
(``hermes_cli.kanban_db.claim_unseen_events_for_sub`` /
``advance_notify_cursor`` / ``rewind_notify_cursor``): claim advances the cursor
under ``BEGIN IMMEDIATE`` so concurrent gateway watchers serialize on SQLite's
writer lock and only one process delivers a given event range; a failed send
rewinds via CAS so the cursor never skips an undelivered message.

``member_filter`` is the decider's "single external voice" enforcement point:
NULL mirrors every ``message.member``; a member_id mirrors only that member's
turns (point it at the decider member_id to expose one voice to the group).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from gateway import hosted_rooms

# The only room event kind a read-only mirror ever forwards. message.user is the
# room's inbound side (a human or the desktop), so never mirroring it makes the
# inbound/outbound echo loop structurally impossible even once C2 slice 2 lands.
MIRRORED_EVENT_KIND = "message.member"

_JOURNAL_MODE_LOCK_RETRIES = 6

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS room_notify_subs (
    room_id        TEXT NOT NULL,
    platform       TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    thread_id      TEXT NOT NULL DEFAULT '',
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    member_filter  TEXT,
    notifier_profile TEXT,
    delivery_metadata TEXT,
    created_at     REAL NOT NULL,
    PRIMARY KEY (room_id, platform, chat_id, thread_id)
)
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_room_notify_room ON room_notify_subs(room_id)
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    from hermes_state import apply_wal_with_fallback

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for attempt in range(_JOURNAL_MODE_LOCK_RETRIES):
            try:
                apply_wal_with_fallback(conn, db_label="state.db (room_mirror)")
                break
            except sqlite3.OperationalError as exc:
                if (
                    str(exc).lower() != "database is locked"
                    or attempt + 1 == _JOURNAL_MODE_LOCK_RETRIES
                ):
                    raise
                time.sleep(0.01 * (2**attempt))
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


@contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def create_sub(
    db_path: Path | str,
    *,
    room_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    member_filter: str | None = None,
    notifier_profile: str | None = None,
    delivery_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a room→chat mirror subscription (idempotent).

    A duplicate ``(room_id, platform, chat_id, thread_id)`` refreshes the
    ``member_filter`` / routing anchor without rewinding ``last_event_seq`` so
    re-running the command never replays already-delivered messages.
    """

    metadata_json = (
        json.dumps(delivery_metadata, sort_keys=True, separators=(",", ":"))
        if delivery_metadata
        else None
    )
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            conn.execute(
                """
                INSERT INTO room_notify_subs (
                    room_id, platform, chat_id, thread_id,
                    last_event_seq, member_filter, notifier_profile,
                    delivery_metadata, created_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(room_id, platform, chat_id, thread_id) DO UPDATE SET
                    member_filter = excluded.member_filter,
                    notifier_profile = COALESCE(
                        excluded.notifier_profile, room_notify_subs.notifier_profile
                    ),
                    delivery_metadata = COALESCE(
                        excluded.delivery_metadata, room_notify_subs.delivery_metadata
                    )
                """,
                (
                    room_id, platform, chat_id, thread_id or "",
                    member_filter, notifier_profile, metadata_json, time.time(),
                ),
            )
            row = conn.execute(
                """SELECT * FROM room_notify_subs
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?""",
                (room_id, platform, chat_id, thread_id or ""),
            ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_subs(db_path: Path | str) -> list[dict[str, Any]]:
    """Return every mirror subscription across all rooms."""

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM room_notify_subs ORDER BY room_id, platform, chat_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def remove_sub(
    db_path: Path | str,
    *,
    room_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
) -> bool:
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            cur = conn.execute(
                """DELETE FROM room_notify_subs
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?""",
                (room_id, platform, chat_id, thread_id or ""),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def _passes_filter(event: dict[str, Any], member_filter: str | None) -> bool:
    if event.get("kind") != MIRRORED_EVENT_KIND:
        return False
    if member_filter is None:
        return True
    payload = event.get("payload") or {}
    return str(payload.get("member_id") or "") == member_filter


def claim_unseen_room_events(
    db_path: Path | str,
    *,
    room_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
) -> tuple[int, int, list[dict[str, Any]]]:
    """Atomically claim mirrorable room events newer than the stored cursor.

    Returns ``(old_cursor, new_cursor, events)``. Reads the room log via
    ``hosted_rooms.read_events`` (bounded monotonic delta), filters to
    ``message.member`` matching ``member_filter``, then advances
    ``last_event_seq`` to the highest *scanned* seq under ``BEGIN IMMEDIATE``
    — advancing past filtered-out rows too so a room that only emits
    non-matching members never re-scans them every tick. Concurrent watchers
    serialize on the writer lock; only the first sees a given range.
    """

    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            row = conn.execute(
                """SELECT last_event_seq, member_filter FROM room_notify_subs
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?""",
                (room_id, platform, chat_id, thread_id or ""),
            ).fetchone()
            if row is None:
                return 0, 0, []
            old_cursor = int(row["last_event_seq"])
            member_filter = row["member_filter"]
            try:
                page = hosted_rooms.read_events(
                    db_path,
                    room_id=room_id,
                    since_seq=old_cursor,
                    limit=hosted_rooms.MAX_LOG_LIMIT,
                )
            except hosted_rooms.HostedRoomError:
                return old_cursor, old_cursor, []
            scanned = page.get("events") or []
            if not scanned:
                return old_cursor, old_cursor, []
            new_cursor = int(scanned[-1]["seq"])
            matched = [
                dict(event)
                for event in scanned
                if _passes_filter(event, member_filter)
            ]
            conn.execute(
                """UPDATE room_notify_subs SET last_event_seq=?
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?
                     AND last_event_seq=?""",
                (
                    new_cursor, room_id, platform, chat_id, thread_id or "",
                    old_cursor,
                ),
            )
        return old_cursor, new_cursor, matched
    finally:
        conn.close()


def advance_cursor(
    db_path: Path | str,
    *,
    room_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    new_cursor: int,
) -> None:
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            conn.execute(
                """UPDATE room_notify_subs SET last_event_seq=?
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?""",
                (int(new_cursor), room_id, platform, chat_id, thread_id or ""),
            )
    finally:
        conn.close()


def rewind_cursor(
    db_path: Path | str,
    *,
    room_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a claim after a failed send. CAS-guarded: only rewinds if no later
    watcher advanced the row past our claim."""

    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            cur = conn.execute(
                """UPDATE room_notify_subs SET last_event_seq=?
                   WHERE room_id=? AND platform=? AND chat_id=? AND thread_id=?
                     AND last_event_seq=?""",
                (
                    int(old_cursor), room_id, platform, chat_id, thread_id or "",
                    int(claimed_cursor),
                ),
            )
        return cur.rowcount > 0
    finally:
        conn.close()
