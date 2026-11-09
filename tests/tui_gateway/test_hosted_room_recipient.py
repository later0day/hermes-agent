"""Explicit Dashboard recipients preserve leader routing and worker DAG dispatch."""

from pathlib import Path

import pytest

from gateway import hosted_room_driver as driver
from gateway import room_task_dag as dag
from tests.tui_gateway.test_hosted_room_service import _FakeRPC, _server
from tests.waiting import wait_until
from tui_gateway.hosted_room_service import HostedRoomService


def _service(db: Path) -> HostedRoomService:
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = service.runtime.rpc = _FakeRPC()
    service.local_profiles = lambda: ("lead", "worker", "other")
    service.create_room(
        room_id="room-1",
        name="Recipient room",
        members=[
            {"member_id": "lead-id", "profile": "lead", "handle": "lead", "role": "decider"},
            {"member_id": "worker-id", "profile": "worker", "handle": "worker", "role": "worker"},
            {"member_id": "other-id", "profile": "other", "handle": "other", "role": "worker"},
        ],
    )
    return service


def test_worker_recipient_runs_only_that_worker_and_completes_its_dag_task(tmp_path: Path):
    db = tmp_path / "state.db"
    service = _service(db)
    service.start()
    try:
        event = service.send(
            room_id="room-1",
            event_id="worker-request",
            payload={"text": "@worker build the API", "thread_id": "browser-thread"},
            recipient="worker",
        )
        assert event["actor"] == {"kind": "user", "id": "task-dag"}
        assert event["payload"]["thread_id"].startswith("dagtask:")
        wait_until(
            lambda: (tasks := dag.list_tasks(db, room_id="room-1"))
            and tasks[0]["status"] == "completed",
            what="direct worker DAG task completed",
        )
    finally:
        assert service.stop(timeout=2.0)

    tasks = dag.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["subject"] == "@worker build the API"
    assert tasks[0]["owner"] == "worker"
    attempts = driver.list_tasks(db, room_id="room-1")
    assert len(attempts) == 1
    assert attempts[0]["status"] == "settled"
    assert attempts[0]["payload"]["target_member_id"] == "worker-id"
    replies = [event for event in service._events("room-1") if event["kind"] == "message.member"]
    assert [event["payload"]["member_id"] for event in replies] == ["worker-id"]


def test_worker_recipient_does_not_dispatch_extra_mentioned_worker(tmp_path: Path):
    db = tmp_path / "state.db"
    service = _service(db)
    service.start()
    try:
        service.send(
            room_id="room-1",
            event_id="worker-request",
            payload={"text": "@worker build the API with @other", "thread_id": "browser-thread"},
            recipient="worker",
        )
        wait_until(
            lambda: (tasks := dag.list_tasks(db, room_id="room-1"))
            and tasks[0]["status"] == "completed",
            what="direct worker DAG task completed",
        )
    finally:
        assert service.stop(timeout=2.0)

    attempts = driver.list_tasks(db, room_id="room-1")
    assert len(attempts) == 1
    assert attempts[0]["payload"]["target_member_id"] == "worker-id"
    replies = [event for event in service._events("room-1") if event["kind"] == "message.member"]
    assert [event["payload"]["member_id"] for event in replies] == ["worker-id"]


@pytest.mark.parametrize("text", ["build the API", "@other build the API"])
def test_worker_recipient_ignores_missing_and_conflicting_mentions(tmp_path: Path, text: str):
    db = tmp_path / "state.db"
    service = _service(db)

    service.send(
        room_id="room-1",
        event_id="worker-request",
        payload={"text": text, "thread_id": "browser-thread"},
        recipient="worker",
    )

    tasks = dag.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "in_progress"
    assert tasks[0]["owner"] == "worker"
    attempts = driver.list_tasks(db, room_id="room-1")
    assert len(attempts) == 1
    assert attempts[0]["status"] == "queued"
    assert attempts[0]["payload"]["target_member_id"] == "worker-id"


def test_leader_recipient_keeps_ordinary_decider_ingress(tmp_path: Path):
    db = tmp_path / "state.db"
    service = _service(db)
    service.start()
    try:
        event = service.send(
            room_id="room-1",
            event_id="leader-request",
            payload={"text": "@lead plan the release", "thread_id": "browser-thread"},
            recipient="lead",
        )
        assert event["actor"] == {"kind": "user", "id": "desktop"}
        assert event["payload"]["thread_id"] == "browser-thread"
        wait_until(
            lambda: any(event["kind"] == "turn.settled" for event in service._events("room-1")),
            what="leader turn settled",
        )
    finally:
        assert service.stop(timeout=2.0)

    assert dag.list_tasks(db, room_id="room-1") == []
    attempts = driver.list_tasks(db, room_id="room-1")
    assert len(attempts) == 1
    assert attempts[0]["payload"]["target_member_id"] == "lead-id"


def test_worker_recipient_replay_reuses_event_and_dag_assignment(tmp_path: Path):
    db = tmp_path / "state.db"
    service = _service(db)
    request = {
        "room_id": "room-1",
        "event_id": "worker-request",
        "payload": {"text": "@worker build the API", "thread_id": "browser-thread"},
        "recipient": "worker",
    }

    first = service.send(**request)
    replayed = service.send(**{**request, "recipient": "other"})

    assert replayed["event_id"] == first["event_id"]
    assert len([event for event in service._events("room-1") if event["event_id"] == "worker-request"]) == 1
    tasks = dag.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "in_progress"
    assert tasks[0]["owner"] == "worker"
    attempts = driver.list_tasks(db, room_id="room-1")
    assert len(attempts) == 1
    assert attempts[0]["status"] == "queued"
    assert attempts[0]["payload"]["target_member_id"] == "worker-id"
