"""Integration tests for the hosted Discussion coordinator."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import MAX_ACTIVE_POLICY_EVENTS
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    PROTOCOL_VERSION,
    catalog_mapping,
    issue_room_grant,
)
from tui_gateway.hosted_room_service import (
    HostedRoomService,
    _RouteStatusPeerClient,
    _grant_revoke_is_terminal,
)
from tui_gateway.hosted_room_peer_transport import PeerMemberRoute
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPError


def _append_room_event(db, **kwargs):
    if kwargs.get("kind") == "message.user":
        room = hosted_rooms.room_state(db, room_id=kwargs["room_id"])
        kwargs.setdefault(
            "authority_gateway_id", str(room["authority_gateway_id"])
        )
        kwargs.setdefault("authority_epoch", int(room["authority_epoch"]))
    return hosted_rooms.append_event(db, **kwargs)


class _FakeRPC:
    def __init__(self) -> None:
        self.sessions = {}
        self.approvals = []

    def resolve_exact(self, *, profile, title, source):
        return self.sessions.get((profile, title))

    def create(self, *, profile, title, source):
        session = {"session_id": f"{profile}-session", "title": title}
        self.sessions[(profile, title)] = session
        return session

    def resume(self, *, profile, session_id, source):
        return {"session_id": session_id}

    def submit(
        self,
        *,
        profile,
        session_id,
        prompt,
        source,
        task,
        execution_generation,
        on_terminal,
    ):
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}

    def history(self, *, profile, session_id, source):
        return []

    def info(self, *, profile, session_id, source):
        return {"active": False, "task_id": None}

    def interrupt(self, *, profile, session_id, source, expected_task_id):
        return {"interrupted": True}

    def approve(self, **kwargs):
        self.approvals.append(dict(kwargs))
        return {"resolved": 1}


class _FakePeerClient:
    def __init__(self) -> None:
        self.dispatches = []
        self.revoked = []
        self.session = {"session_id": "peer-group-session"}

    def prepare(self, **kwargs):
        return (
            self.session
            if kwargs["create"] or kwargs.get("expected_session_id")
            else None
        )

    def dispatch(self, **kwargs):
        self.dispatches.append(kwargs["dispatch"])
        return {"status": "accepted", "task_id": kwargs["dispatch"]["task_id"]}

    def history(self, **kwargs):
        if not self.dispatches:
            return []
        dispatch = self.dispatches[-1]
        return [
            {
                "role": "assistant",
                "task_id": dispatch["task_id"],
                "execution_generation": dispatch["execution_generation"],
                "status": "settled",
                "message_id": f"peer:{dispatch['task_id']}",
                "content": "Remote review complete.",
            }
        ]

    def status(self, **kwargs):
        task_id = self.dispatches[-1]["task_id"] if self.dispatches else None
        return {"active": False, "task_id": task_id}

    def stop(self, **kwargs):
        return {"status": "cancelled"}

    def revoke_grant(self, **kwargs):
        self.revoked.append(kwargs["grant"])
        return {"revoked": True}


class _UnavailablePeerClient(_FakePeerClient):
    def prepare(self, **kwargs):
        raise RuntimeError("peer is offline before admission")


class _NotAdmittedPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.offline = True

    def dispatch(self, **kwargs):
        if self.offline:
            raise PeerRunsHTTPError(
                "peer refused the connection",
                retryable=True,
                not_admitted=True,
            )
        return super().dispatch(**kwargs)


class _ExpiredGrantPeerClient(_FakePeerClient):
    def prepare(self, **kwargs):
        raise PeerRunsHTTPError(
            "peer room authorization needs renewal",
            status_code=401,
            error_code="invalid_room_grant",
        )


class _UnavailableRevokePeerClient(_FakePeerClient):
    def revoke_grant(self, **kwargs):
        raise RuntimeError("peer is offline during revocation")


class _ExpiredRevokePeerClient(_FakePeerClient):
    def revoke_grant(self, **kwargs):
        raise PeerRunsHTTPError(
            "peer room authorization needs renewal",
            status_code=401,
            error_code="invalid_room_grant",
        )


class _RefreshingPeerClient(_FakePeerClient):
    def __init__(self, replacement: str, catalog=None) -> None:
        super().__init__()
        self.replacement = replacement
        self.catalog = catalog
        self.refreshed = []
        self.refresh_arguments = []
        self.dispatched_grants = []

    def refresh_grant(self, **kwargs):
        self.refreshed.append(kwargs["grant"])
        self.refresh_arguments.append(dict(kwargs))
        return {
            "grant": self.replacement,
            **({"catalog": self.catalog} if self.catalog is not None else {}),
        }

    def dispatch(self, **kwargs):
        self.dispatched_grants.append(kwargs["grant"])
        return super().dispatch(**kwargs)


@pytest.mark.parametrize(
    ("status_code", "error_code", "terminal"),
    [
        (401, "invalid_room_grant", True),
        (403, "invalid_room_grant", True),
        (403, "room_reauthorization_required", True),
        (400, "invalid_room_grant", False),
        (403, "room_execution_policy_changed", False),
        (500, "invalid_room_grant", False),
    ],
)
def test_grant_revoke_terminal_classification_uses_structured_fields(
    status_code,
    error_code,
    terminal,
):
    exc = PeerRunsHTTPError(
        "opaque peer error",
        status_code=status_code,
        error_code=error_code,
    )

    assert _grant_revoke_is_terminal(exc) is terminal


class _ApprovalPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.approvals = []

    def status(self, **kwargs):
        task_id = self.dispatches[-1]["task_id"] if self.dispatches else "task-1"
        return {
            "status": "waiting_for_approval",
            "active": True,
            "task_id": task_id,
                "execution_generation": 2,
                "run_id": "run-peer-1",
                "session_id": "peer-group-session",
                "request_id": "req-peer-1",
                "approval": {
                "description": "Run the focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
        }

    def approve_receipt(self, **kwargs):
        self.approvals.append(dict(kwargs))
        return {"resolved": 1}


class _RecoveringPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.recoveries = []

    def recover_dispatch(self, **kwargs):
        dispatch = dict(kwargs["dispatch"])
        self.recoveries.append({**kwargs, "dispatch": dispatch})
        self.dispatches.append(dispatch)
        return {
            "status": "accepted",
            "task_id": dispatch["task_id"],
            "execution_generation": dispatch["execution_generation"],
            "run_id": "run-recovered",
        }


class _PromptRecordingRPC(_FakeRPC):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str]] = []

    def submit(
        self,
        *,
        profile,
        session_id,
        prompt,
        source,
        task,
        execution_generation,
        on_terminal,
    ):
        self.prompts.append((profile, prompt))
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}


class _BlockingFirstRPC(_PromptRecordingRPC):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def submit(self, **kwargs):
        self.prompts.append((kwargs["profile"], kwargs["prompt"]))
        if len(self.prompts) == 1:
            self.first_started.set()
            assert self.release_first.wait(timeout=2)
        kwargs["on_terminal"](
            {"status": "settled", "text": f"reply from {kwargs['profile']}"}
        )
        return {"accepted": True}


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_stop_room_snapshots_tasks_before_status_transitions(monkeypatch, tmp_path):
    """One running task must not be counted again after it becomes stopping."""

    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    task = {"identity": identity, "status": "running", "cancel_id": None}
    calls = []

    def listed(_db, *, room_id, status):
        assert room_id == "room-1"
        return [dict(task)] if task["status"] == status else []

    def cancel(_identity, *, cancel_id):
        calls.append(cancel_id)
        task["status"] = "stopping"
        task["cancel_id"] = cancel_id
        return dict(task)

    monkeypatch.setattr(driver, "list_tasks", listed)
    monkeypatch.setattr(
        hosted_rooms,
        "request_room_stop",
        lambda _db, *, room_id, cancel_id, **_authority: {
            "room_id": room_id,
            "cancel_id": cancel_id,
        },
    )
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    hosted_rooms.create_room(
        service.db_path,
        room_id="room-1",
        name="Stop room",
        members=[],
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
    )
    service.runtime = SimpleNamespace(cancel=cancel, wakeup=lambda: None)

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    assert calls == ["stop-1"]


def test_create_send_drive_publish_and_replay_without_client_transport(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    assert room["room_id"] == "room-1"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect the release", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    assert [event["kind"] for event in events][:3] == [
        "message.user",
        "message.member",
        "turn.settled",
    ]
    assert events[1]["payload"]["text"] == "reply from ops"
    assert service.status("room-1")["working"] is False


def test_restart_republishes_terminal_task_before_admitting_more(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    event = _append_room_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    binding = service.bindings()[0]
    service.prepare_room(binding)
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="crashed",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    driver.settle_task(
        db,
        attempt,
        settlement_id="reply-1",
        status="settled",
        result={"text": "done"},
        clock=time.time,
    )

    service.prepare_room(binding)
    events = service._events("room-1")
    assert event["seq"] == 1
    assert sum(row["kind"] == "message.member" for row in events) == 1
    assert sum(row["kind"] == "turn.settled" for row in events) == 1
    service.prepare_room(binding)
    replayed = service._events("room-1")
    assert replayed == events


def test_real_service_runtime_rpc_prompt_agent_terminal_publication_e2e(
    tmp_path: Path, monkeypatch, request
):
    """Real Room service chain reaches prompt.submit and publishes its reply."""

    import tui_gateway.server as real_server

    home = tmp_path / ".hermes"
    (home / "profiles" / "ops").mkdir(parents=True)
    (home / "profiles" / "reviewer").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    seen_prompts: list[str] = []

    class _DeterministicAgent:
        session_id = ""
        model = "deterministic-model"
        provider = "deterministic"
        base_url = ""
        api_key = ""
        api_mode = ""
        _config_context_length = None
        interim_assistant_callback = None

        def clear_interrupt(self):
            return None

        def run_conversation(
            self,
            prompt,
            *,
            conversation_history=None,
            stream_callback=None,
            persist_user_message=None,
            task_id=None,
        ):
            del conversation_history, persist_user_message, task_id
            seen_prompts.append(str(prompt))
            if stream_callback is not None:
                stream_callback("worker done")
            return {
                "final_response": "worker done",
                "messages": [
                    {"role": "user", "content": str(prompt)},
                    {"role": "assistant", "content": "worker done"},
                ],
                "completed": True,
            }

    def _schedule_build(sid):
        session = real_server._sessions[sid]
        agent = _DeterministicAgent()
        agent.session_id = session["session_key"]
        session["agent"] = agent
        session["agent_ready"].set()

    monkeypatch.setattr(real_server, "_schedule_agent_build", _schedule_build)
    monkeypatch.setattr(real_server, "_start_agent_build", lambda _sid, _session: None)
    monkeypatch.setattr(real_server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(real_server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(real_server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(real_server, "_sync_agent_compression_with_config", lambda *_args: None)
    monkeypatch.setattr(real_server, "_sync_bot_capabilities", lambda *_args: None)
    monkeypatch.setattr(real_server, "make_stream_renderer", lambda _cols: None)
    monkeypatch.setattr(real_server, "render_message", lambda _raw, _cols: None)
    monkeypatch.setattr(real_server, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(real_server, "_emit", lambda *_args, **_kwargs: None)

    with real_server._sessions_lock:
        prior_sessions = dict(real_server._sessions)
        real_server._sessions.clear()

    def _restore_sessions():
        with real_server._sessions_lock:
            real_server._sessions.clear()
            real_server._sessions.update(prior_sessions)

    request.addfinalizer(_restore_sessions)
    service = None
    try:
        db = hosted_rooms.default_db_path()
        service = HostedRoomService(real_server, db_path=db)
        service.local_profiles = lambda: ("ops", "reviewer")
        service.create_room(
            room_id="room-e2e",
            name="Production chain",
            members=[
                {"member_id": "ops", "profile": "ops", "handle": "ops"},
                {
                    "member_id": "reviewer",
                    "profile": "reviewer",
                    "handle": "reviewer",
                },
            ],
        )
        from gateway import room_mirror_db
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource
        from tui_gateway import methods_groups
        import asyncio

        room_mirror_db.create_sub(
            db,
            room_id="room-e2e",
            platform="telegram",
            chat_id="chat-e2e",
            inbound=True,
        )
        monkeypatch.setattr(
            methods_groups, "get_hosted_room_service", lambda: service
        )
        inbound = MessageEvent(
            text="@ops execute the production chain",
            message_id="external-1",
            source=SessionSource(
                platform=Platform.TELEGRAM,
                user_id="platform-user",
                chat_id="chat-e2e",
                chat_type="group",
            ),
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        service.start()
        assert asyncio.run(
            runner._maybe_route_inbound_to_room(inbound, inbound.source)
        ) is None
        # Platform redelivery must route idempotently, never mint another turn.
        assert asyncio.run(
            runner._maybe_route_inbound_to_room(inbound, inbound.source)
        ) is None
        _wait_for(
            lambda: any(
                event["kind"] == "message.member"
                for event in service._events("room-e2e")
            ),
            timeout=8.0,
        )
    finally:
        if service is not None:
            assert service.stop(timeout=3.0)

    with real_server._sessions_lock:
        run_threads = [
            session.get("_run_thread") for session in real_server._sessions.values()
        ]
    for run_thread in run_threads:
        if run_thread is not None:
            run_thread.join(timeout=5.0)
            assert not run_thread.is_alive()

    events = service._events("room-e2e")
    member = next(event for event in events if event["kind"] == "message.member")
    assert member["actor"] == {"kind": "member", "id": "ops", "profile": "ops"}
    assert member["payload"]["text"] == "worker done"
    assert any(event["kind"] == "turn.settled" for event in events)
    assert sum(event["kind"] == "message.user" for event in events) == 1
    user_event = next(event for event in events if event["kind"] == "message.user")
    assert user_event["actor"] == {"kind": "user", "id": "platform-user"}
    assert user_event["payload"]["thread_id"].startswith("inbound:")
    assert member["payload"]["thread_id"] == user_event["payload"]["thread_id"]
    assert len(seen_prompts) == 1
    assert "execute the production chain" in seen_prompts[0]
    resolved = service.rpc.resolve_exact(
        profile="ops", title="Group: room-e2e", source="bot_room"
    )
    assert resolved is not None
    with real_server._sessions_lock:
        assert len(real_server._sessions) == 1



def test_dashboard_relays_exact_approval_to_separate_gateway_process(tmp_path: Path):
    db = tmp_path / "state.db"
    gateway = HostedRoomService(_server(), db_path=db)
    gateway.rpc = _FakeRPC()
    gateway.runtime.rpc = gateway.rpc
    gateway.rpc.owns_session = lambda session_id: session_id == "owner-session"
    gateway.local_profiles = lambda: ("default", "ops")
    gateway.create_room(
        room_id="room-1",
        name="Cross-process approval",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    gateway.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=gateway.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="gateway-process",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    member_id = str(task["payload"]["target_member_id"])
    gateway._set_pending_action(
        "room-1",
        member_id,
        {
            "kind": "approval",
            "task_id": task["identity"].task_id,
            "execution_generation": attempt.execution_generation,
            "session_id": "owner-session",
            "request_id": "request-1",
            "approval": {
                "request_id": "request-1",
                "description": "dangerous command",
            },
        },
    )

    dashboard = HostedRoomService(_server(), db_path=db)
    dashboard.rpc = _FakeRPC()
    dashboard.runtime.rpc = dashboard.rpc
    dashboard.rpc.owns_session = lambda _session_id: False
    action = dashboard.pending_actions("room-1")["actions"][0]
    assert action["kind"] == "permission"
    assert dashboard.handle_action(
        "room-1", action["action_id"], "approve"
    ) == {"ok": True, "message": "Action decision queued"}
    assert dashboard.handle_action(
        "room-1", action["action_id"], "approve"
    ) == {"ok": True, "message": "Action decision already recorded"}
    conflict = dashboard.handle_action(
        "room-1", action["action_id"], "deny"
    )
    assert conflict == {
        "ok": False,
        "message": "Action already has a different decision",
    }
    assert dashboard.rpc.approvals == []

    assert gateway._relay_action_decisions_once() == 1
    assert gateway.rpc.approvals == [
        {
            "session_id": "owner-session",
            "request_id": "request-1",
            "choice": "once",
        }
    ]
    assert dashboard.pending_actions("room-1")["actions"] == []

    # A queued choice is fenced by its task generation even if a replacement
    # action accidentally reuses the same external request id.
    from gateway import hosted_room_actions

    hosted_room_actions.request_action_decision(
        db,
        room_id="room-1",
        member_id=member_id,
        action_id="request-2",
        decision={
            "choice": "deny",
            "task_id": "old-task",
            "execution_generation": 1,
            "request_id": "request-2",
        },
    )
    gateway._set_pending_action(
        "room-1",
        member_id,
        {
            "kind": "approval",
            "task_id": "replacement-task",
            "execution_generation": 2,
            "session_id": "owner-session",
            "request_id": "request-2",
        },
    )
    assert gateway._relay_action_decisions_once() == 1
    assert len(gateway.rpc.approvals) == 1
    assert hosted_room_actions.load_undelivered_decisions(db) == []


def test_c5_manual_dag_task_auto_dispatches_and_completes_closing_the_loop(
    tmp_path: Path,
):
    """C5: a manual task DAG drives real member turns without a decider.

    Two manual tasks (t2 blocked by t1), each subject targeting one worker via
    ``@handle``. With the room otherwise idle, ``prepare_room`` must:
      1. auto-dispatch t1 (lowest seq, unblocked) → its worker turn settles →
         the sweep completes t1, which unblocks t2;
      2. auto-dispatch t2 → its worker turn settles → t2 completes.
    Proven end to end through the real runtime loop (no manual ticks): each
    task ends ``completed`` with the right owner, and the log carries the
    ``message.user`` anchor + the worker's ``message.member`` reply on each
    per-task ``dagtask:`` thread.
    """

    from gateway import room_task_dag as dag

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "backend", "frontend")
    service.create_room(
        room_id="room-1",
        name="DAG room",
        members=[
            {"member_id": "backend", "profile": "backend", "handle": "backend"},
            {
                "member_id": "frontend",
                "profile": "frontend",
                "handle": "frontend",
            },
        ],
    )
    dag.create_task(
        db, room_id="room-1", subject="@backend build the API", task_id="t1"
    )
    dag.create_task(
        db,
        room_id="room-1",
        subject="@frontend build the UI",
        task_id="t2",
        blocked_by=["t1"],
    )

    service.start()
    try:
        _wait_for(
            lambda: dag.get_task(db, room_id="room-1", task_id="t2")["status"]
            == "completed",
            timeout=8.0,
        )
    finally:
        assert service.stop(timeout=2.0)

    t1 = dag.get_task(db, room_id="room-1", task_id="t1")
    t2 = dag.get_task(db, room_id="room-1", task_id="t2")
    assert t1["status"] == "completed" and t1["owner"] == "backend"
    assert t2["status"] == "completed" and t2["owner"] == "frontend"

    log = [
        (event["kind"], (event.get("payload") or {}).get("thread_id"))
        for event in service._events("room-1")
    ]
    for expected in (
        ("message.user", "dagtask:t1"),
        ("message.member", "dagtask:t1"),
        ("message.user", "dagtask:t2"),
        ("message.member", "dagtask:t2"),
    ):
        assert expected in log, f"missing {expected} in {log}"
    anchors = [
        (event.get("payload") or {}).get("text")
        for event in service._events("room-1")
        if event["kind"] == "message.user"
    ]
    assert anchors == ["@backend build the API", "@frontend build the UI"]


def test_c5_dag_completion_waits_for_settled_claimed_worker_reply(
    tmp_path: Path,
):
    """A decider dispatch message must not complete its worker-owned DAG task.

    Publication appends message.member before turn.settled, so even the worker's
    visible reply is insufficient until its terminal event commits it.
    """

    from gateway import room_task_dag as dag

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("lead", "backend")
    service.create_room(
        room_id="room-1",
        name="DAG room with decider",
        members=[
            {
                "member_id": "lead-id",
                "profile": "lead",
                "handle": "lead",
                "role": "decider",
            },
            {
                "member_id": "backend-id",
                "profile": "backend",
                "handle": "backend",
            },
        ],
    )
    dag.create_task(
        db, room_id="room-1", subject="@backend inspect it", task_id="t1"
    )
    assert dag.claim_task_for_dispatch(
        db,
        room_id="room-1",
        task_id="t1",
        owner="backend",
        dispatch_thread_id="dagtask:t1",
    ) is not None
    room = hosted_rooms.room_state(db, room_id="room-1")

    def append(*, event_id: str, kind: str, actor: dict, payload: dict) -> None:
        hosted_rooms.append_event(
            db,
            room_id="room-1",
            event_id=event_id,
            kind=kind,
            actor=actor,
            payload=payload,
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )

    append(
        event_id="decider-message",
        kind="message.member",
        actor={"kind": "member", "id": "lead-id", "profile": "lead"},
        payload={
            "member_id": "lead-id",
            "thread_id": "dagtask:t1",
            "text": "@backend inspect it",
        },
    )
    service._sweep_completed_dag_dispatches(room)
    assert dag.get_task(db, room_id="room-1", task_id="t1")["status"] == "in_progress"

    append(
        event_id="worker-message",
        kind="message.member",
        actor={"kind": "member", "id": "backend-id", "profile": "backend"},
        payload={
            "member_id": "backend-id",
            "thread_id": "dagtask:t1",
            "text": "@lead done",
        },
    )
    service._sweep_completed_dag_dispatches(room)
    assert dag.get_task(db, room_id="room-1", task_id="t1")["status"] == "in_progress"

    append(
        event_id="worker-terminal",
        kind="turn.settled",
        actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
        payload={
            "member_id": "backend-id",
            "thread_id": "dagtask:t1",
            "message_event_id": "worker-message",
        },
    )
    service._sweep_completed_dag_dispatches(room)
    assert dag.get_task(db, room_id="room-1", task_id="t1")["status"] == "completed"


def test_c5_ambiguous_subject_is_not_auto_dispatched(tmp_path: Path):
    """A DAG task whose subject names no single worker stays pending.

    Auto-dispatch requires exactly one ``@handle`` target; a bare subject (or a
    multi-mention one) must never be claimed, so the room stays quiet.
    """

    from gateway import room_task_dag as dag

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "backend", "frontend")
    service.create_room(
        room_id="room-1",
        name="DAG room",
        members=[
            {"member_id": "backend", "profile": "backend", "handle": "backend"},
            {
                "member_id": "frontend",
                "profile": "frontend",
                "handle": "frontend",
            },
        ],
    )
    # No @handle → no unambiguous owner.
    dag.create_task(db, room_id="room-1", subject="do something", task_id="t1")
    # Two @handles → ambiguous, also skipped.
    dag.create_task(
        db, room_id="room-1", subject="@backend @frontend both", task_id="t2"
    )

    binding = service.bindings()[0]
    service.prepare_room(binding)
    service.prepare_room(binding)

    assert dag.get_task(db, room_id="room-1", task_id="t1")["status"] == "pending"
    assert dag.get_task(db, room_id="room-1", task_id="t2")["status"] == "pending"
    kinds = [event["kind"] for event in service._events("room-1")]
    assert "message.user" not in kinds



def test_c5_unroutable_head_does_not_block_later_valid_task(tmp_path: Path):
    from gateway import room_task_dag as dag

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "backend", "frontend")
    service.create_room(
        room_id="room-1",
        name="DAG room",
        members=[
            {"member_id": "backend", "profile": "backend", "handle": "backend"},
            {
                "member_id": "frontend",
                "profile": "frontend",
                "handle": "frontend",
            },
        ],
    )
    dag.create_task(db, room_id="room-1", subject="missing target", task_id="t1")
    dag.create_task(
        db, room_id="room-1", subject="@backend valid work", task_id="t2"
    )

    service.start()
    try:
        _wait_for(
            lambda: dag.get_task(db, room_id="room-1", task_id="t2")["status"]
            == "completed",
            timeout=8.0,
        )
    finally:
        assert service.stop(timeout=2.0)

    assert dag.get_task(db, room_id="room-1", task_id="t1")["status"] == "pending"
    t2 = dag.get_task(db, room_id="room-1", task_id="t2")
    assert t2["status"] == "completed"
    assert t2["owner"] == "backend"



def test_policy_checkpoint_bounds_replay_after_completed_room_history(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Long-running room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "default"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    authority = str(room["authority_gateway_id"])
    rows = []
    for index in range(200):
        user_seq = index * 2 + 1
        activity_seq = user_seq + 1
        thread_id = f"thread-{index}"
        event_id = f"user-{index}"
        rows.extend((
            (
                "room-1",
                user_seq,
                event_id,
                "message.user",
                json.dumps({"kind": "user", "id": "load-test"}),
                None,
                json.dumps({"text": "done", "thread_id": thread_id}),
                float(user_seq),
            ),
            (
                "room-1",
                activity_seq,
                f"activity-{index}",
                "room.activity",
                json.dumps({"kind": "gateway", "id": authority}),
                1,
                json.dumps({
                    "status": "settled",
                    "reason_code": "silent_round",
                    "thread_id": thread_id,
                    "discussion_event_id": event_id,
                }),
                float(activity_seq),
            ),
        ))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """INSERT INTO hosted_room_events(
                   room_id, seq, event_id, kind, actor_json,
                   authority_epoch, payload_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.execute(
            """UPDATE hosted_rooms
               SET next_seq=401, revision=revision+400, updated_at=400
               WHERE room_id='room-1'"""
        )
    _append_room_event(
        db,
        room_id="room-1",
        event_id="user-active",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "Review this", "thread_id": "thread-active"},
        now=401,
    )

    original_read_events = hosted_rooms.read_events
    reads = {"calls": 0, "rows": 0}

    def counted_read_events(*args, **kwargs):
        page = original_read_events(*args, **kwargs)
        reads["calls"] += 1
        reads["rows"] += len(page["events"])
        return page

    monkeypatch.setattr(hosted_rooms, "read_events", counted_read_events)
    binding = service.bindings()[0]
    service.prepare_room(binding)
    assert reads["rows"] == 401
    snapshot = service._policy_snapshot(hosted_rooms.room_state(db, room_id="room-1"))
    assert len(snapshot.events) == 1
    assert len(snapshot.events) <= MAX_ACTIVE_POLICY_EVENTS
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM hosted_room_policy_events").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM hosted_room_policy_threads").fetchone()[
                0
            ]
            == 1
        )

    reads.update(calls=0, rows=0)
    service.prepare_room(binding)
    assert reads == {"calls": 0, "rows": 0}


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


def test_research_team_pending_permission_survives_restart_and_relays_exact_action(tmp_path: Path):
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
    assert service.stop(timeout=1.0)

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
    assert service.stop(timeout=1.0)
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
    assert service.stop(timeout=1.0)
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


def test_service_uses_low_idle_poll_with_immediate_wakeup(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")

    assert service.runtime.poll_interval_seconds == 5.0
    assert service.runtime.active_poll_interval_seconds == 0.25
    assert service.runtime.turn_timeout_seconds == 1830.0
    service.runtime._wake.clear()
    service.wakeup()
    assert service.runtime._wake.is_set()


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


def test_local_pending_approval_requires_exact_task_generation_and_request(
    tmp_path: Path,
):
    class ApprovalRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.approvals = []

        def approve(self, *, session_id, request_id, choice):
            self.approvals.append((session_id, request_id, choice))
            return {"resolved": 1}

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = ApprovalRPC()
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
    task = driver.get_task(db, task["identity"])
    service.runtime._report_pending_action(
        task,
        session_id="ops-session",
        info={
            "pending_approval": {
                "request_id": "approval-1",
                "choices": ["once", "always", "deny"],
            }
        },
    )

    action = service.status("room-1")["pending_actions"][0]
    assert action["member_id"] == "ops"
    assert action["approval"]["choices"] == ["once", "deny"]
    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="ops",
            task_id=task["identity"].task_id,
            execution_generation=1,
            choice="once",
            request_id="wrong-request",
        )

    assert service.approve_room_task(
        "room-1",
        member_id="ops",
        task_id=task["identity"].task_id,
        execution_generation=1,
        choice="once",
        request_id="approval-1",
    ) == {"resolved": 1}
    assert rpc.approvals == [("ops-session", "approval-1", "once")]
    assert service.status("room-1")["pending_actions"] == []


def test_headless_room_publishes_peer_member_reply_without_desktop_transport(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    peer = _FakePeerClient()
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-reviewer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        execution_policy_digest="b" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        db_path=db,
        peer_routes={("room-1", "member-reviewer"): route},
        peer_clients={"install-peer": peer},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default",)
    room = service.create_room(
        room_id="room-1",
        name="Review room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "local",
            },
            {
                "member_id": "member-reviewer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    assert room["members"][1]["target"]["kind"] == "peer"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-peer-1",
        payload={"text": "@reviewer inspect this", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    reply = next(event for event in events if event["kind"] == "message.member")
    assert reply["payload"]["member_id"] == "member-reviewer"
    assert reply["payload"]["text"] == "Remote review complete."
    assert reply["actor"]["connection_id"] == "peer-review"
    assert peer.dispatches[0]["target_profile"] == "reviewer"


def test_unadmitted_peer_failure_does_not_block_next_healthy_member(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        db_path=db,
        peer_routes={("room-1", "member-peer"): route},
        peer_clients={"install-peer": _UnavailablePeerClient()},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("local",)
    service.create_room(
        room_id="room-1",
        name="Fallback room",
        members=[
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
            {"member_id": "local", "profile": "local", "handle": "local"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-fallback-1",
        payload={"text": "Review this together", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            and event["payload"]["member_id"] == "local"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    assert any(
        event["kind"] == "turn.failed"
        and event["payload"]["member_id"] == "member-peer"
        for event in events
    )
    assert any(
        event["kind"] == "message.member" and event["payload"]["member_id"] == "local"
        for event in events
    )


def test_registered_peer_route_rehydrates_after_service_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    first = HostedRoomService(_server(), db_path=db)
    first.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    restarted = HostedRoomService(_server(), db_path=db)
    restored = restarted.peer_routes[("room-1", "member-peer")]
    assert restored.target_install_id == "install-peer"
    assert restored.target_profile == "reviewer"
    assert restored.grant == "signed.room.grant"
    assert ("room-1", "member-peer") in restarted.peer_clients


def test_one_corrupt_stored_route_does_not_hide_healthy_peers(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    service = HostedRoomService(_server(), db_path=db)
    for room_id, member_id in (("room-good", "member-good"), ("room-bad", "member-bad")):
        route = PeerMemberRoute(
            home_install_id=hosted_rooms.local_authority_gateway_id(),
            member_id=member_id,
            target_install_id="install-peer",
            target_profile="reviewer",
            capability_digest=catalog.catalog_digest,
            cancellation_scope_id=f"cancel-{room_id}",
            trace_id=f"trace-{room_id}",
            grant=f"grant-{room_id}",
        )
        service.register_peer_route(
            room_id=room_id,
            member_id=member_id,
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE hosted_room_links SET target_url=? WHERE room_id=?",
            ("http://public-plaintext.example.test", "room-bad"),
        )

    restarted = HostedRoomService(_server(), db_path=db)

    assert ("room-good", "member-good") in restarted.peer_routes
    assert ("room-bad", "member-bad") not in restarted.peer_routes
    assert restarted.status()["link_load_error"] == "room-bad:member-bad:invalid"


def test_unpublished_roomlink_v1_route_is_quarantined_for_reinvitation(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    legacy_catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            protocol_versions=(1,),
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=legacy_catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="legacy-v1-grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=legacy_catalog,
    )

    restarted = HostedRoomService(_server(), db_path=db)

    assert restarted.peer_routes == {}
    assert restarted.status()["link_load_error"] == (
        "room-1:member-peer:protocol-upgrade-required"
    )


def test_peer_member_without_route_fails_closed_instead_of_running_locally(
    tmp_path: Path,
):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    with pytest.raises(RuntimeError, match="route is unavailable"):
        service._resolve_member_transport(
            service.bindings()[0],
            {
                "payload": {
                    "target_member_id": "member-peer",
                    "target_profile": "reviewer",
                    "source_event_seq": 9,
                }
            },
        )


def test_registration_disk_failure_does_not_publish_live_route(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    monkeypatch.setattr(
        "gateway.hosted_room_links.save_room_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        service.register_peer_route(
            room_id="room-1",
            member_id="member-peer",
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )
    assert ("room-1", "member-peer") not in service.peer_routes
    assert "install-peer" not in service.peer_clients


def test_room_route_revocation_is_remote_first_and_removes_local_state(
    tmp_path: Path,
):
    from gateway import hosted_room_links

    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _FakePeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    assert service.revoke_room_routes("room-1") == 1
    assert peer.revoked == ["signed.room.grant"]
    assert ("room-1", "member-peer") not in service.peer_routes
    assert hosted_room_links.load_room_links(db) == ()


def test_failed_remote_revocation_preserves_route_for_retry(tmp_path: Path):
    from gateway import hosted_room_links

    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_UnavailableRevokePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    with pytest.raises(RuntimeError, match="offline during revocation"):
        service.revoke_room_routes("room-1")
    assert ("room-1", "member-peer") in service.peer_routes
    assert len(hosted_room_links.load_room_links(db)) == 1


def test_expired_remote_grant_no_longer_blocks_room_cleanup(tmp_path: Path):
    from gateway import hosted_room_links

    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        execution_policy_digest=catalog.execution_policy.policy_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="expired.room.grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_ExpiredRevokePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    assert service.revoke_room_routes("room-1") == 1
    assert ("room-1", "member-peer") not in service.peer_routes
    assert hosted_room_links.load_room_links(db) == ()


def test_expired_grant_surfaces_needs_reauthorization_without_secret(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_ExpiredGrantPeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 3,
            }
        },
    )
    with pytest.raises(PeerRunsHTTPError):
        transport.resolve_exact(
            profile="reviewer",
            title="Group: room-1",
            source="bot_room",
        )
    status = service.status("room-1")
    assert status["peer_routes"] == [
        {
            "room_id": "room-1",
            "member_id": "member-peer",
            "status": "needs_reauthorization",
        }
    ]
    assert "signed.room.grant" not in repr(status)
    restarted = HostedRoomService(_server(), db_path=db)
    assert restarted.status("room-1")["peer_routes"] == status["peer_routes"]

    rotated = PeerMemberRoute(
        home_install_id=route.home_install_id,
        member_id=route.member_id,
        target_install_id=route.target_install_id,
        target_profile=route.target_profile,
        capability_digest=route.capability_digest,
        cancellation_scope_id=route.cancellation_scope_id,
        trace_id="trace-room-rotated",
        grant="rotated.room.grant",
    )
    restarted.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=rotated,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "ready"
    after_rotation = HostedRoomService(_server(), db_path=db)
    assert after_rotation.peer_routes[("room-1", "member-peer")].grant == (
        "rotated.room.grant"
    )


def test_not_admitted_dispatch_persists_unavailable_route_until_success(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _NotAdmittedPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    binding = service.bindings()[0]
    task = {
        "identity": driver.TaskIdentity(
            "room-1", "task-peer", "thread-1", "turn-1"
        ),
        "execution_generation": 1,
        "payload": {
            "target_member_id": "member-peer",
            "target_profile": "reviewer",
            "source_event_seq": 3,
        },
    }
    transport = service._resolve_member_transport(binding, task)
    session = transport.create(
        profile="reviewer",
        title="Group: room-1",
        source="bot_room",
    )

    with pytest.raises(PeerRunsHTTPError) as caught:
        transport.submit(
            profile="reviewer",
            session_id=session["session_id"],
            prompt="Review the queued task.",
            source="bot_room",
            task=task["identity"],
            execution_generation=1,
            on_terminal=lambda _receipt: None,
        )

    assert caught.value.not_admitted is True
    assert service.status("room-1")["peer_routes"][0]["status"] == "unavailable"
    restarted = HostedRoomService(_server(), db_path=db)
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "unavailable"

    peer.offline = False
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review the queued task.",
        source="bot_room",
        task=task["identity"],
        execution_generation=2,
        on_terminal=lambda _receipt: None,
    )
    assert service.status("room-1")["peer_routes"][0]["status"] == "ready"


def test_dispatch_refresh_persists_before_remote_admission(tmp_path: Path):
    now = time.time()
    secret = b"s" * 32
    old_grant = issue_room_grant(
        secret,
        grant_id="grant-old",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        issued_at=now - 3700,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    new_grant = issue_room_grant(
        secret,
        grant_id="grant-new",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        issued_at=now,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant=old_grant,
    )
    peer = _RefreshingPeerClient(new_grant)
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 11,
            },
        },
    )
    session = transport.create(
        profile="reviewer", title="Group: room-1", source="bot_room"
    )
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review this",
        source="bot_room",
        task=identity,
        execution_generation=1,
        on_terminal=lambda _receipt: None,
    )
    assert peer.refreshed == [old_grant]
    assert peer.refresh_arguments == [
        {
            "grant": old_grant,
            "capability_digest": catalog.catalog_digest,
            "execution_policy_digest": catalog.execution_policy.policy_digest,
        }
    ]
    assert peer.dispatched_grants == [new_grant]
    assert peer.dispatches[0]["capability_digest"] == catalog.catalog_digest
    assert (
        peer.dispatches[0]["execution_policy_digest"]
        == catalog.execution_policy.policy_digest
    )
    assert HostedRoomService(_server(), db_path=db).peer_routes[
        ("room-1", "member-peer")
    ].grant == new_grant


@pytest.mark.parametrize(
    ("capability_changed", "policy_changed"),
    [(False, True), (True, False), (True, True)],
)
def test_dispatch_refresh_marks_route_for_reauthorization_on_drift(
    capability_changed,
    policy_changed,
):
    from gateway.hosted_room_execution_policy import execution_policy_mapping

    base_policy = execution_policy_mapping(
        target_profile="reviewer",
        config={"agent": {"max_turns": 20}},
    )
    changed_policy = execution_policy_mapping(
        target_profile="reviewer",
        config={"agent": {"max_turns": 21}},
    )
    base_catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            persistent_process=True,
            execution_policy=base_policy,
        )
    )
    refreshed_catalog = catalog_mapping(
        installation_id="install-peer",
        persistent_process=True,
        attachments=capability_changed,
        execution_policy=changed_policy if policy_changed else base_policy,
    )
    now = time.time()
    grant = issue_room_grant(
        b"s" * 32,
        grant_id="grant-old",
        room_id="room-1",
        home_install_id="install-home",
        authority_gateway_id="install-home",
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        execution_policy_digest=base_catalog.execution_policy.policy_digest,
        issued_at=now - 3500,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    prompt = "Review this"
    dispatch = HostedMemberDispatch(
        protocol_version=PROTOCOL_VERSION,
        room_id="room-1",
        home_install_id="install-home",
        authority_gateway_id="install-home",
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        task_id="task-1",
        execution_generation=1,
        source_event_seq=1,
        cancellation_scope_id="cancel-room-1",
        prompt=prompt,
        prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
        capability_digest=base_catalog.catalog_digest,
        execution_policy_digest=base_catalog.execution_policy.policy_digest,
        trace_id="trace-room-1",
    )
    peer = _RefreshingPeerClient(
        "replacement.room.grant",
        catalog=refreshed_catalog,
    )
    reauthorization = []
    refreshed = []
    tracked = _RouteStatusPeerClient(
        peer,
        on_ready=lambda: None,
        on_reauthorization=lambda: reauthorization.append(True),
        on_unavailable=lambda: None,
        on_refreshed=lambda *args: refreshed.append(args),
    )

    with pytest.raises(PeerRunsHTTPError) as caught:
        tracked.dispatch(dispatch=dispatch.as_mapping(), grant=grant)

    assert caught.value.needs_reauthorization is True
    assert reauthorization == [True]
    assert refreshed == []
    assert peer.dispatches == []


def test_peer_approval_is_scoped_visible_and_resolvable(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _ApprovalPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "hermes",
            },
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            }
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 2,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 1,
            },
        },
    )

    status = transport.info(
        profile="reviewer",
        session_id="peer-group-session",
        source="bot_room",
    )
    assert status["status"] == "waiting_for_approval"
    service._set_pending_action(
        "room-1",
        "member-peer",
        {
            "kind": "approval",
            "task_id": status["task_id"],
            "execution_generation": status["execution_generation"],
            "run_id": status["run_id"],
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": status["approval"],
        },
    )
    pending = service.status("room-1")["pending_actions"]
    assert len(pending) == 1
    assert pending[0]["created_at"] > 0
    assert {key: value for key, value in pending[0].items() if key != "created_at"} == {
            "kind": "approval",
            "task_id": "task-1",
            "execution_generation": 2,
            "run_id": "run-peer-1",
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": {
                "description": "Run the focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
            "member_id": "member-peer",
        }

    assert service.approve_room_task(
        "room-1",
        member_id="member-peer",
        task_id="task-1",
        execution_generation=2,
        choice="once",
        request_id="req-peer-1",
    ) == {"resolved": 1}
    assert peer.approvals == [
        {
            "task_id": "task-1",
            "execution_generation": 2,
            "request_id": "req-peer-1",
            "choice": "once",
            "grant": "signed.room.grant",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []


def test_local_room_approval_uses_the_exact_hidden_session(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service._set_pending_action(
        "room-1",
        "local",
        {
            "kind": "approval",
            "task_id": "task-local-1",
            "execution_generation": 1,
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "approval": {
                "description": "Run focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
        },
    )

    assert service.approve_room_task(
        "room-1",
        member_id="local",
        task_id="task-local-1",
        execution_generation=1,
        choice="once",
        request_id="approval-local-1",
    ) == {"resolved": 1}
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []


def test_pending_local_approval_survives_restart_and_generic_action_relays(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    first = HostedRoomService(_server(), db_path=db)
    first._set_pending_action(
        "room-1",
        "local",
        {
            "kind": "approval",
            "task_id": "task-local-1",
            "execution_generation": 3,
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "approval": {"choices": ["once", "deny"]},
        },
    )

    restarted = HostedRoomService(_server(), db_path=db)
    rpc = _FakeRPC()
    restarted.rpc = rpc
    restarted.runtime.rpc = rpc
    dashboard_action = restarted.pending_actions("room-1")["actions"][0]
    assert dashboard_action["action_id"] == "approval-local-1"
    assert dashboard_action["kind"] == "permission"
    assert dashboard_action["description"] == ""
    assert dashboard_action["detail"]["tool_name"] == "terminal"
    assert dashboard_action["detail"]["scope"] == "once"
    assert dashboard_action["created_at"] > 0

    result = restarted.handle_action(
        "room-1", "approval-local-1", "approve"
    )

    assert result["ok"] is True
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert restarted.pending_actions("room-1")["actions"] == []
    after_resolution = HostedRoomService(_server(), db_path=db)
    assert after_resolution.pending_actions("room-1")["actions"] == []



def test_stale_local_approval_cannot_resolve_replacement_request(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    action = {
        "kind": "approval",
        "task_id": "task-local-1",
        "execution_generation": 1,
        "session_id": "local-session",
        "approval": {"choices": ["once", "deny"]},
    }
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-A"}
    )
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-B"}
    )

    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="local",
            task_id="task-local-1",
            execution_generation=1,
            choice="once",
            request_id="approval-A",
        )

    assert rpc.approvals == []
    assert service.status("room-1")["pending_actions"][0]["request_id"] == (
        "approval-B"
    )


def test_peer_recovery_replays_the_same_execution_generation(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _RecoveringPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")

    service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "status": "indeterminate",
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 9,
                "prompt": "Recover the accepted review.",
            },
        },
    )

    assert len(peer.recoveries) == 1
    recovered = peer.recoveries[0]["dispatch"]
    assert recovered["task_id"] == "task-1"
    assert recovered["execution_generation"] == 1
    assert recovered["prompt"] == "Recover the accepted review."


def test_f1_decider_member_is_restricted_to_orchestration_only_tools(
    tmp_path: Path, monkeypatch
):
    """A decider member's live agent is built with orchestration-only tools.

    This is the hard, build-time enforcement of the decider's "schedule-only"
    contract — the Hermes analogue of Claude Code's applyCoordinatorToolFilter.
    A decider bound to a full engineer profile (file/terminal/code_execution/
    web/browser) has those stripped down to the orchestration allow-set
    (bot_room/todo/clarify), always retaining bot_room so it can still @mention
    and speak. Generic delegation is stripped because Room workers must be
    dispatched through the durable @mention protocol, not ephemeral subagents.
    A worker in the same room keeps its full toolset.
    """

    from tui_gateway import server as tui_server

    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("lead", "backend")
    service.create_room(
        room_id="room-1",
        name="Decider room",
        members=[
            {
                "member_id": "lead",
                "profile": "lead",
                "handle": "lead",
                "role": "decider",
            },
            {"member_id": "backend", "profile": "backend", "handle": "backend"},
        ],
    )

    # The build seam reads the authority DB via default_db_path(); point it at
    # this room's DB so the filter resolves the real persisted roster + roles.
    monkeypatch.setattr(
        "gateway.hosted_rooms.default_db_path", lambda: db
    )

    full_engineer = [
        "bot_room",
        "browser",
        "clarify",
        "code_execution",
        "delegation",
        "file",
        "terminal",
        "todo",
        "web",
    ]

    def _room_session(profile: str) -> dict:
        return {
            "source": "bot_room",
            "title": "Group: room-1",
            "profile_home": str(tmp_path / "profiles" / profile),
        }

    # Decider: intersected with the orchestration-only allow-set — the write /
    # execution / browsing toolsets are gone, the room voice survives.
    decider_tools = tui_server._room_decider_toolset_filter(
        _room_session("lead"), list(full_engineer)
    )
    assert decider_tools == ["bot_room", "clarify", "todo"]
    assert "file" not in decider_tools
    assert "terminal" not in decider_tools
    assert "code_execution" not in decider_tools
    assert "browser" not in decider_tools
    assert "bot_room" in decider_tools
    assert "delegation" not in decider_tools

    # Worker: untouched — the same full engineer toolset passes through.
    worker_tools = tui_server._room_decider_toolset_filter(
        _room_session("backend"), list(full_engineer)
    )
    assert worker_tools == full_engineer

    # A decider bound to "every toolset enabled" (base None) still collapses to
    # exactly the orchestration allow-set — it can never gain write tools.
    decider_from_all = tui_server._room_decider_toolset_filter(
        _room_session("lead"), None
    )
    assert decider_from_all == ["bot_room", "clarify", "todo"]

    # Normal sessions are untouched. A malformed bot_room session fails closed
    # because its worker/decider role cannot be positively established.
    assert (
        tui_server._room_decider_toolset_filter(
            {"source": "desktop", "title": "Group: room-1"}, list(full_engineer)
        )
        == full_engineer
    )
    assert tui_server._room_decider_toolset_filter(
        {"source": "bot_room", "title": "Untitled"}, list(full_engineer)
    ) == ["bot_room", "clarify", "todo"]

    # Regression (real-E2E): ``session.create`` stashes the room name under
    # ``pending_title`` — not ``title`` — because no DB row exists yet at the
    # ``_make_agent`` build seam (the row is created lazily on the first
    # prompt). An earlier revision read only ``title``, so the decider's first
    # LIVE turn fell open and received the FULL engineer toolset (file /
    # terminal / code_execution). The filter must bind off ``pending_title``
    # too, or F1 silently no-ops on turn one. This is exactly the seam that a
    # real qwen-plus room run exposed.
    decider_pending = tui_server._room_decider_toolset_filter(
        {
            "source": "bot_room",
            "pending_title": "Group: room-1",
            "profile_home": str(tmp_path / "profiles" / "lead"),
        },
        list(full_engineer),
    )
    assert decider_pending == ["bot_room", "clarify", "todo"]
    worker_pending = tui_server._room_decider_toolset_filter(
        {
            "source": "bot_room",
            "pending_title": "Group: room-1",
            "profile_home": str(tmp_path / "profiles" / "backend"),
        },
        list(full_engineer),
    )
    assert worker_pending == full_engineer

    unknown_profile = tui_server._room_decider_toolset_filter(
        {
            "source": "bot_room",
            "pending_title": "Group: room-1",
            "profile_home": str(tmp_path / "profiles" / "unknown"),
        },
        list(full_engineer),
    )
    assert unknown_profile == ["bot_room", "clarify", "todo"]

    monkeypatch.setattr(
        "gateway.hosted_rooms.room_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("db unavailable")),
    )
    unavailable = tui_server._room_decider_toolset_filter(
        _room_session("backend"), list(full_engineer)
    )
    assert unavailable == ["bot_room", "clarify", "todo"]
