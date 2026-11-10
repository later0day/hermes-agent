"""Integration tests for the hosted Discussion coordinator."""

from __future__ import annotations

import json
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
from tui_gateway import hosted_room_dag
from tui_gateway.hosted_room_service import (
    HostedRoomService,
    _grant_revoke_is_terminal,
)
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
        member_id="", attachments=None,
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
        member_id="", attachments=None,
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
    task = {"identity": identity, "status": "running", "cancel_id": None, "payload": {"source_event_seq": 1}}
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
            "cancel_id": cancel_id, "seq": 2,
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




def test_topology_projects_decider_and_worker_roles_for_dashboard(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("lead", "worker")
    service.create_room(
        room_id="role-room",
        name="Role room",
        members=[
            {"member_id": "lead", "profile": "lead", "handle": "lead", "role": "decider"},
            {"member_id": "worker", "profile": "worker", "handle": "worker", "role": "worker"},
        ],
    )

    topology = service.topology("role-room")
    assert [(member["handle"], member["role"]) for member in topology["members"]] == [
        ("lead", "team_lead"),
        ("worker", "teammate"),
    ]
    assert topology["team_lead_id"] == "lead"



def test_send_with_recipient_rejects_non_member_before_append(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("lead", "worker")
    service.create_room(
        room_id="recipient-room",
        name="Recipient room",
        members=[
            {"member_id": "lead", "profile": "lead", "handle": "Lead", "role": "decider"},
            {"member_id": "worker", "profile": "worker", "handle": "worker", "role": "worker"},
        ],
    )

    with pytest.raises(hosted_rooms.HostedRoomError, match="recipient is not a member"):
        service.send(
            room_id="recipient-room",
            event_id="unknown-recipient",
            payload={"text": "@missing do work", "thread_id": "thread"},
            recipient="missing",
        )
    assert not any(event["event_id"] == "unknown-recipient" for event in service._events("recipient-room"))

    service.prepare_room = lambda _binding: None
    service.runtime = SimpleNamespace(wakeup=lambda: None)
    accepted = service.send(
        room_id="recipient-room",
        event_id="known-recipient",
        payload={"text": "@Lead plan work", "thread_id": "thread"},
        recipient="lead",
    )
    assert accepted["event_id"] == "known-recipient"

def test_send_to_room_with_deleted_member_profile_is_stored_once_without_raising(tmp_path: Path):
    """A member whose profile was deleted leaves a roster that no longer validates. send() must not answer
    "error" for a message it already committed (every retry would stack another one): upstream #143239 stores
    the Send once and lets the room driver report the stall in the room."""
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("lead", "worker")
    service.create_room(
        room_id="orphan-room", name="Orphan room",
        members=[{"member_id": "lead", "profile": "lead", "handle": "lead", "role": "decider"},
                 {"member_id": "worker", "profile": "worker", "handle": "worker"}])
    service.local_profiles = lambda: ("lead",)  # `worker` was deleted

    sent = service.send(room_id="orphan-room", event_id="after-delete",
                        payload={"text": "@worker hi", "thread_id": "thread"}, recipient="worker")
    assert sent["kind"] == "message.user"
    assert [event["event_id"] for event in service._events("orphan-room")].count("after-delete") == 1


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
    assert service.stop(timeout=5.0)

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
        # The gateway's mirror watcher loads the in-memory inbound index at startup (and /room mirror refreshes it);
        # an unbound chat is rejected from that index before any I/O, so mirror the watcher here.
        runner._reload_room_inbound_sources()
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
    hosted_room_dag.sweep_completed(service, room)
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
    hosted_room_dag.sweep_completed(service, room)
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
    hosted_room_dag.sweep_completed(service, room)
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





































































def test_local_profiles_skips_delete_tombstones_and_dot_dirs(tmp_path: Path):
    """`hermes profile delete` leaves ``profiles/.deleted/<name>``; neither the tombstone dir, a
    tombstoned profile, nor a marker-less cron shell is a roster member (#106847: ``.deleted``
    failed validate_roster every cycle; #99392: side-effect dirs listed as bots)."""
    from hermes_constants import mark_named_profile_deleted

    profiles = tmp_path / "profiles"
    (profiles / "ops").mkdir(parents=True)
    (profiles / "ops" / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profiles / "gone").mkdir()
    (profiles / "gone" / "config.yaml").write_text("{}\n", encoding="utf-8")
    mark_named_profile_deleted(profiles / "gone")
    assert (profiles / ".deleted").is_dir()
    (profiles / "shell" / "cron").mkdir(parents=True)

    service = HostedRoomService(_server(), db_path=tmp_path / "shared-state.db")

    assert service.local_profiles() == ("default", "ops")


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
