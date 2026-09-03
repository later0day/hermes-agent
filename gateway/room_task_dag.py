"""Room ↔ task DAG (C3): a shared coordination ledger for hosted rooms.

A self-contained sidecar store — the structural sibling of
``gateway.room_mirror_db`` — that gives a hosted room the *shared task DAG* the
Claude Code agent-team feature coordinates around (see
``docs/design/hosted-room-task-dag.md`` and ``claude-agent-team-reference.md``
§4/A.2). It deliberately does NOT touch ``hosted_rooms``: it opens the same
``state.db`` file and creates its own ``room_task_dag`` / ``room_task_deps``
tables with ``CREATE TABLE IF NOT EXISTS`` (``hosted_rooms._schema_is_current``
uses subset checks per known table and ignores extra tables, so the additive
tables are invisible to it — no migration harness, no schema-guard change).

Concurrency discipline is the same battle-tested clone used by the C2 mirror:
WAL + ``BEGIN IMMEDIATE`` + CAS. The load-bearing invariant is the *claim*: a
worker self-claims the lowest-seq task that is ``pending``, unowned, and whose
every ``blockedBy`` dependency is ``completed`` (CC's F3 pull-based self-claim,
"lowest ID first"). Because every write serializes on SQLite's writer lock,
exactly one concurrent worker can claim a given task.

Completion auto-unblocks dependents *implicitly*: there is no stored "blocked"
flag to flip — ``claim_next`` re-evaluates the availability predicate every
call, so a dependent becomes claimable the instant its last blocker completes.
This structurally avoids the unblock-loop bug class entirely.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

VALID_STATUSES = ("pending", "in_progress", "completed")

_JOURNAL_MODE_LOCK_RETRIES = 6

_CREATE_TASK_TABLE = """
CREATE TABLE IF NOT EXISTS room_task_dag (
    room_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','in_progress','completed')),
    owner       TEXT,
    seq         INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (room_id, task_id)
)
"""
_CREATE_DEPS_TABLE = """
CREATE TABLE IF NOT EXISTS room_task_deps (
    room_id    TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    blocked_by TEXT NOT NULL,
    PRIMARY KEY (room_id, task_id, blocked_by)
)
"""
_CREATE_SEQ_INDEX = """
CREATE INDEX IF NOT EXISTS idx_room_task_seq ON room_task_dag(room_id, seq)
"""
_CREATE_DEPS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_room_task_deps_blocker
    ON room_task_deps(room_id, blocked_by)
"""


class RoomTaskError(Exception):
    """A caller-visible task-DAG error (bad state transition, cycle, etc.)."""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    from hermes_state import apply_wal_with_fallback

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for attempt in range(_JOURNAL_MODE_LOCK_RETRIES):
            try:
                apply_wal_with_fallback(conn, db_label="state.db (room_task_dag)")
                break
            except sqlite3.OperationalError as exc:
                if (
                    str(exc).lower() != "database is locked"
                    or attempt + 1 == _JOURNAL_MODE_LOCK_RETRIES
                ):
                    raise
                time.sleep(0.01 * (2**attempt))
        conn.execute(_CREATE_TASK_TABLE)
        conn.execute(_CREATE_DEPS_TABLE)
        conn.execute(_CREATE_SEQ_INDEX)
        conn.execute(_CREATE_DEPS_INDEX)
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


# ── internal helpers (must run inside a write txn) ──────────────────────────

def _deps_of(conn: sqlite3.Connection, room_id: str, task_id: str) -> list[str]:
    return [
        str(r["blocked_by"])
        for r in conn.execute(
            "SELECT blocked_by FROM room_task_deps WHERE room_id=? AND task_id=?",
            (room_id, task_id),
        ).fetchall()
    ]


def _would_cycle(
    conn: sqlite3.Connection, room_id: str, task_id: str, blocked_by: str
) -> bool:
    """True if adding edge ``task_id --blockedBy--> blocked_by`` closes a cycle.

    A cycle forms iff ``blocked_by`` can already (transitively) reach ``task_id``
    by following blockedBy edges — i.e. ``task_id`` is already a (transitive)
    blocker of ``blocked_by``. DFS over ``room_task_deps`` inside the caller's
    write txn, so the check is race-free. A self-edge (task blocks itself) is a
    trivial cycle.
    """

    if blocked_by == task_id:
        return True
    seen: set[str] = set()
    stack = [blocked_by]
    while stack:
        cur = stack.pop()
        if cur == task_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(_deps_of(conn, room_id, cur))
    return False


def _next_seq(conn: sqlite3.Connection, room_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM room_task_dag WHERE room_id=?",
        (room_id,),
    ).fetchone()
    return int(row["m"]) + 1


def _next_task_id(conn: sqlite3.Connection, room_id: str) -> str:
    """Allocate ``t<N>`` with N one past the highest existing numeric id."""

    highest = 0
    for r in conn.execute(
        "SELECT task_id FROM room_task_dag WHERE room_id=?", (room_id,)
    ).fetchall():
        tid = str(r["task_id"])
        if tid.startswith("t") and tid[1:].isdigit():
            highest = max(highest, int(tid[1:]))
    return f"t{highest + 1}"


def _row_exists(conn: sqlite3.Connection, room_id: str, task_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM room_task_dag WHERE room_id=? AND task_id=?",
            (room_id, task_id),
        ).fetchone()
        is not None
    )


def _blocked_by_incomplete(
    conn: sqlite3.Connection, room_id: str, task_id: str
) -> bool:
    """True if any blockedBy dep is not ``completed`` (a missing dep counts as
    blocking — fail-closed, so a typo'd dependency never makes work claimable)."""

    for dep in _deps_of(conn, room_id, task_id):
        row = conn.execute(
            "SELECT status FROM room_task_dag WHERE room_id=? AND task_id=?",
            (room_id, dep),
        ).fetchone()
        if row is None or str(row["status"]) != "completed":
            return True
    return False


def _hydrate(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    room_id = str(row["room_id"])
    task_id = str(row["task_id"])
    blocked_by = _deps_of(conn, room_id, task_id)
    blocks = [
        str(r["task_id"])
        for r in conn.execute(
            "SELECT task_id FROM room_task_deps WHERE room_id=? AND blocked_by=?",
            (room_id, task_id),
        ).fetchall()
    ]
    return {
        "room_id": room_id,
        "task_id": task_id,
        "subject": str(row["subject"]),
        "description": str(row["description"] or ""),
        "status": str(row["status"]),
        "owner": row["owner"],
        "seq": int(row["seq"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "blockedBy": sorted(blocked_by),
        "blocks": sorted(blocks),
    }


# ── public API ──────────────────────────────────────────────────────────────

def create_task(
    db_path: Path | str,
    *,
    room_id: str,
    subject: str,
    description: str = "",
    task_id: str | None = None,
    blocked_by: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a ``pending``, unowned task; return the hydrated row.

    ``task_id`` auto-allocates ``t<N>`` when None. Idempotent on an existing
    ``(room_id, task_id)`` — a duplicate returns the existing row unchanged
    (the ledger never silently rewrites a subject out from under a worker).
    Each ``blocked_by`` edge is cycle-checked before insert; a self- or
    back-edge raises :class:`RoomTaskError` and nothing is written.
    """

    subject = (subject or "").strip()
    if not subject:
        raise RoomTaskError("task subject must be non-empty")
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            tid = task_id or _next_task_id(conn, room_id)
            existing = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, tid),
            ).fetchone()
            if existing is not None:
                return _hydrate(conn, existing)
            now = time.time()
            conn.execute(
                """INSERT INTO room_task_dag
                   (room_id, task_id, subject, description, status, owner,
                    seq, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, ?)""",
                (room_id, tid, subject, description or "",
                 _next_seq(conn, room_id), now, now),
            )
            for dep in blocked_by:
                dep = str(dep)
                if not dep or dep == tid:
                    raise RoomTaskError(f"invalid dependency {dep!r}")
                if _would_cycle(conn, room_id, tid, dep):
                    raise RoomTaskError(
                        f"dependency {tid}->{dep} would create a cycle"
                    )
                conn.execute(
                    """INSERT OR IGNORE INTO room_task_deps
                       (room_id, task_id, blocked_by) VALUES (?, ?, ?)""",
                    (room_id, tid, dep),
                )
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, tid),
            ).fetchone()
            return _hydrate(conn, row)
    finally:
        conn.close()


def add_dependency(
    db_path: Path | str, *, room_id: str, task_id: str, blocked_by: str
) -> dict[str, Any]:
    """Add a ``blockedBy`` edge after creation (idempotent, cycle-checked)."""

    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            if not _row_exists(conn, room_id, task_id):
                raise RoomTaskError(f"unknown task {task_id!r}")
            if not _row_exists(conn, room_id, blocked_by):
                raise RoomTaskError(f"unknown dependency {blocked_by!r}")
            if _would_cycle(conn, room_id, task_id, blocked_by):
                raise RoomTaskError(
                    f"dependency {task_id}->{blocked_by} would create a cycle"
                )
            conn.execute(
                """INSERT OR IGNORE INTO room_task_deps
                   (room_id, task_id, blocked_by) VALUES (?, ?, ?)""",
                (room_id, task_id, blocked_by),
            )
            conn.execute(
                "UPDATE room_task_dag SET updated_at=? WHERE room_id=? AND task_id=?",
                (time.time(), room_id, task_id),
            )
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            return _hydrate(conn, row)
    finally:
        conn.close()


def claim_next(
    db_path: Path | str, *, room_id: str, owner: str
) -> dict[str, Any] | None:
    """F3 pull-based self-claim: claim the lowest-seq available task.

    Available = ``status='pending'`` ∧ ``owner IS NULL`` ∧ every ``blockedBy``
    dep is ``completed``. Sets ``owner`` and flips to ``in_progress`` under
    ``BEGIN IMMEDIATE``; concurrent callers serialize on the writer lock so no
    two ever claim the same task. Returns the claimed task, or None if nothing
    is available.
    """

    owner = (owner or "").strip()
    if not owner:
        raise RoomTaskError("claim requires a non-empty owner")
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            candidates = conn.execute(
                """SELECT * FROM room_task_dag
                   WHERE room_id=? AND status='pending' AND owner IS NULL
                   ORDER BY seq ASC""",
                (room_id,),
            ).fetchall()
            for row in candidates:
                if _blocked_by_incomplete(conn, room_id, str(row["task_id"])):
                    continue
                now = time.time()
                conn.execute(
                    """UPDATE room_task_dag
                       SET owner=?, status='in_progress', updated_at=?
                       WHERE room_id=? AND task_id=?
                         AND status='pending' AND owner IS NULL""",
                    (owner, now, room_id, str(row["task_id"])),
                )
                claimed = conn.execute(
                    "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                    (room_id, str(row["task_id"])),
                ).fetchone()
                return _hydrate(conn, claimed)
            return None
    finally:
        conn.close()


def claim_task(
    db_path: Path | str, *, room_id: str, task_id: str, owner: str
) -> dict[str, Any]:
    """Claim a *specific* task (must satisfy the availability predicate)."""

    owner = (owner or "").strip()
    if not owner:
        raise RoomTaskError("claim requires a non-empty owner")
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            if row is None:
                raise RoomTaskError(f"unknown task {task_id!r}")
            if str(row["status"]) != "pending" or row["owner"] is not None:
                raise RoomTaskError(f"task {task_id!r} is not available to claim")
            if _blocked_by_incomplete(conn, room_id, task_id):
                raise RoomTaskError(f"task {task_id!r} is blocked by an open dependency")
            now = time.time()
            conn.execute(
                """UPDATE room_task_dag
                   SET owner=?, status='in_progress', updated_at=?
                   WHERE room_id=? AND task_id=?
                     AND status='pending' AND owner IS NULL""",
                (owner, now, room_id, task_id),
            )
            claimed = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            return _hydrate(conn, claimed)
    finally:
        conn.close()


def assign_task(
    db_path: Path | str, *, room_id: str, task_id: str, owner: str
) -> dict[str, Any]:
    """Push-assign a task to ``owner`` (CC's ``isTaskAssignment`` branch).

    The decider sets the owner directly regardless of self-claim ordering, but
    still refuses to assign a task blocked by an incomplete dependency (an owner
    can't start work whose prerequisites are open). A completed task cannot be
    reassigned.
    """

    owner = (owner or "").strip()
    if not owner:
        raise RoomTaskError("assign requires a non-empty owner")
    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            if row is None:
                raise RoomTaskError(f"unknown task {task_id!r}")
            if str(row["status"]) == "completed":
                raise RoomTaskError(f"task {task_id!r} is already completed")
            if _blocked_by_incomplete(conn, room_id, task_id):
                raise RoomTaskError(f"task {task_id!r} is blocked by an open dependency")
            conn.execute(
                """UPDATE room_task_dag
                   SET owner=?, status='in_progress', updated_at=?
                   WHERE room_id=? AND task_id=?""",
                (owner, time.time(), room_id, task_id),
            )
            assigned = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            return _hydrate(conn, assigned)
    finally:
        conn.close()


def complete_task(
    db_path: Path | str, *, room_id: str, task_id: str
) -> dict[str, Any]:
    """Mark a task ``completed``. Dependents auto-unblock implicitly (the claim
    predicate re-checks blockers every call — no stored flag to flip)."""

    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            if row is None:
                raise RoomTaskError(f"unknown task {task_id!r}")
            conn.execute(
                """UPDATE room_task_dag SET status='completed', updated_at=?
                   WHERE room_id=? AND task_id=?""",
                (time.time(), room_id, task_id),
            )
            done = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            return _hydrate(conn, done)
    finally:
        conn.close()


def release_task(
    db_path: Path | str, *, room_id: str, task_id: str
) -> dict[str, Any]:
    """Return an in_progress task to ``pending`` and clear its owner (a worker
    gives up, or crash recovery re-opens an abandoned claim)."""

    conn = _connect(db_path)
    try:
        with _write_txn(conn):
            row = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            if row is None:
                raise RoomTaskError(f"unknown task {task_id!r}")
            conn.execute(
                """UPDATE room_task_dag SET status='pending', owner=NULL, updated_at=?
                   WHERE room_id=? AND task_id=?""",
                (time.time(), room_id, task_id),
            )
            released = conn.execute(
                "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
                (room_id, task_id),
            ).fetchone()
            return _hydrate(conn, released)
    finally:
        conn.close()


def get_task(
    db_path: Path | str, *, room_id: str, task_id: str
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM room_task_dag WHERE room_id=? AND task_id=?",
            (room_id, task_id),
        ).fetchone()
        return _hydrate(conn, row) if row is not None else None
    finally:
        conn.close()


def list_tasks(db_path: Path | str, *, room_id: str) -> list[dict[str, Any]]:
    """Return every task in the room in creation (seq) order — CC's TaskList."""

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM room_task_dag WHERE room_id=? ORDER BY seq ASC",
            (room_id,),
        ).fetchall()
        return [_hydrate(conn, row) for row in rows]
    finally:
        conn.close()
