import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import hosted_room_actions as actions


def _decision(choice: str = "once") -> dict:
    return {
        "choice": choice,
        "task_id": "task-1",
        "execution_generation": 2,
        "request_id": "request-1",
    }


def test_action_decision_is_first_writer_wins_and_never_reopens(tmp_path):
    db = tmp_path / "state.db"

    assert actions.request_action_decision(
        db,
        room_id="room-1",
        member_id="member-1",
        action_id="request-1",
        decision=_decision(),
    ) == "queued"
    assert actions.request_action_decision(
        db,
        room_id="room-1",
        member_id="member-1",
        action_id="request-1",
        decision=_decision(),
    ) == "duplicate"
    with pytest.raises(
        actions.ActionDecisionConflictError,
        match="already has a different decision",
    ):
        actions.request_action_decision(
            db,
            room_id="room-1",
            member_id="member-1",
            action_id="request-1",
            decision=_decision("deny"),
        )

    assert actions.mark_decision_delivered(
        db,
        room_id="room-1",
        member_id="member-1",
        action_id="request-1",
    ) is True
    assert actions.request_action_decision(
        db,
        room_id="room-1",
        member_id="member-1",
        action_id="request-1",
        decision=_decision(),
    ) == "delivered"
    assert actions.load_undelivered_decisions(db) == []


def test_corrupt_decision_is_quarantined_without_blocking_later_rows(tmp_path):
    db = tmp_path / "state.db"
    actions.request_action_decision(
        db,
        room_id="room-1",
        member_id="valid-member",
        action_id="valid-action",
        decision=_decision(),
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO hosted_room_action_decisions
               (room_id, member_id, action_id, decision_json, created_at,
                delivered_at) VALUES (?, ?, ?, ?, ?, NULL)""",
            ("room-1", "corrupt-member", "corrupt-action", "{", 0.0),
        )
        conn.commit()
    finally:
        conn.close()

    loaded = actions.load_undelivered_decisions(db)
    assert [(row["member_id"], row["action_id"]) for row in loaded] == [
        ("valid-member", "valid-action")
    ]
    conn = sqlite3.connect(db)
    try:
        quarantined = conn.execute(
            """SELECT delivered_at FROM hosted_room_action_decisions
               WHERE member_id='corrupt-member'"""
        ).fetchone()
    finally:
        conn.close()
    assert quarantined is not None
    assert quarantined[0] is not None


def test_conflicting_concurrent_decisions_have_one_immutable_winner(tmp_path):
    db = tmp_path / "state.db"
    actions.load_undelivered_decisions(db)  # Initialize schema before racing.
    barrier = threading.Barrier(2)

    def decide(choice: str) -> str:
        barrier.wait()
        try:
            return actions.request_action_decision(
                db,
                room_id="room-1",
                member_id="member-1",
                action_id="request-race",
                decision=_decision(choice),
            )
        except actions.ActionDecisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("once", "deny")))

    assert sorted(results) == ["conflict", "queued"]
    loaded = actions.load_undelivered_decisions(db)
    assert len(loaded) == 1
    assert loaded[0]["choice"] in {"once", "deny"}


def test_decision_relay_batches_and_prunes_delivered_history(tmp_path):
    db = tmp_path / "state.db"
    actions.load_undelivered_decisions(db)
    encoded = json.dumps(_decision(), sort_keys=True, separators=(",", ":"))
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            """INSERT INTO hosted_room_action_decisions
               (room_id, member_id, action_id, decision_json, created_at,
                delivered_at) VALUES (?, ?, ?, ?, ?, NULL)""",
            [
                ("room-1", f"member-{i:03d}", f"action-{i:03d}", encoded, float(i))
                for i in range(257)
            ],
        )
        conn.execute(
            """INSERT INTO hosted_room_action_decisions
               (room_id, member_id, action_id, decision_json, created_at,
                delivered_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "room-old",
                "member-old",
                "action-old",
                encoded,
                0.0,
                time.time() - actions._DELIVERED_DECISION_RETENTION_SECONDS - 1,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    first = actions.load_undelivered_decisions(db)
    assert len(first) == actions._DECISION_RELAY_BATCH
    for row in first:
        assert actions.mark_decision_delivered(
            db,
            room_id=row["room_id"],
            member_id=row["member_id"],
            action_id=row["action_id"],
        )
    second = actions.load_undelivered_decisions(db)
    assert [row["action_id"] for row in second] == ["action-256"]

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            """SELECT 1 FROM hosted_room_action_decisions
               WHERE action_id='action-old'"""
        ).fetchone() is None
    finally:
        conn.close()
