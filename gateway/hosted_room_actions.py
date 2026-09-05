"""Durable pending-action state for hosted Room member turns."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS hosted_room_pending_actions (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    action_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, member_id)
)
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    from hermes_state import apply_wal_with_fallback

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    apply_wal_with_fallback(
        conn, db_label="state.db (hosted room pending actions)"
    )
    conn.execute(_CREATE_TABLE)
    return conn


def load_pending_actions(
    db_path: Path | str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load every unresolved action keyed by (room_id, member_id)."""

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT room_id, member_id, action_json FROM hosted_room_pending_actions"
        ).fetchall()
        actions: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            value = json.loads(str(row["action_json"]))
            if not isinstance(value, dict):
                continue
            member_id = str(row["member_id"])
            actions[(str(row["room_id"]), member_id)] = {
                **value,
                "member_id": member_id,
            }
        return actions
    finally:
        conn.close()


def set_pending_action(
    db_path: Path | str,
    *,
    room_id: str,
    member_id: str,
    action: Mapping[str, Any],
) -> None:
    """Durably replace one member's pending action."""

    value = {**action, "member_id": member_id}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO hosted_room_pending_actions
                       (room_id, member_id, action_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(room_id, member_id) DO UPDATE SET
                       action_json=excluded.action_json,
                       updated_at=excluded.updated_at""",
                (room_id, member_id, encoded, time.time()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def clear_pending_action(
    db_path: Path | str,
    *,
    room_id: str,
    member_id: str,
) -> bool:
    """Durably remove one resolved or withdrawn pending action."""

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """DELETE FROM hosted_room_pending_actions
                   WHERE room_id=? AND member_id=?""",
                (room_id, member_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
