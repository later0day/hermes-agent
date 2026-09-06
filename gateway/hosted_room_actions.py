"""Durable pending-action state for hosted Room member turns."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

_DECISION_RELAY_BATCH = 256
_DELIVERED_DECISION_RETENTION_SECONDS = 30 * 24 * 60 * 60

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS hosted_room_pending_actions (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    action_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, member_id)
)
"""

_CREATE_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS hosted_room_action_decisions (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL,
    PRIMARY KEY (room_id, member_id, action_id)
)
"""

_CREATE_DECISIONS_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS hosted_room_action_decisions_pending
ON hosted_room_action_decisions(delivered_at, created_at, room_id, member_id, action_id)
"""


class ActionDecisionConflictError(ValueError):
    """The exact pending action already has a different immutable decision."""


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
    conn.execute(_CREATE_DECISIONS_TABLE)
    conn.execute(_CREATE_DECISIONS_PENDING_INDEX)
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
            try:
                value = json.loads(str(row["action_json"]))
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring corrupt hosted Room pending action for %s/%s",
                    row["room_id"],
                    row["member_id"],
                )
                continue
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


def request_action_decision(
    db_path: Path | str,
    *,
    room_id: str,
    member_id: str,
    action_id: str,
    decision: Mapping[str, Any],
) -> Literal["queued", "duplicate", "delivered"]:
    """Durably record the first decision for one exact pending action.

    The row is immutable: retries of the same choice are idempotent, conflicting
    choices fail closed, and a delivered decision can never be resurrected.
    """

    encoded = json.dumps(dict(decision), sort_keys=True, separators=(",", ":"))
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """DELETE FROM hosted_room_action_decisions
                   WHERE delivered_at IS NOT NULL AND delivered_at < ?""",
                (now - _DELIVERED_DECISION_RETENTION_SECONDS,),
            )
            cur = conn.execute(
                """INSERT INTO hosted_room_action_decisions
                       (room_id, member_id, action_id, decision_json, created_at,
                        delivered_at)
                   VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(room_id, member_id, action_id) DO NOTHING""",
                (room_id, member_id, action_id, encoded, now),
            )
            if cur.rowcount > 0:
                outcome: Literal["queued", "duplicate", "delivered"] = "queued"
            else:
                existing = conn.execute(
                    """SELECT decision_json, delivered_at
                       FROM hosted_room_action_decisions
                       WHERE room_id=? AND member_id=? AND action_id=?""",
                    (room_id, member_id, action_id),
                ).fetchone()
                if existing is None or str(existing["decision_json"]) != encoded:
                    raise ActionDecisionConflictError(
                        "Action already has a different decision"
                    )
                outcome = (
                    "delivered"
                    if existing["delivered_at"] is not None
                    else "duplicate"
                )
            conn.commit()
            return outcome
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def load_undelivered_decisions(
    db_path: Path | str,
) -> list[dict[str, Any]]:
    """Load one bounded decision batch; quarantine corrupt rows independently."""

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT room_id, member_id, action_id, decision_json, created_at
               FROM hosted_room_action_decisions
               WHERE delivered_at IS NULL
               ORDER BY created_at, room_id, member_id, action_id
               LIMIT ?""",
            (_DECISION_RELAY_BATCH,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(str(row["decision_json"]))
            except (TypeError, ValueError):
                value = None
            if not isinstance(value, dict):
                logger.warning(
                    "Discarding corrupt hosted Room action decision for %s/%s/%s",
                    row["room_id"],
                    row["member_id"],
                    row["action_id"],
                )
                # This write is exceptional. Normal relay polling remains a
                # read-only indexed query and does not contend on SQLite's
                # global writer lock across Dashboard/Gateway processes.
                conn.execute(
                    """UPDATE hosted_room_action_decisions SET delivered_at=?
                       WHERE room_id=? AND member_id=? AND action_id=?
                         AND delivered_at IS NULL""",
                    (
                        time.time(),
                        row["room_id"],
                        row["member_id"],
                        row["action_id"],
                    ),
                )
                continue
            result.append({
                "room_id": str(row["room_id"]),
                "member_id": str(row["member_id"]),
                "action_id": str(row["action_id"]),
                "created_at": float(row["created_at"]),
                **value,
            })
        return result
    finally:
        conn.close()


def mark_decision_delivered(
    db_path: Path | str,
    *,
    room_id: str,
    member_id: str,
    action_id: str,
) -> bool:
    """Mark one exact action decision delivered or safely discarded."""

    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """UPDATE hosted_room_action_decisions SET delivered_at=?
                   WHERE room_id=? AND member_id=? AND action_id=?
                     AND delivered_at IS NULL""",
                (now, room_id, member_id, action_id),
            )
            conn.execute(
                """DELETE FROM hosted_room_action_decisions
                   WHERE delivered_at IS NOT NULL AND delivered_at < ?""",
                (now - _DELIVERED_DECISION_RETENTION_SECONDS,),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
