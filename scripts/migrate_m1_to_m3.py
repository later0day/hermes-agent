"""M3.4 · Migrate M1 per-member hermes sessions into M3 shared messages.

Design ref: docs/design/agent-room/design.html §2.4 + §10.4.

Under M1, each room member's message history lives in its own hermes
session (session_key=room_member:{room_id}:{member}). The observer's
routing history lives in room_observer:{room_id}. Under M3, the
authoritative history is the shared ``agent_room_messages`` table.

Spike 1 finding (M3 pre-work): live gateway didn't persist room turns
under a stable session_key (it called _run_agent_inner with
session_key=None), so state.db currently contains NO room_* sessions
to migrate. This script is therefore both:

  (a) A safety net for any state.db that DID accumulate room_ session
      rows (from earlier gateway versions or manual test setups).
  (b) A dry-run-first tool: --dry-run mode counts what would migrate
      without writing anything. This is the workflow the design doc's
      §10.4 pre-work step 1 requires.

Usage:
    python -m scripts.migrate_m1_to_m3 --dry-run
    python -m scripts.migrate_m1_to_m3          # actual write

Guarantees:
  * Idempotent: re-running after success is a no-op (deletes+reinserts
    per room, so partial state from a previous crash gets healed).
  * Ordered: canonical sequence is derived from state.db's per-message
    timestamp, then row_id as tiebreaker.
  * Rollback-safe: writes are per-room-transactional; a crash halfway
    through leaves earlier rooms migrated, later rooms untouched.
  * Room-scope: only migrates rooms that currently exist in
    ``agent_rooms`` (the M1 store). Orphaned sessions are logged and
    skipped.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("migrate_m1_to_m3")


ROOM_MEMBER_RE = re.compile(r"^room_member:(?P<room_id>[^:]+):(?P<member>.+)$")
ROOM_OBSERVER_RE = re.compile(r"^room_observer:(?P<room_id>[^:]+)$")


@dataclass(frozen=True)
class MigrationCounts:
    rooms_scanned: int = 0
    rooms_skipped_no_data: int = 0
    rooms_orphaned: int = 0
    messages_migrated: int = 0
    observer_rows: int = 0
    member_rows: int = 0
    tool_result_rows: int = 0
    user_rows: int = 0


def _iter_room_sessions(state_db: Path) -> Iterable[dict]:
    """Yield state.db session rows whose session_key matches the room_*
    conventions. Read-only; the caller decides what to do with them."""
    if not state_db.exists():
        logger.info("state.db not found at %s — nothing to migrate", state_db)
        return

    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        # sessions.session_key IS the room_* string in M1
        rows = conn.execute(
            "SELECT id, session_key, message_count FROM sessions "
            "WHERE session_key LIKE 'room_observer:%' "
            "   OR session_key LIKE 'room_member:%' "
            "ORDER BY session_key"
        ).fetchall()
        for r in rows:
            yield dict(r)
    finally:
        conn.close()


def _iter_session_messages(state_db: Path, session_id: str) -> Iterable[dict]:
    """Read messages for one session in insertion order."""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp "
            "FROM messages WHERE session_id = ? "
            "ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        for r in rows:
            yield dict(r)
    finally:
        conn.close()


def _classify_message(session_key: str, role: str) -> tuple[str, str, str]:
    """Return (room_id, sender_kind, sender_name) for one legacy row.

    role is the state.db messages.role column (system/user/assistant/tool).
    """
    m_obs = ROOM_OBSERVER_RE.match(session_key)
    if m_obs:
        room_id = m_obs.group("room_id")
        if role == "user":
            # A user turn under an observer session was the incoming
            # DingTalk message — categorize as user with unknown name.
            return room_id, "user", "user"
        if role == "assistant":
            return room_id, "observer", session_key
        if role == "tool":
            return room_id, "tool_result", "route_to_member"
        # system/other: skip
        return room_id, "skip", ""

    m_mem = ROOM_MEMBER_RE.match(session_key)
    if m_mem:
        room_id = m_mem.group("room_id")
        member = m_mem.group("member")
        if role == "user":
            return room_id, "user", "user"
        if role == "assistant":
            return room_id, "member", member
        if role == "tool":
            return room_id, "tool_result", member
        return room_id, "skip", ""

    return "", "skip", ""


def _known_room_ids(rooms_db: Path) -> set[str]:
    """Return the set of room_ids that currently exist in agent_rooms.
    Sessions referring to a room_id NOT in this set are orphans."""
    if not rooms_db.exists():
        return set()
    conn = sqlite3.connect(str(rooms_db))
    try:
        rows = conn.execute("SELECT room_id FROM agent_rooms").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def migrate(
    *,
    state_db: Path,
    rooms_db: Path,
    messages_db: Path,
    dry_run: bool,
) -> MigrationCounts:
    """Main migration entry point.

    dry_run=True walks the data and reports counts without writing.
    """
    known_rooms = _known_room_ids(rooms_db)
    if not known_rooms:
        logger.warning(
            "no rooms found in %s — nothing to migrate", rooms_db,
        )
        return MigrationCounts()

    # Group state.db sessions by room_id
    per_room: dict[str, list[dict]] = {}
    for sess in _iter_room_sessions(state_db):
        key = sess["session_key"]
        m_obs = ROOM_OBSERVER_RE.match(key)
        m_mem = ROOM_MEMBER_RE.match(key)
        room_id = (m_obs and m_obs.group("room_id")) or (m_mem and m_mem.group("room_id"))
        if not room_id:
            continue
        per_room.setdefault(room_id, []).append(sess)

    counts = MigrationCounts(
        rooms_scanned=len(per_room),
        rooms_skipped_no_data=0,
        rooms_orphaned=0,
    )
    orphan_count = 0
    total_msgs = 0
    obs_count = 0
    mem_count = 0
    tool_count = 0
    user_count = 0

    # Open the target store only in write mode
    target_conn = None
    if not dry_run:
        target_conn = sqlite3.connect(str(messages_db))
        target_conn.execute("PRAGMA journal_mode=WAL")

    try:
        for room_id, sessions in per_room.items():
            if room_id not in known_rooms:
                orphan_count += 1
                logger.info(
                    "orphan room_id %s (session_keys: %s) — skipping",
                    room_id,
                    [s["session_key"] for s in sessions],
                )
                continue

            # Collect every message across every session for this room,
            # sorted by (timestamp, session_id, row_id)
            all_rows: list[tuple[float, int, str, dict]] = []
            for sess in sessions:
                for msg in _iter_session_messages(state_db, sess["id"]):
                    all_rows.append(
                        (float(msg["timestamp"] or 0.0), int(msg["id"]),
                         sess["session_key"], msg)
                    )

            if not all_rows:
                counts = MigrationCounts(
                    **{**counts.__dict__, "rooms_skipped_no_data": counts.rooms_skipped_no_data + 1}
                )
                continue

            all_rows.sort(key=lambda t: (t[0], t[1]))

            if dry_run:
                # Just count what would be inserted
                for ts, rowid, skey, msg in all_rows:
                    _, kind, _ = _classify_message(skey, msg["role"])
                    if kind == "skip":
                        continue
                    total_msgs += 1
                    if kind == "observer":
                        obs_count += 1
                    elif kind == "member":
                        mem_count += 1
                    elif kind == "tool_result":
                        tool_count += 1
                    elif kind == "user":
                        user_count += 1
                logger.info(
                    "[dry-run] room %s: %d sessions, %d messages classified",
                    room_id, len(sessions), sum(1 for _ in all_rows),
                )
                continue

            # Actual write path: clear this room's existing rows, then
            # re-insert everything in canonical order.
            target_conn.execute(
                "DELETE FROM agent_room_messages WHERE room_id = ?",
                (room_id,),
            )
            target_conn.execute(
                "DELETE FROM agent_room_message_seq WHERE room_id = ?",
                (room_id,),
            )
            seq = 0
            for ts, rowid, skey, msg in all_rows:
                _, kind, name = _classify_message(skey, msg["role"])
                if kind == "skip":
                    continue
                seq += 1
                total_msgs += 1
                if kind == "observer":
                    obs_count += 1
                elif kind == "member":
                    mem_count += 1
                elif kind == "tool_result":
                    tool_count += 1
                elif kind == "user":
                    user_count += 1
                target_conn.execute(
                    "INSERT INTO agent_room_messages "
                    "(room_id, sequence, sender_kind, sender_name, content, "
                    " tool_calls, tool_call_id, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (room_id, seq, kind, name, msg["content"] or "",
                     msg["tool_calls"], msg["tool_call_id"], ts or time.time()),
                )
            # Reset the seq counter
            target_conn.execute(
                "INSERT INTO agent_room_message_seq(room_id, next_seq) VALUES (?, ?)",
                (room_id, seq + 1),
            )
            target_conn.commit()
            logger.info(
                "[migrated] room %s: %d sessions, %d messages inserted",
                room_id, len(sessions), seq,
            )
    finally:
        if target_conn is not None:
            target_conn.close()

    return MigrationCounts(
        rooms_scanned=counts.rooms_scanned,
        rooms_skipped_no_data=counts.rooms_skipped_no_data,
        rooms_orphaned=orphan_count,
        messages_migrated=total_msgs,
        observer_rows=obs_count,
        member_rows=mem_count,
        tool_result_rows=tool_count,
        user_rows=user_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate M1 room sessions to M3 messages")
    parser.add_argument("--state-db", type=Path, default=None,
                        help="Path to state.db (default: ~/.hermes/state.db)")
    parser.add_argument("--rooms-db", type=Path, default=None,
                        help="Path to gateway_agent_rooms.sqlite")
    parser.add_argument("--messages-db", type=Path, default=None,
                        help="Path to gateway_agent_room_messages.sqlite")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would migrate without writing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from hermes_constants import get_default_hermes_root
    from gateway.agent_room_store import DEFAULT_AGENT_ROOMS_DB
    from gateway.agent_room_messages_store import DEFAULT_AGENT_ROOM_MESSAGES_DB

    root = get_default_hermes_root()
    state_db = args.state_db or (root / "state.db")
    rooms_db = args.rooms_db or (root / DEFAULT_AGENT_ROOMS_DB)
    messages_db = args.messages_db or (root / DEFAULT_AGENT_ROOM_MESSAGES_DB)

    # For actual write, initialize the target schema first
    if not args.dry_run:
        from gateway.agent_room_messages_store import AgentRoomMessagesStore
        _ = AgentRoomMessagesStore(messages_db)  # creates the schema
        _.close()

    counts = migrate(
        state_db=state_db,
        rooms_db=rooms_db,
        messages_db=messages_db,
        dry_run=args.dry_run,
    )
    mode = "DRY-RUN" if args.dry_run else "WRITE"
    logger.info("─── %s summary ───", mode)
    logger.info("  rooms scanned:              %d", counts.rooms_scanned)
    logger.info("  rooms with no data:         %d", counts.rooms_skipped_no_data)
    logger.info("  orphan rooms (not in store): %d", counts.rooms_orphaned)
    logger.info("  total messages:             %d", counts.messages_migrated)
    logger.info("    · user rows:              %d", counts.user_rows)
    logger.info("    · observer rows:          %d", counts.observer_rows)
    logger.info("    · member rows:            %d", counts.member_rows)
    logger.info("    · tool_result rows:       %d", counts.tool_result_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
