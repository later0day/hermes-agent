"""Source-to-agent binding storage for gateway conversations."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_default_hermes_root


DEFAULT_SOURCE_AGENT_BINDINGS_DB = (
    get_default_hermes_root() / "gateway_source_agent_bindings.sqlite"
)
_SQLITE_INIT_LOCK = threading.RLock()


def _execute_sqlite_init(conn: sqlite3.Connection, sql: str) -> None:
    """Run SQLite init statements with a short retry for cross-connection locks."""
    for attempt in range(6):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


@dataclass(frozen=True)
class SourceAgentBinding:
    source_binding_key: str
    profile_name: str
    agent_id: str
    fallback_target: dict[str, Any] | None = None
    fallback_extra: dict[str, Any] | None = None
    created_at: int = 0
    created_by: str | None = None
    created_by_name: str | None = None
    updated_at: int = 0
    updated_by: str | None = None
    updated_by_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_binding_key": self.source_binding_key,
            "profile_name": self.profile_name,
            "agent_id": self.agent_id,
            "fallback_target": self.fallback_target,
            "fallback_extra": self.fallback_extra,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "created_by_name": self.created_by_name,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "updated_by_name": self.updated_by_name,
        }


class SourceAgentBindingStore:
    """SQLite-backed source-agent binding store."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = (
            Path(db_path)
            if db_path is not None
            else DEFAULT_SOURCE_AGENT_BINDINGS_DB
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
        self._secure_files()

    def _secure_files(self) -> None:
        """Keep the DB and live WAL sidecars private (webhooks are secrets)."""
        if os.name != "posix":
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.db_path}{suffix}").chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_agent_bindings (
              source_binding_key TEXT PRIMARY KEY,
              profile_name TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              fallback_target_json TEXT,
              fallback_extra_json TEXT,
              created_at INTEGER NOT NULL,
              created_by TEXT,
              created_by_name TEXT,
              updated_at INTEGER NOT NULL,
              updated_by TEXT,
              updated_by_name TEXT
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _json_dumps(value: dict[str, Any] | None) -> str | None:
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _json_loads(value: str | None) -> dict[str, Any] | None:
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _binding_from_row(cls, row: sqlite3.Row | None) -> SourceAgentBinding | None:
        if row is None:
            return None
        return SourceAgentBinding(
            source_binding_key=str(row["source_binding_key"]),
            profile_name=str(row["profile_name"]),
            agent_id=str(row["agent_id"]),
            fallback_target=cls._json_loads(row["fallback_target_json"]),
            fallback_extra=cls._json_loads(row["fallback_extra_json"]),
            created_at=int(row["created_at"]),
            created_by=row["created_by"],
            created_by_name=row["created_by_name"],
            updated_at=int(row["updated_at"]),
            updated_by=row["updated_by"],
            updated_by_name=row["updated_by_name"],
        )

    def get_binding(self, source_binding_key: str) -> SourceAgentBinding | None:
        key = str(source_binding_key or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM source_agent_bindings
                WHERE source_binding_key = ?
                """,
                (key,),
            ).fetchone()
            return self._binding_from_row(row)

    def set_binding(
        self,
        source_binding_key: str,
        profile_name: str,
        *,
        agent_id: str | None = None,
        fallback_target: dict[str, Any] | None = None,
        fallback_extra: dict[str, Any] | None = None,
        actor_user_id: str | None = None,
        actor_user_name: str | None = None,
    ) -> SourceAgentBinding:
        key = str(source_binding_key or "").strip()
        profile = str(profile_name or "").strip()
        resolved_agent_id = str(agent_id or profile).strip()
        if not key:
            raise ValueError("source_binding_key is required")
        if not profile:
            raise ValueError("profile_name is required")
        if not resolved_agent_id:
            raise ValueError("agent_id is required")

        now = int(time.time())
        with self._lock:
            existing = self.get_binding(key)
            created_at = existing.created_at if existing else now
            created_by = existing.created_by if existing else actor_user_id
            created_by_name = existing.created_by_name if existing else actor_user_name
            self._conn.execute(
                """
                INSERT INTO source_agent_bindings (
                  source_binding_key, profile_name, agent_id,
                  fallback_target_json, fallback_extra_json,
                  created_at, created_by, created_by_name,
                  updated_at, updated_by, updated_by_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_binding_key) DO UPDATE SET
                  profile_name = excluded.profile_name,
                  agent_id = excluded.agent_id,
                  fallback_target_json = excluded.fallback_target_json,
                  fallback_extra_json = excluded.fallback_extra_json,
                  updated_at = excluded.updated_at,
                  updated_by = excluded.updated_by,
                  updated_by_name = excluded.updated_by_name
                """,
                (
                    key,
                    profile,
                    resolved_agent_id,
                    self._json_dumps(fallback_target),
                    self._json_dumps(fallback_extra),
                    created_at,
                    created_by,
                    created_by_name,
                    now,
                    actor_user_id,
                    actor_user_name,
                ),
            )
            self._conn.commit()
            self._secure_files()
            binding = self.get_binding(key)
            if binding is None:
                raise RuntimeError("failed to read stored source-agent binding")
            return binding

    def delete_binding(self, source_binding_key: str) -> bool:
        key = str(source_binding_key or "").strip()
        if not key:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM source_agent_bindings WHERE source_binding_key = ?",
                (key,),
            )
            self._conn.commit()
            self._secure_files()
            return cur.rowcount > 0

    def delete_bindings_for_profile(self, profile_name: str) -> int:
        profile = str(profile_name or "").strip()
        if not profile:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM source_agent_bindings WHERE profile_name = ?",
                (profile,),
            )
            self._conn.commit()
            self._secure_files()
            return int(cur.rowcount or 0)

    def list_bindings(self, *, profile_name: str | None = None) -> list[SourceAgentBinding]:
        params: Iterable[Any]
        sql = "SELECT * FROM source_agent_bindings"
        profile = str(profile_name or "").strip()
        if profile:
            sql += " WHERE profile_name = ?"
            params = (profile,)
        else:
            params = ()
        sql += " ORDER BY updated_at DESC, source_binding_key ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [
                binding
                for row in rows
                if (binding := self._binding_from_row(row)) is not None
            ]
