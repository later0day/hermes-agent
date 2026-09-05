"""Tests for the in-process hosted room session adapter."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from gateway.hosted_room_driver import TaskIdentity
from tui_gateway.hosted_room_server_rpc import (
    HostedRoomServerRPC,
    HostedRoomSessionError,
)


def _server():
    sessions = {}
    calls = []

    def method(name, result):
        def handler(rid, params):
            calls.append((name, params))
            value = result(params) if callable(result) else result
            return {"id": rid, **value}

        return handler

    methods = {
        "session.list": method(
            "session.list",
            {"result": {"sessions": [{"id": "stored", "resolved_id": "tip", "title": "Group: room"}]}},
        ),
        "session.create": method("session.create", {"result": {"session_id": "runtime"}}),
        "session.resume": method("session.resume", {"result": {"session_id": "runtime"}}),
        "session.history": method("session.history", {"result": {"messages": [{"role": "assistant"}]}}),
        "session.interrupt": method("session.interrupt", {"result": {"interrupted": True}}),
        "approval.respond": method("approval.respond", {"result": {"resolved": 1}}),
        "prompt.submit": method("prompt.submit", {"result": {"status": "streaming"}}),
    }
    server = SimpleNamespace(
        _methods=methods,
        _sessions=sessions,
        _sessions_lock=threading.Lock(),
        _pending_approval_request_payload=lambda _session_key: None,
    )
    return server, calls


def test_routes_exact_hidden_session_and_internal_task_proof():
    server, calls = _server()
    rpc = HostedRoomServerRPC(server)
    task = TaskIdentity("room", "task", "thread", "turn")
    callback = lambda _receipt: None

    assert rpc.resolve_exact(profile="ops", title="Group: room", source="bot_room")["session_id"] == "tip"
    assert rpc.create(profile="ops", title="Group: room", source="bot_room")["session_id"] == "runtime"
    rpc.submit(
        profile="ops",
        session_id="runtime",
        prompt="Do the work",
        source="bot_room",
        task=task,
        execution_generation=2,
        on_terminal=callback,
    )

    create = next(params for method, params in calls if method == "session.create")
    submit = next(params for method, params in calls if method == "prompt.submit")
    assert create["hidden"] is True
    assert create["room_plumbing"] is True
    assert create["follow_profile_config"] is True
    assert create["close_on_disconnect"] is False
    assert submit["_hosted_task"] == {
        "room_id": "room",
        "task_id": "task",
        "thread_id": "thread",
        "turn_id": "turn",
        "execution_generation": 2,
    }
    assert submit["_hosted_terminal_callback"] is callback

    rpc.resume(profile="ops", session_id="stored", source="bot_room")
    resume = next(params for method, params in calls if method == "session.resume")
    assert resume["source"] == "bot_room"


def test_info_and_interrupt_are_exact_task_scoped():
    server, calls = _server()
    lock = threading.Lock()
    server._sessions["runtime"] = {
        "history_lock": lock,
        "running": True,
        "_hosted_room_task": {"task_id": "task-a"},
    }
    rpc = HostedRoomServerRPC(server)

    assert rpc.info(profile="ops", session_id="runtime", source="bot_room") == {
        "active": True,
        "task_id": "task-a",
    }
    rpc.interrupt(
        profile="ops",
        session_id="runtime",
        source="bot_room",
        expected_task_id="task-a",
    )
    params = next(params for method, params in calls if method == "session.interrupt")
    assert params["expected_hosted_task_id"] == "task-a"


def test_local_approval_snapshot_and_response_use_exact_request():
    server, calls = _server()
    server._pending_approval_request_payload = lambda session_key: {
        "request_id": "approval-1",
        "command": "pytest -q tests/focused",
        "choices": ["once", "deny"],
    } if session_key == "stored-session" else None
    server._sessions["runtime"] = {
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "stored-session",
        "_hosted_room_task": {"task_id": "task-a"},
    }
    rpc = HostedRoomServerRPC(server)

    info = rpc.info(profile="ops", session_id="runtime", source="bot_room")
    assert info["status"] == "waiting_for_approval"
    assert info["pending_approval"]["request_id"] == "approval-1"
    assert rpc.approve(
        session_id="runtime",
        request_id="approval-1",
        choice="once",
    ) == {"resolved": 1}
    params = next(params for method, params in calls if method == "approval.respond")
    assert params == {
        "session_id": "runtime",
        "request_id": "approval-1",
        "choice": "once",
        "all": False,
    }


def test_rpc_errors_are_typed():
    server, _calls = _server()
    server._methods["session.list"] = lambda rid, _params: {
        "id": rid,
        "error": {"code": 4007, "message": "not found"},
    }
    rpc = HostedRoomServerRPC(server)

    with pytest.raises(HostedRoomSessionError) as exc:
        rpc.resolve_exact(profile="ops", title="Group: room", source="bot_room")
    assert exc.value.code == 4007


def test_real_prompt_submit_runs_agent_and_commits_terminal_receipt(
    tmp_path, monkeypatch
):
    """RPC → real prompt.submit thread → agent turn → terminal callback."""

    import tui_gateway.server as real_server

    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    seen = []

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
            seen.append(
                {
                    "prompt": prompt,
                    "history": list(conversation_history or []),
                    "persist_user_message": persist_user_message,
                    "task_id": task_id,
                }
            )
            if stream_callback is not None:
                stream_callback("worker done")
            return {
                "final_response": "worker done",
                "messages": [
                    {"role": "user", "content": persist_user_message or prompt},
                    {"role": "assistant", "content": "worker done"},
                ],
                "completed": True,
            }

    agent = _DeterministicAgent()

    def _schedule_build(sid):
        session = real_server._sessions[sid]
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

    with real_server._sessions_lock:
        prior_sessions = dict(real_server._sessions)
        real_server._sessions.clear()
    try:
        rpc = HostedRoomServerRPC(real_server)
        created = rpc.create(
            profile="ops", title="Group: room-e2e", source="bot_room"
        )
        receipt = []
        terminal = threading.Event()

        accepted = rpc.submit(
            profile="ops",
            session_id=str(created["session_id"]),
            prompt="execute deterministic room task",
            source="bot_room",
            task=TaskIdentity("room-e2e", "task-1", "thread-1", "turn-1"),
            execution_generation=1,
            on_terminal=lambda value: (receipt.append(dict(value)), terminal.set()),
        )

        assert accepted["status"] == "streaming"
        assert terminal.wait(5.0), "real prompt.submit turn did not settle"
        assert seen == [
            {
                "prompt": "execute deterministic room task",
                "history": [],
                "persist_user_message": "execute deterministic room task",
                "task_id": created["stored_session_id"],
            }
        ]
        assert receipt == [{"status": "settled", "text": "worker done"}]
        with real_server._sessions_lock:
            session = real_server._sessions[str(created["session_id"])]
            run_thread = session.get("_run_thread")
        if run_thread is not None:
            run_thread.join(timeout=5.0)
            assert not run_thread.is_alive()
        deadline = time.time() + 5.0
        resolved = None
        while time.time() < deadline and resolved is None:
            resolved = rpc.resolve_exact(
                profile="ops", title="Group: room-e2e", source="bot_room"
            )
            if resolved is None:
                time.sleep(0.01)
        assert resolved is not None
        assert resolved["session_id"] == created["stored_session_id"]
    finally:
        with real_server._sessions_lock:
            real_server._sessions.clear()
            real_server._sessions.update(prior_sessions)



def test_prompt_rejection_is_proven_not_admitted():
    server, _calls = _server()
    server._methods["prompt.submit"] = lambda rid, _params: {
        "id": rid,
        "error": {"code": 4121, "message": "session is already busy"},
    }
    rpc = HostedRoomServerRPC(server)

    with pytest.raises(HostedRoomSessionError) as exc:
        rpc.submit(
            profile="ops",
            session_id="runtime",
            prompt="Do the work",
            source="bot_room",
            task=TaskIdentity("room", "task", "thread", "turn"),
            execution_generation=1,
            on_terminal=lambda _receipt: None,
        )

    assert exc.value.code == 4121
    assert exc.value.not_admitted is True
