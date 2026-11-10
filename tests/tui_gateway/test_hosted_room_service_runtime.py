"""Research, deferred-recovery, and stop behavior for the hosted room service."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from tests.tui_gateway.test_hosted_room_service import (
    _BlockingFirstRPC,
    _FakeRPC,
    _PromptRecordingRPC,
    _append_room_event,
    _server,
    _wait_for,
)
from tui_gateway.hosted_room_service import HostedRoomService


class _ResearchTeamRPC(_FakeRPC):
    """Scripted model transport that still exercises the real Room runtime."""

    def __init__(self, *, sessions=None) -> None:
        super().__init__()
        self.sessions = sessions if sessions is not None else {}
        self.prompts: list[tuple[str, str]] = []
        self.counts: dict[str, int] = {}
        self.creates: list[tuple[str, str]] = []
        self.resumes: list[tuple[str, str]] = []
        self.submits: list[tuple[str, str]] = []

    def create(self, *, profile, title, source):
        self.creates.append((profile, title))
        return super().create(profile=profile, title=title, source=source)

    def resume(self, *, profile, session_id, source):
        self.resumes.append((profile, session_id))
        return super().resume(profile=profile, session_id=session_id, source=source)

    def submit(self, **kwargs):
        profile = kwargs["profile"]
        prompt = kwargs["prompt"]
        self.prompts.append((profile, prompt))
        self.submits.append((profile, kwargs["session_id"]))
        index = self.counts.get(profile, 0)
        self.counts[profile] = index + 1
        responses = {
            ("research", 0): "@site_a research site A with URL, time, facts, confidence. @site_b research site B using the same schema.",
            ("site_a", 0): "A: https://a.example/alpha at 09:00 says 10 affected; confidence medium. @research compare.",
            ("site_b", 0): "B: https://b.example/alpha at 10:00 says official update has 12 affected. @research compare.",
            ("research", 1): "@site_a verify A revision time. @site_b verify B cites the official update.",
            ("site_a", 1): "A revised at 10:15 but retains initial bulletin 10. @research verified.",
            ("site_b", 1): "B cites the 10:00 official update with 12. @research verified.",
            ("research", 2): "Leader report: A reports initial 10; B reports later official 12; temporal difference verified.",
        }
        text = responses.get((profile, index), "(pass)")
        kwargs["on_terminal"]({"status": "settled", "text": text})
        return {"accepted": True}


def test_research_team_runs_parallel_followups_and_synthesis_through_runtime(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _ResearchTeamRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "research", "site_a", "site_b")
    service.create_room(
        room_id="research-room",
        name="News research",
        members=[
            {"member_id": "c", "profile": "research", "handle": "research", "role": "decider"},
            {"member_id": "a", "profile": "site_a", "handle": "site_a"},
            {"member_id": "b", "profile": "site_b", "handle": "site_b"},
        ],
    )
    service.start()
    service.send(
        room_id="research-room",
        event_id="research-request",
        payload={"text": "Compare sites A and B and give one verified report.", "thread_id": "research-thread"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            and event["payload"].get("member_id") == "c"
            and str(event["payload"].get("text") or "").startswith("Leader report:")
            for event in service._events("research-room")
        ),
        timeout=5,
    )
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            and event["payload"].get("status") == "settled"
            for event in service._events("research-room")
        ),
        timeout=5,
    )
    assert service.stop(timeout=2)

    assert rpc.counts["site_a"] == 2
    assert rpc.counts["site_b"] == 2
    assert rpc.counts["research"] in {3, 4}
    first_worker_indices = {
        profile: next(i for i, value in enumerate(rpc.prompts) if value[0] == profile)
        for profile in ("site_a", "site_b")
    }
    assert all(index > 0 for index in first_worker_indices.values())
    second_coordinator = [prompt for profile, prompt in rpc.prompts if profile == "research"][1]
    assert "https://a.example/alpha" in second_coordinator
    assert "https://b.example/alpha" in second_coordinator
    transcript = "\n".join(
        event["payload"].get("text", "")
        for event in service._events("research-room")
        if event["kind"] == "message.member"
    )
    assert "revised at 10:15" in transcript
    assert "10:00 official update" in transcript
    events = service._events("research-room")
    member_messages = [event for event in events if event["kind"] == "message.member"]
    reports = [
        event
        for event in member_messages
        if event["payload"]["member_id"] == "c"
        and event["payload"]["text"].startswith("Leader report:")
    ]
    assert len(reports) == 1
    assert all(
        not event["payload"]["text"].startswith("Leader report:")
        for event in member_messages
        if event["payload"]["member_id"] != "c"
    )
    assert not driver.list_tasks(db, room_id="research-room", status="running")
    assert not driver.list_tasks(db, room_id="research-room", status="queued")


def test_research_team_restarts_midway_and_finishes_from_durable_room_state(tmp_path: Path):
    db = tmp_path / "state.db"
    first = HostedRoomService(_server(), db_path=db)
    first_rpc = _ResearchTeamRPC()
    first.rpc = first_rpc
    first.runtime.rpc = first_rpc
    first.local_profiles = lambda: ("research", "site_a", "site_b")
    first.create_room(
        room_id="research-room",
        name="Restartable research",
        members=[
            {"member_id": "c", "profile": "research", "handle": "research", "role": "decider"},
            {"member_id": "a", "profile": "site_a", "handle": "site_a"},
            {"member_id": "b", "profile": "site_b", "handle": "site_b"},
        ],
    )
    binding = first.bindings()[0]
    first.send(
        room_id="research-room",
        event_id="research-request",
        payload={"text": "Compare A and B with follow-up verification.", "thread_id": "research-thread"},
    )
    # Run only the opening coordinator plus both initial researchers, then
    # replace the whole service/runtime object while the synthesis is pending.
    for _ in range(3):
        first.runtime._process_room(binding)
    before = first._events("research-room")
    assert any(
        event["kind"] == "message.member" and event["payload"].get("member_id") == "a"
        for event in before
    )
    assert any(
        event["kind"] == "message.member" and event["payload"].get("member_id") == "b"
        for event in before
    )
    assert not any(
        event["kind"] == "message.member"
        and str(event["payload"].get("text") or "").startswith("Leader report:")
        for event in before
    )
    old_lease = first.runtime._leases["research-room"]
    driver.release_lease(db, old_lease, clock=time.time)

    created_sessions = dict(first_rpc.sessions)
    assert {profile for profile, _title in first_rpc.creates} == {
        "research",
        "site_a",
        "site_b",
    }
    assert len(first_rpc.creates) == 3

    restarted = HostedRoomService(_server(), db_path=db)
    restarted_rpc = _ResearchTeamRPC(sessions=created_sessions)
    # The new model sessions resume at the synthesis turn; prior responses are
    # recovered from SQLite rather than regenerated through this transport.
    restarted_rpc.counts.update({"research": 1, "site_a": 1, "site_b": 1})
    restarted.rpc = restarted_rpc
    restarted.runtime.rpc = restarted_rpc
    restarted.local_profiles = lambda: ("research", "site_a", "site_b")
    restarted_binding = restarted.bindings()[0]
    for _ in range(8):
        restarted.runtime._process_room(restarted_binding)
        current_events = restarted._events("research-room")
        if (
            restarted_rpc.counts.get("site_a", 0) >= 2
            and restarted_rpc.counts.get("site_b", 0) >= 2
            and any(
                event["kind"] == "message.member"
                and str(event["payload"].get("text") or "").startswith("Leader report:")
                for event in current_events
            )
        ):
            break

    events = restarted._events("research-room")
    reports = [
        event
        for event in events
        if event["kind"] == "message.member"
        and event["payload"].get("member_id") == "c"
        and str(event["payload"].get("text") or "").startswith("Leader report:")
    ]
    assert len(reports) == 1
    assert restarted_rpc.counts["site_a"] == 2
    assert restarted_rpc.counts["site_b"] == 2
    assert restarted_rpc.counts["research"] in {3, 4}
    assert restarted_rpc.creates == []
    resumed_profiles = {profile for profile, _session_id in restarted_rpc.resumes}
    assert resumed_profiles == {"research", "site_a", "site_b"}
    expected_session_ids = {
        profile: created_sessions[(profile, "Group: research-room")]["session_id"]
        for profile in ("research", "site_a", "site_b")
    }
    assert all(
        session_id == expected_session_ids[profile]
        for profile, session_id in restarted_rpc.resumes
    )
    assert all(
        session_id == expected_session_ids[profile]
        for profile, session_id in restarted_rpc.submits
    )
    for _ in range(3):
        restarted.runtime._process_room(restarted_binding)
        if not driver.list_tasks(db, room_id="research-room", status="queued"):
            break
    assert not driver.list_tasks(db, room_id="research-room", status="queued")
    assert not driver.list_tasks(db, room_id="research-room", status="running")


def test_research_team_pending_permission_survives_restart_and_relays_exact_action(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service._set_pending_action(
        "research-room",
        "a",
        {
            "kind": "approval",
            "task_id": "site-a-fetch",
            "execution_generation": 2,
            "session_id": "site-a-session",
            "request_id": "allow-site-a",
            "approval": {
                "description": "Fetch protected source A",
                "command": "PRIVATE_RESEARCH_COMMAND",
                "choices": ["once", "deny"],
            },
        },
    )

    restarted = HostedRoomService(_server(), db_path=db)
    rpc = _FakeRPC()
    restarted.rpc = rpc
    restarted.runtime.rpc = rpc
    monkeypatch.setattr(driver, "list_tasks", lambda *_a, **_kw: [{
        "identity": driver.TaskIdentity("research-room", "site-a-fetch", "thread-1", "turn-1"),
        "execution_generation": 2, "status": "running", "payload": {"target_profile": "a"}}])
    actions = restarted.pending_actions("research-room")["actions"]
    assert len(actions) == 1
    assert actions[0]["action_id"] == "allow-site-a"
    assert actions[0]["kind"] == "permission"
    assert actions[0]["description"] == "Fetch protected source A"
    assert "PRIVATE_RESEARCH_COMMAND" not in json.dumps(actions[0])

    resolved = restarted.handle_action(
        "research-room", "allow-site-a", "approve"
    )
    assert resolved["ok"] is True
    assert resolved["result"] == {"resolved": 1}
    assert rpc.approvals == [
        {
            "session_id": "site-a-session",
            "request_id": "allow-site-a",
            "choice": "once",
        }
    ]
    assert restarted.pending_actions("research-room")["actions"] == []


def test_same_thread_followup_migrates_and_delivers_committed_peer_reply(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _PromptRecordingRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Shared context room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops provide the marker", "thread_id": "thread-1"},
    )
    _wait_for(lambda: len(service.rpc.prompts) == 1)
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            and event["payload"]["discussion_event_id"] == "user-1"
            for event in service._events("room-1")
        )
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM hosted_room_policy_transcript
               WHERE room_id='room-1' AND thread_id='thread-1'"""
        ).fetchone()[0] == 2
        conn.execute("DELETE FROM hosted_room_policy_transcript")
        conn.execute(
            """DELETE FROM hosted_room_policy_transcript_state
               WHERE room_id='room-1'"""
        )
    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@hermes continue", "thread_id": "thread-1"},
    )
    _wait_for(lambda: len(service.rpc.prompts) == 2)
    assert service.stop(timeout=5.0)

    profile, prompt = service.rpc.prompts[1]
    assert profile == "default"
    assert "@ops: reply from ops" in prompt
    assert "User (user): @hermes continue" in prompt


def test_active_same_thread_followup_waits_for_current_task(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _BlockingFirstRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Serialized room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops start", "thread_id": "thread-1"},
    )
    assert service.rpc.first_started.wait(timeout=2)
    service.send(
        room_id="room-1",
        event_id="user-2",
        payload={"text": "@hermes follow up", "thread_id": "thread-1"},
    )
    assert len(service.rpc.prompts) == 1
    service.rpc.release_first.set()
    _wait_for(lambda: len(service.rpc.prompts) == 2)
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            and event["payload"]["discussion_event_id"] == "user-2"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=5.0)
    assert "User (user): @hermes follow up" in service.rpc.prompts[1][1]


def test_thread_transcript_prunes_committed_message_and_settlement_together(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Bounded room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.start()
    service.send(
        room_id="room-1",
        event_id="user-first",
        payload={"text": "@ops old", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "room.activity"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=5.0)
    for index in range(24):
        _append_room_event(
            db,
            room_id="room-1",
            event_id=f"user-tail-{index}",
            kind="message.user",
            actor={"kind": "user", "id": "desktop"},
            payload={"text": f"tail {index}", "thread_id": "thread-1"},
        )

    room = hosted_rooms.room_state(db, room_id="room-1")
    snapshot = service._policy_snapshot(room)
    assert len(snapshot.events) == 24
    assert {event["kind"] for event in snapshot.events} == {"message.user"}
    discussion.plan_next_task(
        room,
        snapshot.events,
        local_profiles=service.local_profiles(),
        initial_watermarks=snapshot.watermarks,
    )


def test_service_derives_room_deadline_from_agent_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "90")

    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")

    assert service.runtime.turn_timeout_seconds == 120.0


def test_service_publishes_deferred_turn_continues_and_retries_new_generation(
    tmp_path: Path,
):
    now = [100.0]

    def clock():
        return now[0]

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.runtime.clock = clock
    service.runtime.lease_ttl_seconds = 30
    service.runtime.indeterminate_defer_seconds = 5
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Resilient room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-resilience",
        payload={"text": "Check this", "thread_id": "thread-1"},
    )
    first = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    old_lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=service.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="offline-member",
        ttl_seconds=1,
        clock=clock,
    )
    old_attempt = driver.start_task(
        db,
        first["identity"],
        old_lease,
        expected_cancel_generation=0,
        clock=clock,
    )

    now[0] = 102.0
    binding = service.bindings()[0]
    service.runtime._process_room(binding)
    now[0] = 108.0
    service.runtime._process_room(binding)

    events = service._events("room-1")
    deferred = next(event for event in events if event["kind"] == "turn.deferred")
    assert deferred["payload"]["task_id"] == first["identity"].task_id
    assert deferred["payload"]["execution_generation"] == 1
    # Ordinary discussion deferrals do not stall independent later members:
    # only server-owned manual-DAG anchors are held for exact retry.
    assert any(
        event["kind"] == "message.member"
        and event["payload"]["member_id"] == "ops"
        for event in events
    )

    retry_action = service.pending_actions("room-1")["actions"][0]
    assert retry_action["kind"] == "retry"
    assert retry_action["detail"]["task_id"] == first["identity"].task_id
    assert retry_action["detail"]["status"] == "deferred"
    handled = service.handle_action(
        "room-1", retry_action["action_id"], "approve"
    )
    assert handled == {"ok": True, "message": "Room turn retry queued"}
    requeued = driver.get_task(db, first["identity"])
    assert requeued["status"] == "queued"
    assert service.pending_actions("room-1")["actions"] == []

    service.runtime._process_room(binding)
    retried = driver.get_task(db, first["identity"])
    assert retried["status"] == "settled"
    service.runtime._process_room(binding)
    assert retried["execution_generation"] == old_attempt.execution_generation + 1
    retried_events = service._events("room-1")
    assert any(
        event["kind"] == "message.member"
        and event["payload"]["member_id"] == "ops"
        for event in retried_events
    )
    assert sum(
        event["kind"] == "turn.settled"
        and event["payload"]["task_id"] == first["identity"].task_id
        for event in retried_events
    ) == 1


def test_research_team_deferred_site_retry_advances_generation_and_settles(tmp_path: Path):
    now = [100.0]

    def clock():
        return now[0]

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.runtime.clock = clock
    service.runtime.lease_ttl_seconds = 30
    service.runtime.indeterminate_defer_seconds = 5
    service.local_profiles = lambda: ("research", "site_a", "site_b")
    service.create_room(
        room_id="research-room",
        name="Resilient research",
        members=[
            {"member_id": "c", "profile": "research", "handle": "research", "role": "decider"},
            {"member_id": "a", "profile": "site_a", "handle": "site_a"},
            {"member_id": "b", "profile": "site_b", "handle": "site_b"},
        ],
    )
    _append_room_event(
        db,
        room_id="research-room",
        event_id="dagdispatch:site-a-fetch",
        kind="message.user",
        actor={"kind": "user", "id": "task-dag"},
        payload={
            "text": "@site_a inspect source A",
            "thread_id": "dagtask:site-a-fetch",
        },
    )
    service.prepare_room(service.bindings()[0])
    task = driver.list_tasks(db, room_id="research-room", status="queued")[0]
    old_lease = driver.acquire_lease(
        db,
        room_id="research-room",
        gateway_id=service.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="crashed-researcher-a",
        ttl_seconds=1,
        clock=clock,
    )
    old_attempt = driver.start_task(
        db,
        task["identity"],
        old_lease,
        expected_cancel_generation=0,
        clock=clock,
    )
    now[0] = 102.0
    binding = service.bindings()[0]
    service.runtime._process_room(binding)
    now[0] = 108.0
    service.runtime._process_room(binding)

    deferred = driver.get_task(db, task["identity"])
    assert deferred["status"] == "deferred"
    action = next(
        item
        for item in service.pending_actions("research-room")["actions"]
        if item["kind"] == "retry"
    )
    assert action["from_handle"] == "a"
    assert action["detail"]["status"] == "deferred"
    assert service.handle_action(
        "research-room", action["action_id"], "deny"
    )["ok"] is False
    assert service.handle_action(
        "research-room", action["action_id"], "approve"
    ) == {"ok": True, "message": "Room turn retry queued"}
    service.runtime._process_room(binding)
    retried = driver.get_task(db, task["identity"])
    assert retried["status"] == "settled"
    assert retried["execution_generation"] == old_attempt.execution_generation + 1


def test_research_team_stop_cancels_all_active_research_without_final_report(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("research", "site_a", "site_b")
    service.create_room(
        room_id="research-room",
        name="Cancelled research",
        members=[
            {"member_id": "c", "profile": "research", "handle": "research", "role": "decider"},
            {"member_id": "a", "profile": "site_a", "handle": "site_a"},
            {"member_id": "b", "profile": "site_b", "handle": "site_b"},
        ],
    )
    service.send(
        room_id="research-room",
        event_id="research-request",
        payload={"text": "Compare source A and source B", "thread_id": "research-thread"},
    )
    assert driver.list_tasks(db, room_id="research-room", status="queued")
    assert service.stop_room("research-room", cancel_id="leader-cancel") == 1
    service.prepare_room(service.bindings()[0])
    tasks = driver.list_tasks(db, room_id="research-room")
    assert tasks and all(task["status"] == "cancelled" for task in tasks)
    events = service._events("research-room")
    assert any(event["kind"] == "room.stop_requested" for event in events)
    assert not any(
        event["kind"] == "message.member"
        and str(event["payload"].get("text") or "").startswith("Leader report:")
        for event in events
    )


def test_stop_fence_prevents_the_next_room_member_from_starting(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    monkeypatch.setattr(service, "local_profiles", lambda: ("default", "ops"))
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "Inspect the release", "thread_id": "thread-1"},
    )
    assert len(driver.list_tasks(db, room_id="room-1")) == 1

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    service.prepare_room(service.bindings()[0])

    tasks = driver.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "cancelled"
    assert any(
        event["kind"] == "room.stop_requested" for event in service._events("room-1")
    )


def test_acknowledged_stop_refuses_to_disband_while_exact_turn_is_still_running(
    tmp_path: Path,
):
    class PendingStopRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.active_task_id = None

        def info(self, *, profile, session_id, source):
            return {"active": True, "task_id": self.active_task_id}

        def interrupt(self, *, profile, session_id, source, expected_task_id):
            return None

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = PendingStopRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="worker",
        ttl_seconds=30,
        clock=time.time,
    )
    driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    rpc.sessions[("ops", "Group: room-1")] = {"session_id": "ops-session"}
    rpc.active_task_id = task["identity"].task_id

    with pytest.raises(RuntimeError, match="still stopping"):
        service.stop_room(
            "room-1",
            cancel_id="stop-1",
            require_acknowledged=True,
        )

    stopping = driver.get_task(db, task["identity"])
    assert stopping["status"] == "stopping"
    assert stopping["cancel_id"] == "stop-1"
