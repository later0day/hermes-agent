"""Tests for gateway/room_task_dag.py — C3 shared task DAG.

Exercises the additive sidecar store against real sqlite: create/idempotency,
the F3 availability predicate + lowest-seq self-claim, push-assign, auto-unblock
on completion, release, cycle rejection, and the bidirectional blockedBy/blocks
survey. The venv has no pytest, so these run under a tiny standalone harness
(see the __main__ block) as well as under pytest if available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway import room_task_dag as dag

ROOM = "room-1"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_create_defaults_and_auto_id(db):
    t = dag.create_task(db, room_id=ROOM, subject="Build API")
    assert t["task_id"] == "t1"
    assert t["status"] == "pending"
    assert t["owner"] is None
    assert t["seq"] == 1
    assert t["blockedBy"] == [] and t["blocks"] == []
    t2 = dag.create_task(db, room_id=ROOM, subject="Write tests")
    assert t2["task_id"] == "t2" and t2["seq"] == 2


def test_create_is_idempotent_on_explicit_id(db):
    a = dag.create_task(db, room_id=ROOM, subject="First", task_id="x")
    b = dag.create_task(db, room_id=ROOM, subject="Rewritten", task_id="x")
    # Duplicate returns the existing row unchanged (subject not clobbered).
    assert b["subject"] == "First"
    assert a["seq"] == b["seq"]
    assert len(dag.list_tasks(db, room_id=ROOM)) == 1


def test_empty_subject_rejected(db):
    with pytest.raises(dag.RoomTaskError):
        dag.create_task(db, room_id=ROOM, subject="   ")


def test_unblocked_task_is_claimable(db):
    dag.create_task(db, room_id=ROOM, subject="Solo")
    claimed = dag.claim_next(db, room_id=ROOM, owner="alice")
    assert claimed is not None
    assert claimed["task_id"] == "t1"
    assert claimed["owner"] == "alice"
    assert claimed["status"] == "in_progress"


def test_blocked_task_not_claimable_until_blocker_completes(db):
    dag.create_task(db, room_id=ROOM, subject="Design", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="t2",
                    blocked_by=["t1"])
    # t2 is blocked; claim_next must pick t1 first, never t2.
    first = dag.claim_next(db, room_id=ROOM, owner="alice")
    assert first["task_id"] == "t1"
    # With t1 in_progress (not completed) t2 is still blocked → nothing to claim.
    assert dag.claim_next(db, room_id=ROOM, owner="bob") is None
    # Complete t1 → t2 auto-unblocks.
    dag.complete_task(db, room_id=ROOM, task_id="t1")
    second = dag.claim_next(db, room_id=ROOM, owner="bob")
    assert second["task_id"] == "t2" and second["owner"] == "bob"


def test_blocked_by_missing_id_is_fail_closed(db):
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="t1",
                    blocked_by=["ghost"])
    # A dependency on a non-existent task counts as blocking (fail-closed).
    assert dag.claim_next(db, room_id=ROOM, owner="alice") is None


def test_claim_next_tie_break_lowest_seq(db):
    dag.create_task(db, room_id=ROOM, subject="A")  # t1 seq1
    dag.create_task(db, room_id=ROOM, subject="B")  # t2 seq2
    first = dag.claim_next(db, room_id=ROOM, owner="alice")
    assert first["task_id"] == "t1"
    second = dag.claim_next(db, room_id=ROOM, owner="bob")
    assert second["task_id"] == "t2"
    # Nothing left.
    assert dag.claim_next(db, room_id=ROOM, owner="carol") is None


def test_two_claims_never_take_the_same_task(db):
    dag.create_task(db, room_id=ROOM, subject="Only one")
    a = dag.claim_next(db, room_id=ROOM, owner="alice")
    b = dag.claim_next(db, room_id=ROOM, owner="bob")
    assert a is not None and a["task_id"] == "t1"
    assert b is None  # bob gets nothing — the single task is already owned


def test_claim_specific_task(db):
    dag.create_task(db, room_id=ROOM, subject="A")
    dag.create_task(db, room_id=ROOM, subject="B")
    got = dag.claim_task(db, room_id=ROOM, task_id="t2", owner="bob")
    assert got["task_id"] == "t2" and got["owner"] == "bob"
    with pytest.raises(dag.RoomTaskError):
        dag.claim_task(db, room_id=ROOM, task_id="t2", owner="carol")


def test_claim_specific_blocked_task_refused(db):
    dag.create_task(db, room_id=ROOM, subject="Design", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="t2",
                    blocked_by=["t1"])
    with pytest.raises(dag.RoomTaskError):
        dag.claim_task(db, room_id=ROOM, task_id="t2", owner="bob")


def test_assign_overrides_ordering_but_refuses_blocked(db):
    dag.create_task(db, room_id=ROOM, subject="A")  # t1
    dag.create_task(db, room_id=ROOM, subject="B")  # t2
    # Push-assign t2 to bob even though t1 has the lower seq.
    got = dag.assign_task(db, room_id=ROOM, task_id="t2", owner="bob")
    assert got["owner"] == "bob" and got["status"] == "in_progress"
    # A blocked task cannot be assigned.
    dag.create_task(db, room_id=ROOM, subject="Design", task_id="d")
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="b",
                    blocked_by=["d"])
    with pytest.raises(dag.RoomTaskError):
        dag.assign_task(db, room_id=ROOM, task_id="b", owner="carol")


def test_release_returns_to_pending(db):
    dag.create_task(db, room_id=ROOM, subject="Solo")
    dag.claim_next(db, room_id=ROOM, owner="alice")
    rel = dag.release_task(db, room_id=ROOM, task_id="t1")
    assert rel["status"] == "pending" and rel["owner"] is None
    # Re-claimable after release.
    again = dag.claim_next(db, room_id=ROOM, owner="bob")
    assert again["task_id"] == "t1" and again["owner"] == "bob"


def test_add_dependency_after_creation(db):
    dag.create_task(db, room_id=ROOM, subject="Design", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="t2")
    dag.add_dependency(db, room_id=ROOM, task_id="t2", blocked_by="t1")
    task = dag.get_task(db, room_id=ROOM, task_id="t2")
    assert task["blockedBy"] == ["t1"]
    # t2 now blocked → only t1 claimable.
    assert dag.claim_next(db, room_id=ROOM, owner="a")["task_id"] == "t1"


def test_cycle_rejection_self_edge(db):
    dag.create_task(db, room_id=ROOM, subject="A", task_id="t1")
    with pytest.raises(dag.RoomTaskError):
        dag.add_dependency(db, room_id=ROOM, task_id="t1", blocked_by="t1")


def test_cycle_rejection_two_cycle(db):
    dag.create_task(db, room_id=ROOM, subject="A", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="B", task_id="t2",
                    blocked_by=["t1"])  # t2 depends on t1
    with pytest.raises(dag.RoomTaskError):
        # t1 depends on t2 would close t1->t2->t1
        dag.add_dependency(db, room_id=ROOM, task_id="t1", blocked_by="t2")


def test_cycle_rejection_transitive(db):
    dag.create_task(db, room_id=ROOM, subject="A", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="B", task_id="t2",
                    blocked_by=["t1"])
    dag.create_task(db, room_id=ROOM, subject="C", task_id="t3",
                    blocked_by=["t2"])
    with pytest.raises(dag.RoomTaskError):
        # t1 depends on t3 closes t1->t3->t2->t1
        dag.add_dependency(db, room_id=ROOM, task_id="t1", blocked_by="t3")
    # And the whole edge set is unchanged / still acyclic and claimable.
    assert dag.claim_next(db, room_id=ROOM, owner="a")["task_id"] == "t1"


def test_create_with_cyclic_dep_writes_nothing(db):
    dag.create_task(db, room_id=ROOM, subject="A", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="B", task_id="t2",
                    blocked_by=["t1"])
    with pytest.raises(dag.RoomTaskError):
        dag.create_task(db, room_id=ROOM, subject="C", task_id="t3",
                        blocked_by=["t2", "t3"])  # self-ref in the same create
    # t3 must not exist (rolled back).
    assert dag.get_task(db, room_id=ROOM, task_id="t3") is None


def test_list_and_survey_both_directions(db):
    dag.create_task(db, room_id=ROOM, subject="Design", task_id="t1")
    dag.create_task(db, room_id=ROOM, subject="Build", task_id="t2",
                    blocked_by=["t1"])
    tasks = {t["task_id"]: t for t in dag.list_tasks(db, room_id=ROOM)}
    assert tasks["t2"]["blockedBy"] == ["t1"]
    assert tasks["t1"]["blocks"] == ["t2"]  # inverse edge


def test_rooms_are_isolated(db):
    dag.create_task(db, room_id="A", subject="a-task")
    dag.create_task(db, room_id="B", subject="b-task")
    assert len(dag.list_tasks(db, room_id="A")) == 1
    assert len(dag.list_tasks(db, room_id="B")) == 1
    # Independent id spaces.
    assert dag.list_tasks(db, room_id="A")[0]["task_id"] == "t1"
    assert dag.list_tasks(db, room_id="B")[0]["task_id"] == "t1"


def test_additive_tables_invisible_to_hosted_rooms_schema_guard(tmp_path):
    # The load-bearing safety claim: creating the C3 tables in a state.db that
    # hosted_rooms also manages must not trip its schema guard.
    from gateway import hosted_rooms
    dbp = tmp_path / "state.db"
    hosted_rooms.create_room(
        dbp, room_id=ROOM, name="R", members=[
            {"member_id": "m1", "profile": "plan", "handle": "plan",
             "target": {"kind": "local", "profile": "plan"}},
        ], authority_gateway_id="g", now=1,
    )
    dag.create_task(dbp, room_id=ROOM, subject="coexist")
    # hosted_rooms still reads its own room fine after the extra tables exist.
    assert hosted_rooms.read_events(
        dbp, room_id=ROOM, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT
    ) is not None
    assert dag.list_tasks(dbp, room_id=ROOM)[0]["subject"] == "coexist"
