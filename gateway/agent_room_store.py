"""Agent Room storage — SQLite-backed room metadata + in-memory Fence set.

Design reference: docs/design/agent-room/design.html §4.1 (data model),
§6.3 (Fence mechanism).

An Agent Room binds a member profile roster + an auto-generated observer
profile to an IM conversation (via SourceAgentBindingStore.fallback_extra
.room_id — see agent_room_bootstrapper.py and gateway/run.py's room
resolution branch). This module owns only the room record itself; profile
directory construction lives in agent_room_bootstrapper.py, and routing
lives in agent_room_router.py.

Storage location mirrors gateway/source_agent_binding.py: a sibling SQLite
file under the Hermes root, same WAL/retry init pattern, same "runtime
data, not profile-scoped" placement rationale.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root


DEFAULT_AGENT_ROOMS_DB = get_default_hermes_root() / "gateway_agent_rooms.sqlite"

# N3 (design.html §3): hard cap on room membership. Bootstrapper generates
# one observer profile per room whose SOUL.md enumerates every member by
# name + description — an unbounded roster would blow that prompt's context
# and make the observer's routing decision less reliable, not more.
MAX_ROOM_MEMBERS = 5

_SQLITE_INIT_LOCK = threading.RLock()


def _execute_sqlite_init(conn: sqlite3.Connection, sql: str) -> None:
    """Run SQLite init statements with a short retry for cross-connection locks.

    Mirrors gateway/source_agent_binding.py::_execute_sqlite_init verbatim —
    same failure mode (WAL mode switch racing another process's connection
    open), same bounded retry.
    """
    for attempt in range(6):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


class AgentRoomError(ValueError):
    """User-facing validation error for Agent Room operations."""


@dataclass(frozen=True)
class AgentRoom:
    room_id: str
    room_name: str
    description: str
    observer_profile: str
    members: tuple[str, ...]
    default_member: str
    created_at: int
    created_by: str
    updated_at: int
    updated_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "description": self.description,
            "observer_profile": self.observer_profile,
            "members": list(self.members),
            "default_member": self.default_member,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    def resolve_default_member(self) -> str:
        """Fallback routing target when the observer can't decide.

        M1-B4 / M1-B14: an explicit default_member wins; otherwise the
        first member in the roster is the fallback. Callers must not
        inline "or members[0]" themselves — this is the single source of
        truth so M1.5's router and M1.2's SOUL.md template agree.
        """
        if self.default_member and self.default_member in self.members:
            return self.default_member
        if self.members:
            return self.members[0]
        raise AgentRoomError(f"room {self.room_id!r} has no members to fall back to")


class AgentRoomStore:
    """SQLite-backed Agent Room store + in-memory session Fence set.

    The Fence set (§6.3) is process-local and intentionally NOT persisted:
    a Fence exists to protect against a race between "structural change"
    and "in-flight turn finishing late" within a single gateway process's
    lifetime. A restart already interrupts every in-flight turn (M1-B11),
    so there is nothing left to fence across a restart boundary.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = (
            Path(db_path) if db_path is not None else DEFAULT_AGENT_ROOMS_DB
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        with _SQLITE_INIT_LOCK, self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            _execute_sqlite_init(self._conn, "PRAGMA journal_mode=WAL")
            self._ensure_schema()

        # §6.3 Fence set: room_id -> set of fenced session_ids. Populated by
        # fence_room() on structural changes (delete / unbind / member
        # add-remove); checked by agent_room_router.py before every
        # observer-turn-result or member-turn-result emission.
        self._fenced_sessions: dict[str, set[str]] = {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_rooms (
              room_id TEXT PRIMARY KEY,
              room_name TEXT NOT NULL UNIQUE,
              description TEXT NOT NULL DEFAULT '',
              observer_profile TEXT NOT NULL,
              members_json TEXT NOT NULL DEFAULT '[]',
              default_member TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              created_by TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_rooms_name
            ON agent_rooms(room_name)
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Members JSON <-> tuple
    # ------------------------------------------------------------------

    @staticmethod
    def _members_dumps(members: tuple[str, ...]) -> str:
        return json.dumps(list(members), separators=(",", ":"))

    @staticmethod
    def _members_loads(value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(str(m).strip() for m in parsed if str(m).strip())

    @classmethod
    def _room_from_row(cls, row: sqlite3.Row | None) -> AgentRoom | None:
        if row is None:
            return None
        return AgentRoom(
            room_id=str(row["room_id"]),
            room_name=str(row["room_name"]),
            description=str(row["description"] or ""),
            observer_profile=str(row["observer_profile"]),
            members=cls._members_loads(row["members_json"]),
            default_member=str(row["default_member"] or ""),
            created_at=int(row["created_at"]),
            created_by=str(row["created_by"] or ""),
            updated_at=int(row["updated_at"]),
            updated_by=str(row["updated_by"] or ""),
        )

    @staticmethod
    def _normalize_members(members: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Dedupe (case-sensitive, profile names are) while preserving order.

        M1-B3: enforced here so both create_room and add_member share one
        gate instead of duplicating the >MAX_ROOM_MEMBERS check.
        """
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in members:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)
        if len(normalized) > MAX_ROOM_MEMBERS:
            raise AgentRoomError(
                f"room cannot have more than {MAX_ROOM_MEMBERS} members "
                f"(got {len(normalized)}); trim the roster before retrying"
            )
        if not normalized:
            raise AgentRoomError("room must have at least one member")
        return tuple(normalized)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get_room(self, room_id: str) -> AgentRoom | None:
        key = str(room_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_rooms WHERE room_id = ?", (key,)
            ).fetchone()
            return self._room_from_row(row)

    def get_room_by_name(self, room_name: str) -> AgentRoom | None:
        name = str(room_name or "").strip()
        if not name:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_rooms WHERE room_name = ?", (name,)
            ).fetchone()
            return self._room_from_row(row)

    def create_room(
        self,
        room_id: str,
        room_name: str,
        *,
        observer_profile: str,
        members: list[str] | tuple[str, ...],
        description: str = "",
        default_member: str = "",
        actor: str = "",
    ) -> AgentRoom:
        key = str(room_id or "").strip()
        name = str(room_name or "").strip()
        observer = str(observer_profile or "").strip()
        if not key:
            raise AgentRoomError("room_id is required")
        if not name:
            raise AgentRoomError("room_name is required")
        if not observer:
            raise AgentRoomError("observer_profile is required")

        normalized_members = self._normalize_members(members)
        resolved_default = str(default_member or "").strip()
        if resolved_default and resolved_default not in normalized_members:
            raise AgentRoomError(
                f"default_member {resolved_default!r} is not in the member roster"
            )

        now = int(time.time())
        with self._lock:
            if self.get_room_by_name(name) is not None:
                raise AgentRoomError(f"room name {name!r} already exists")
            try:
                self._conn.execute(
                    """
                    INSERT INTO agent_rooms (
                      room_id, room_name, description, observer_profile,
                      members_json, default_member,
                      created_at, created_by, updated_at, updated_by
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        name,
                        str(description or ""),
                        observer,
                        self._members_dumps(normalized_members),
                        resolved_default,
                        now,
                        str(actor or ""),
                        now,
                        str(actor or ""),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AgentRoomError(f"room_id {key!r} already exists") from exc
            self._conn.commit()
            room = self.get_room(key)
            if room is None:
                raise RuntimeError("failed to read back created room")
            return room

    def update_members(
        self,
        room_id: str,
        members: list[str] | tuple[str, ...],
        *,
        default_member: str | None = None,
        actor: str = "",
    ) -> AgentRoom:
        """Replace the member roster. Callers must re-render SOUL.md after this
        (agent_room_bootstrapper.regenerate_soul) and fence the room's active
        sessions BEFORE calling this (see fence_room) — this method only
        touches the SQL row, it does not fence or regenerate anything itself,
        so the two side effects can't be silently forgotten by one caller and
        not another.
        """
        key = str(room_id or "").strip()
        normalized_members = self._normalize_members(members)
        with self._lock:
            existing = self.get_room(key)
            if existing is None:
                raise AgentRoomError(f"room {key!r} not found")
            resolved_default = (
                str(default_member).strip()
                if default_member is not None
                else existing.default_member
            )
            if resolved_default and resolved_default not in normalized_members:
                resolved_default = ""
            self._conn.execute(
                """
                UPDATE agent_rooms
                SET members_json = ?, default_member = ?,
                    updated_at = ?, updated_by = ?
                WHERE room_id = ?
                """,
                (
                    self._members_dumps(normalized_members),
                    resolved_default,
                    int(time.time()),
                    str(actor or ""),
                    key,
                ),
            )
            self._conn.commit()
            room = self.get_room(key)
            if room is None:
                raise RuntimeError("failed to read back updated room")
            return room

    def delete_room(self, room_id: str) -> bool:
        """Delete the room row. Idempotent (M1-B9): missing row -> False,
        never raises. Callers are responsible for fencing + deleting the
        observer profile directory + clearing any SourceAgentBinding.room_id
        pointing at this room BEFORE or AFTER calling this — deletion order
        across those three stores is owned by the router/bootstrapper layer,
        not by this store.
        """
        key = str(room_id or "").strip()
        if not key:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM agent_rooms WHERE room_id = ?", (key,)
            )
            self._conn.commit()
            self._fenced_sessions.pop(key, None)
            return cur.rowcount > 0

    def list_rooms(self) -> list[AgentRoom]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_rooms ORDER BY updated_at DESC, room_id ASC"
            ).fetchall()
            return [
                room for row in rows if (room := self._room_from_row(row)) is not None
            ]

    # ------------------------------------------------------------------
    # §6.3 Fence mechanism
    # ------------------------------------------------------------------

    def fence_room(self, room_id: str, session_ids: list[str] | tuple[str, ...]) -> None:
        """Mark the given session_ids as fenced for this room.

        Called by the router/bootstrapper BEFORE a structural change
        (delete / unbind / member add-remove) so any in-flight turn on
        those sessions has its result discarded once it (eventually)
        finishes — see is_fenced().
        """
        key = str(room_id or "").strip()
        if not key:
            return
        ids = {str(s).strip() for s in session_ids if str(s).strip()}
        if not ids:
            return
        with self._lock:
            self._fenced_sessions.setdefault(key, set()).update(ids)

    def is_fenced(self, room_id: str, session_id: str) -> bool:
        key = str(room_id or "").strip()
        sid = str(session_id or "").strip()
        if not key or not sid:
            return False
        with self._lock:
            return sid in self._fenced_sessions.get(key, ())

    def unfence_room(self, room_id: str) -> None:
        """Release all fenced sessions for a room (e.g. after a successful
        re-bind establishes a fresh set of active sessions)."""
        key = str(room_id or "").strip()
        if not key:
            return
        with self._lock:
            self._fenced_sessions.pop(key, None)
