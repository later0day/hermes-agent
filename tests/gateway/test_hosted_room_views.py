import json
from pathlib import Path

from gateway import hosted_room_driver as driver
from gateway import hosted_room_views as views
from gateway import hosted_rooms
from gateway import room_task_dag as dag
from gateway import hosted_room_actions as actions
from gateway.hosted_room_actions import set_pending_action

ROOM = "room-ui"


def _room(db: Path) -> dict:
    return hosted_rooms.create_room(
        db,
        room_id=ROOM,
        name="Release room",
        members=[
            {"member_id": "lead", "handle": "lead", "profile": "default", "role": "coordinator"},
            {"member_id": "worker", "handle": "worker", "profile": "worker", "role": "teammate"},
        ],
        authority_gateway_id="gateway-a",
        now=1,
    )


def _event(db: Path, *, event_id: str, kind: str, actor: dict, payload: dict, now: float) -> dict:
    return hosted_rooms.append_event(
        db,
        room_id=ROOM,
        event_id=event_id,
        kind=kind,
        actor=actor,
        payload=payload,
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        now=now,
    )


def test_project_manual_dag_and_pending_action():
    raw = [
        {"task_id": "t1", "status": "completed", "blockedBy": [], "subject": "Design"},
        {"task_id": "t2", "status": "pending", "blockedBy": ["t1"], "subject": "Build"},
        {"task_id": "t3", "status": "pending", "blockedBy": ["t2"], "subject": "Ship"},
    ]
    actions = [{"action_id": "a1", "task_id": "t2", "kind": "approval"}]
    projected = views.project_manual_tasks(raw, actions=actions)
    assert [task["visual_state"] for task in projected] == ["completed", "needs_action", "blocked"]
    assert projected[1]["pending_actions"][0]["action_id"] == "a1"


def test_conversation_and_activity_do_not_invent_success():
    events = [
        {"room_id": ROOM, "seq": 1, "event_id": "u1", "kind": "message.user", "actor": {"kind": "user", "id": "user"}, "payload": {"text": "Do it", "thread_id": "thread"}, "created_at": 1},
        {"room_id": ROOM, "seq": 2, "event_id": "f1", "kind": "turn.failed", "actor": {"kind": "gateway", "id": "gateway-a"}, "payload": {"task_id": "task-1", "thread_id": "thread", "member_id": "worker", "error": "model unavailable"}, "created_at": 2},
    ]
    conversation = views.project_conversation(events)
    assert conversation[0]["text"] == "Do it"
    assert conversation[0]["terminal_kind"] is None
    activity = views.project_activity(events)
    assert activity[1]["category"] == "errors"
    assert activity[1]["title"] == "@worker task-1 failed"
    assert activity[1]["summary"] == "model unavailable"


def test_conversation_terminal_requires_exact_turn_coordinate():
    events = [
        {"seq": 1, "event_id": "m-old", "kind": "message.member", "actor": {"kind": "member", "id": "worker"}, "payload": {"text": "old", "task_id": "same-task", "thread_id": "thread", "turn_id": "turn-old"}, "created_at": 1},
        {"seq": 2, "event_id": "c-old", "kind": "turn.cancelled", "actor": {"kind": "gateway", "id": "g"}, "payload": {"task_id": "same-task", "thread_id": "thread", "turn_id": "turn-old", "member_id": "worker", "reason": "superseded_by_newer_user_event"}, "created_at": 2},
        {"seq": 3, "event_id": "m-new", "kind": "message.member", "actor": {"kind": "member", "id": "worker"}, "payload": {"text": "new", "task_id": "same-task", "thread_id": "thread", "turn_id": "turn-new"}, "created_at": 3},
        {"seq": 4, "event_id": "s-new", "kind": "turn.settled", "actor": {"kind": "gateway", "id": "g"}, "payload": {"task_id": "same-task", "thread_id": "thread", "turn_id": "turn-new", "member_id": "worker"}, "created_at": 4},
    ]
    conversation = views.project_conversation(events)
    assert [item["terminal_kind"] for item in conversation] == [
        "turn.cancelled",
        "turn.settled",
    ]


def test_supersession_and_deferred_are_explicit_activity():
    events = [
        {"room_id": ROOM, "seq": 1, "event_id": "c1", "kind": "turn.cancelled", "actor": {"kind": "gateway", "id": "g"}, "payload": {"task_id": "old", "member_id": "worker", "reason": "superseded_by_newer_user_event"}, "created_at": 1},
        {"room_id": ROOM, "seq": 2, "event_id": "d1", "kind": "turn.deferred", "actor": {"kind": "gateway", "id": "g"}, "payload": {"task_id": "new", "member_id": "worker", "reason": "gateway restarted", "execution_generation": 1}, "created_at": 2},
    ]
    items = views.project_activity(events)
    assert items[0]["summary"] == "superseded_by_newer_user_event"
    assert items[1]["title"].endswith("needs retry")


def test_workspace_joins_real_durable_stores(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    dag.create_task(db, room_id=ROOM, task_id="t1", subject="Design")
    dag.create_task(db, room_id=ROOM, task_id="t2", subject="Build", blocked_by=["t1"])
    dag.claim_task(db, room_id=ROOM, task_id="t1", owner="worker")
    _event(db, event_id="u1", kind="message.user", actor={"kind": "user", "id": "user"}, payload={"text": "Start", "thread_id": "thread-1"}, now=2)
    set_pending_action(db, room_id=ROOM, member_id="worker", action={"action_id": "request-1", "request_id": "request-1", "kind": "approval", "task_id": "t1", "created_at": 3})

    result = views.build_room_workspace(db, room_id=ROOM)

    assert result["room"]["room_id"] == ROOM
    assert [task["task_id"] for task in result["tasks"]] == ["t1", "t2"]
    assert result["tasks"][0]["visual_state"] == "needs_action"
    assert result["tasks"][1]["visual_state"] == "blocked"
    assert result["pending_actions"][0]["member_id"] == "worker"
    assert result["pending_actions"][0]["action_id"] == "request-1"
    assert result["pending_actions"][0]["kind"] == "permission"
    assert result["pending_actions"][0]["from_handle"] == "worker"
    assert result["conversation"][0]["text"] == "Start"
    assert result["log"]["has_more"] is False


def test_manual_task_and_its_driver_attempt_are_not_counted_twice(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    dag.create_task(db, room_id=ROOM, task_id="t1", subject="Build")
    assert dag.claim_task_for_dispatch(
        db,
        room_id=ROOM,
        task_id="t1",
        owner="worker",
        dispatch_thread_id="dagtask:t1",
    )
    identity = driver.TaskIdentity(ROOM, "driver-t1", "dagtask:t1", "turn-1")
    driver.admit_task(
        db,
        identity=identity,
        payload={"target_profile": "worker", "prompt": "do it", "source_event_seq": 1},
        clock=lambda: 2,
    )
    result = views.build_room_workspace(db, room_id=ROOM)
    assert [task["task_id"] for task in result["tasks"]] == ["t1"]
    assert result["tasks"][0]["latest_attempt"]["identity"]["task_id"] == "driver-t1"


def test_room_summary_classifies_terminal_failures_as_failed():
    for status in ("failed", "cancelled", "indeterminate"):
        result = views.room_summary(
            {"room_id": ROOM, "updated_at": 1},
            tasks=[{"task_id": "t1", "status": status, "visual_state": status}],
            actions=[],
        )
        assert result["workspace"]["state"] == "failed"
        assert result["workspace"]["health"] == "critical"


def test_driver_attempt_serializes_identity(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    identity = driver.TaskIdentity(ROOM, "task-1", "thread-1", "turn-1")
    driver.admit_task(
        db,
        identity=identity,
        payload={"target_profile": "worker", "prompt": "do it", "source_event_seq": 1},
        clock=lambda: 2,
    )
    result = views.build_room_workspace(db, room_id=ROOM)
    assert result["attempts"][0]["identity"] == {"room_id": ROOM, "task_id": "task-1", "thread_id": "thread-1", "turn_id": "turn-1"}
    assert result["attempts"][0]["status"] == "queued"
    assert "payload" not in result["attempts"][0]
    assert "result" not in result["attempts"][0]
    assert result["attempts"][0]["redacted"] is True


def test_workspace_projects_retry_action_for_uncertain_attempt(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    identity = driver.TaskIdentity(ROOM, "retry-task", "retry-thread", "retry-turn")
    driver.admit_task(
        db,
        identity=identity,
        payload={"target_profile": "worker", "prompt": "private", "source_event_seq": 1},
        clock=lambda: 1,
    )
    lease = driver.acquire_lease(
        db,
        room_id=ROOM,
        gateway_id="gateway-a",
        authority_epoch=1,
        process_generation="process-a",
        ttl_seconds=30,
        clock=lambda: 2,
    )
    driver.start_task(
        db,
        identity,
        lease,
        expected_cancel_generation=0,
        clock=lambda: 3,
    )
    hosted_rooms.claim_authority(
        db,
        room_id=ROOM,
        expected_gateway_id="gateway-a",
        expected_epoch=1,
        new_gateway_id="gateway-b",
        event_id="claim-b",
        now=4,
    )
    new_lease = driver.acquire_lease(
        db,
        room_id=ROOM,
        gateway_id="gateway-b",
        authority_epoch=2,
        process_generation="process-b",
        ttl_seconds=30,
        clock=lambda: 5,
    )
    driver.recover_room(db, new_lease, clock=lambda: 6)
    workspace = views.build_room_workspace(db, room_id=ROOM)
    action = workspace["pending_actions"][0]
    assert action["action_id"] == "retry:retry-task:1:0"
    assert action["kind"] == "retry"
    assert action["detail"] == {
        "task_id": "retry-task",
        "thread_id": "retry-thread",
        "status": "indeterminate",
        "execution_generation": 1,
    }


def test_workspace_omits_private_driver_action_and_event_payloads(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    dag.create_task(db, room_id=ROOM, task_id="t1", subject="Safe title")
    identity = driver.TaskIdentity(ROOM, "driver-secret", "secret-thread", "secret-turn")
    driver.admit_task(
        db,
        identity=identity,
        payload={"target_profile": "worker", "prompt": "PRIVATE_PROMPT_CANARY", "source_event_seq": 1},
        clock=lambda: 2,
    )
    _event(
        db,
        event_id="private-event",
        kind="message.user",
        actor={"kind": "user", "id": "user"},
        payload={"text": "Visible conversation", "secret": "PRIVATE_EVENT_CANARY"},
        now=3,
    )
    actions.set_pending_action(
        db,
        room_id=ROOM,
        member_id="worker",
        action={"request_id": "safe-action", "kind": "approval", "approval": {"tool": "terminal", "command": "PRIVATE_COMMAND_CANARY"}},
    )
    workspace = views.build_room_workspace(db, room_id=ROOM)
    encoded = json.dumps(workspace)
    assert "PRIVATE_PROMPT_CANARY" not in encoded
    assert "PRIVATE_EVENT_CANARY" not in encoded
    assert "PRIVATE_COMMAND_CANARY" not in encoded
    assert "Visible conversation" in encoded
    assert workspace["pending_actions"][0]["detail"]["tool_name"] == "terminal"
    assert workspace["log"]["redacted"] is True
