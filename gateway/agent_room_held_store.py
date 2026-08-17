"""Agent Room · held-draft store (Raft AX 改造 1).

Design ref: docs/design/agent-room/ax-alignment.md §3 改造 1.

Raft《Is Having Agents in the Room Meant to Be Chaotic?》's central AX
insight is that an agent is *turn-based*: it reads a snapshot of the room,
reasons, and only then commits — and the room can move underneath it in
the gap between "reasoned" and "committed". Raft's answer is the
**held draft**: instead of silently swallowing a now-stale reply, the
system *holds* it and hands it back to the author with an explicit set of
recovery paths (Revise / Send-as-is / Stay-silent / Send-anyway).

Our ``AgentRoomRouter`` already had the *freshness-check* half of this
(the §6.3 Fence gates in agent_room_router.py) — but its resolution was
the exact opposite of Raft's: on a fence hit it ``return {"reply": None}``,
i.e. it **silently dropped** the member's finished reply, with no record,
no notification, no recovery. The same black hole happens on the live
DingTalk path when ``session_webhook`` expires (defect #2): the member
produced a perfectly good reply, but the transport was gone, so the reply
vanished.

This store is the durable backing for held-draft. When the router would
otherwise drop a reply, it instead persists a ``HeldReply`` row here:

  * ``room_version`` — the ``agent_room_messages`` sequence the reply was
    reasoned against. This is the "snapshot marker" Raft describes: on
    resolution we compare it to the room's *current* max sequence to tell
    whether the room actually moved (→ offer Revise) or merely lost its
    transport (→ Send-as-is is safe).
  * ``held_reason`` — why it was held (``fenced`` / ``no_webhook`` /
    ``send_failed``), so the resolver / operator can pick the right path.
  * ``status`` — ``held`` → ``resolved`` (terminal). A resolved row keeps
    the ``resolution`` string (which of the four paths was taken) for
    audit; it is never re-delivered.

Persistence rationale (vs. the Fence set, which agent_room_store.py
intentionally does NOT persist): a Fence exists only to win an in-process
race between a structural change and a late-finishing turn, so a restart —
which already kills every in-flight turn — makes it moot. A *held reply*
is the opposite: it is a finished, valuable artifact whose whole point is
to survive until someone (or the gateway, post-restart) delivers or
consciously discards it. So it MUST be durable. The two mechanisms are
orthogonal, exactly as noted in ax-alignment.md §3 改造 1 step 4.

Mirrors AgentRoomMessagesStore's WAL + retry + row_factory pattern.
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

DEFAULT_AGENT_ROOM_HELD_DB = "gateway_agent_room_held.sqlite"

# held_reason vocabulary — why a reply could not be delivered as-is.
HELD_REASON_FENCED = "fenced"        # §6.3 fence hit (room moved structurally)
HELD_REASON_NO_WEBHOOK = "no_webhook"  # defect #2: session_webhook expired/absent
HELD_REASON_SEND_FAILED = "send_failed"  # transport raised on delivery attempt
HELD_REASON_ROOM_MOVED = "room_moved"  # Raft turn-based gap: a *new user turn*
#                                        landed while this reply was being
#                                        composed, so it would arrive as a
#                                        non-sequitur (the counting-game case).

_VALID_HELD_REASONS = frozenset(
    {HELD_REASON_FENCED, HELD_REASON_NO_WEBHOOK, HELD_REASON_SEND_FAILED,
     HELD_REASON_ROOM_MOVED}
)

# The four Raft recovery paths — the vocabulary a resolver records when it
# takes a held reply out of the "held" state.
RESOLUTION_REVISE = "revise"          # re-project + re-run against current room
RESOLUTION_SEND_AS_IS = "send_as_is"  # deliver unchanged via a transport-independent path
RESOLUTION_STAY_SILENT = "stay_silent"  # topic already covered → conscious discard
RESOLUTION_SEND_ANYWAY = "send_anyway"  # explicit bypass after repeated holds

_VALID_RESOLUTIONS = frozenset(
    {RESOLUTION_REVISE, RESOLUTION_SEND_AS_IS,
     RESOLUTION_STAY_SILENT, RESOLUTION_SEND_ANYWAY}
)

STATUS_HELD = "held"
STATUS_RESOLVED = "resolved"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_room_held_replies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    member        TEXT NOT NULL,
    room_version  INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    held_reason   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'held'
                    CHECK (status IN ('held','resolved')),
    resolution    TEXT,
    chat_id       TEXT,
    extra         TEXT,
    created_at    REAL NOT NULL,
    resolved_at   REAL
);

CREATE INDEX IF NOT EXISTS agent_room_held_room_status
  ON agent_room_held_replies(room_id, status);
CREATE INDEX IF NOT EXISTS agent_room_held_status
  ON agent_room_held_replies(status);
"""


@dataclass
class HeldReply:
    """One held (undelivered) member reply awaiting a Raft recovery path.

    ``room_version`` is the ``agent_room_messages`` sequence the reply was
    reasoned against; comparing it to the room's *current* max sequence at
    resolution time is how we tell "the room moved" (offer Revise) from
    "only the transport was lost" (Send-as-is is safe)."""
    id: Optional[int]
    room_id: str
    session_id: str
    member: str
    room_version: int
    payload: str
    held_reason: str
    status: str = STATUS_HELD
    resolution: Optional[str] = None
    chat_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    resolved_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "session_id": self.session_id,
            "member": self.member,
            "room_version": self.room_version,
            "payload": self.payload,
            "held_reason": self.held_reason,
            "status": self.status,
            "resolution": self.resolution,
            "chat_id": self.chat_id,
            "extra": self.extra,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class AgentRoomHeldStore:
    """SQLite-backed durable store of held (undelivered) member replies."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        if db_path is None:
            db_path = get_default_hermes_root() / DEFAULT_AGENT_ROOM_HELD_DB
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

    # ------------------------------------------------------------------
    # Row <-> dataclass
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_held(r: sqlite3.Row) -> HeldReply:
        extra_raw = r["extra"]
        try:
            extra = json.loads(extra_raw) if extra_raw else {}
        except (json.JSONDecodeError, TypeError):
            extra = {}
        return HeldReply(
            id=int(r["id"]),
            room_id=r["room_id"],
            session_id=r["session_id"],
            member=r["member"],
            room_version=int(r["room_version"]),
            payload=r["payload"],
            held_reason=r["held_reason"],
            status=r["status"],
            resolution=r["resolution"],
            chat_id=r["chat_id"],
            extra=extra if isinstance(extra, dict) else {},
            created_at=float(r["created_at"]),
            resolved_at=float(r["resolved_at"]) if r["resolved_at"] is not None else None,
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def hold(
        self,
        room_id: str,
        *,
        session_id: str,
        member: str,
        room_version: int,
        payload: str,
        held_reason: str,
        chat_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> HeldReply:
        """Persist one held reply. Returns the stored row (with its id).

        Raises ValueError on an unknown ``held_reason`` so a typo can't
        silently create an un-resolvable row."""
        if not room_id:
            raise ValueError("room_id required")
        if not member:
            raise ValueError("member required")
        if held_reason not in _VALID_HELD_REASONS:
            raise ValueError(
                f"invalid held_reason {held_reason!r}; "
                f"expected one of {sorted(_VALID_HELD_REASONS)}"
            )
        ts = time.time()
        extra_json = json.dumps(extra) if extra else None

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO agent_room_held_replies
                    (room_id, session_id, member, room_version, payload,
                     held_reason, status, resolution, chat_id, extra,
                     created_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
                """,
                (room_id, session_id, member, int(room_version), payload,
                 held_reason, STATUS_HELD, chat_id, extra_json, ts),
            )
            new_id = int(cur.lastrowid)

        logger.info(
            "room %s: held reply from member %s (reason=%s, room_version=%d, id=%d)",
            room_id, member, held_reason, room_version, new_id,
        )
        return HeldReply(
            id=new_id,
            room_id=room_id,
            session_id=session_id,
            member=member,
            room_version=int(room_version),
            payload=payload,
            held_reason=held_reason,
            status=STATUS_HELD,
            resolution=None,
            chat_id=chat_id,
            extra=extra or {},
            created_at=ts,
            resolved_at=None,
        )

    def get(self, held_id: int) -> Optional[HeldReply]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_room_held_replies WHERE id = ?",
                (int(held_id),),
            ).fetchone()
        return self._row_to_held(row) if row else None

    def list_held(
        self,
        room_id: Optional[str] = None,
        *,
        include_resolved: bool = False,
    ) -> list[HeldReply]:
        """List held replies, oldest first (delivery/resolution order).

        By default returns only ``held`` rows; pass ``include_resolved`` to
        also see the terminal audit rows."""
        where: list[str] = []
        params: list[Any] = []
        if room_id is not None:
            where.append("room_id = ?")
            params.append(room_id)
        if not include_resolved:
            where.append("status = ?")
            params.append(STATUS_HELD)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        query = (
            f"SELECT * FROM agent_room_held_replies{clause} "
            "ORDER BY created_at ASC, id ASC"
        )
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_held(r) for r in rows]

    def resolve(self, held_id: int, resolution: str) -> bool:
        """Mark a held reply resolved via one of the four Raft paths.

        Idempotent-ish: returns True if a *still-held* row was transitioned,
        False if the id doesn't exist or was already resolved (so a
        double-resolve can't re-deliver). Raises ValueError on an unknown
        resolution."""
        if resolution not in _VALID_RESOLUTIONS:
            raise ValueError(
                f"invalid resolution {resolution!r}; "
                f"expected one of {sorted(_VALID_RESOLUTIONS)}"
            )
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE agent_room_held_replies
                SET status = ?, resolution = ?, resolved_at = ?
                WHERE id = ? AND status = ?
                """,
                (STATUS_RESOLVED, resolution, ts, int(held_id), STATUS_HELD),
            )
            changed = (cur.rowcount or 0) > 0
        if changed:
            logger.info(
                "held reply %d resolved via %s", held_id, resolution,
            )
        return changed

    def delete_room(self, room_id: str) -> int:
        """Delete every held row for a room. Returns rows deleted. Called
        from AgentRoomStore.delete_room to keep the stores in sync."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM agent_room_held_replies WHERE room_id = ?",
                (room_id,),
            )
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
