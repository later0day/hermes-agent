"""M3.2 · Agent Room shared message store.

Design ref: docs/design/agent-room/design.html §2.4 (M3 明确包含).

M1's design was that each member's message history was implicit in
their own hermes session_id (per-member session). Under that model, a
member couldn't see what other members had said — that's the M1 UX
cliff §8 tried to patch with cross-member summaries.

M3 replaces that: a single shared table ``agent_room_messages`` holds
the FULL history of a room, with each message tagged with its sender
identity (user / observer / member_profile / tool). The projection
layer (M3.3) will then render that history from any single member's
point of view for their turn.

Schema decisions:
  * (room_id, sequence) is the canonical ordering key — sequence is a
    monotonic per-room counter, so concurrent inserts still order
    deterministically (§2.4 "消息落库时的顺序保证").
  * sender_kind distinguishes user / observer / member / tool_result
    — the projection algorithm treats them differently.
  * content is stored as UTF-8 text; tool_calls/tool_result_id are
    optional JSON blobs so tool exchanges can be re-rendered into
    natural language during projection.
  * Fence integration: same fence bit as M1 (AgentRoomStore) — a room
    delete/unbind/member-swap will drop in-flight member writes.

Mirrors AgentRoomStore's WAL + retry + row_factory pattern.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ROOM_MESSAGES_DB = "gateway_agent_room_messages.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_room_messages (
    room_id       TEXT NOT NULL,
    sequence      INTEGER NOT NULL,
    sender_kind   TEXT NOT NULL CHECK (sender_kind IN ('user','observer','member','tool_result')),
    sender_name   TEXT NOT NULL,
    content       TEXT NOT NULL,
    tool_calls    TEXT,
    tool_call_id  TEXT,
    timestamp     REAL NOT NULL,
    PRIMARY KEY (room_id, sequence)
);

CREATE INDEX IF NOT EXISTS agent_room_messages_room_ts
  ON agent_room_messages(room_id, timestamp);
CREATE INDEX IF NOT EXISTS agent_room_messages_sender
  ON agent_room_messages(room_id, sender_name);

CREATE TABLE IF NOT EXISTS agent_room_message_seq (
    room_id  TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class RoomMessage:
    """A single row in agent_room_messages."""
    room_id: str
    sequence: int
    sender_kind: str      # "user" / "observer" / "member" / "tool_result"
    sender_name: str      # user_id / observer_profile / member_profile / tool name
    content: str
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "sequence": self.sequence,
            "sender_kind": self.sender_kind,
            "sender_name": self.sender_name,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "timestamp": self.timestamp,
        }


class AgentRoomMessagesStore:
    """SQLite-backed shared message history for M3."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        if db_path is None:
            db_path = get_default_hermes_root() / DEFAULT_AGENT_ROOM_MESSAGES_DB
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._open()
        self._init_schema()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # WAL + retry same as AgentRoomStore
        for _ in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    time.sleep(0.05)
                    continue
                raise
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def _next_seq(self, room_id: str) -> int:
        """Atomic per-room monotonic sequence — guarantees canonical order
        even under concurrent writes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT next_seq FROM agent_room_message_seq WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO agent_room_message_seq(room_id, next_seq) VALUES (?, ?)",
                    (room_id, 2),
                )
                return 1
            seq = int(row["next_seq"])
            self._conn.execute(
                "UPDATE agent_room_message_seq SET next_seq = ? WHERE room_id = ?",
                (seq + 1, room_id),
            )
            return seq

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def append(
        self,
        room_id: str,
        *,
        sender_kind: str,
        sender_name: str,
        content: str,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
    ) -> RoomMessage:
        """Append one message. Sequence is auto-assigned."""
        if not room_id:
            raise ValueError("room_id required")
        if sender_kind not in ("user", "observer", "member", "tool_result"):
            raise ValueError(f"invalid sender_kind: {sender_kind!r}")

        seq = self._next_seq(room_id)
        ts = time.time()
        tc_json = json.dumps(tool_calls) if tool_calls else None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_room_messages
                    (room_id, sequence, sender_kind, sender_name, content,
                     tool_calls, tool_call_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (room_id, seq, sender_kind, sender_name, content,
                 tc_json, tool_call_id, ts),
            )

        return RoomMessage(
            room_id=room_id,
            sequence=seq,
            sender_kind=sender_kind,
            sender_name=sender_name,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            timestamp=ts,
        )

    def list_messages(
        self,
        room_id: str,
        *,
        limit: Optional[int] = None,
        since_seq: Optional[int] = None,
    ) -> list[RoomMessage]:
        """List messages in canonical (sequence) order."""
        params: list = [room_id]
        where = "room_id = ?"
        if since_seq is not None:
            where += " AND sequence > ?"
            params.append(since_seq)
        query = f"SELECT * FROM agent_room_messages WHERE {where} ORDER BY sequence"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        return [
            RoomMessage(
                room_id=r["room_id"],
                sequence=r["sequence"],
                sender_kind=r["sender_kind"],
                sender_name=r["sender_name"],
                content=r["content"],
                tool_calls=json.loads(r["tool_calls"]) if r["tool_calls"] else None,
                tool_call_id=r["tool_call_id"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def count(self, room_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM agent_room_messages WHERE room_id = ?",
                (room_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def delete_room(self, room_id: str) -> int:
        """Delete every message + seq counter for a room. Returns rows deleted.
        Idempotent — called from M1's AgentRoomStore.delete_room to keep
        the two tables in sync."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM agent_room_messages WHERE room_id = ?", (room_id,)
            )
            self._conn.execute(
                "DELETE FROM agent_room_message_seq WHERE room_id = ?", (room_id,)
            )
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
