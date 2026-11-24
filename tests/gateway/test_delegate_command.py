"""Gateway ``/kanban create`` and ``/kanban swarm`` behavior contracts."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def _use_kanban_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    return root


def _make_runner(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._source_agent_binding_store = SourceAgentBindingStore(
        tmp_path / "bindings.sqlite"
    )
    runner._kanban_notifier_profile = "default"
    return runner


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.DINGTALK,
            chat_id="chat-1",
            chat_type="group",
            user_id="user-1",
            user_name="Alice",
            thread_id="thread-1",
        ),
    )


def _created_task_id(reply: str) -> str:
    match = re.search(r"\bCreated\s+(t_[0-9a-f]+)\b", reply)
    assert match, reply
    return match.group(1)


@pytest.mark.asyncio
async def test_create_persists_task_and_subscribes_source(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban create "summarize the incident report" '
        '--body "summarize the incident report" --assignee worker'
    ))

    assert "Created t_" in reply
    assert "subscribed" in reply
    task_id = _created_task_id(reply)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)

    assert task is not None
    assert task.assignee == "worker"
    assert task.title == "summarize the incident report"
    assert task.body == "summarize the incident report"
    assert len(subs) == 1
    assert subs[0]["platform"] == "dingtalk"
    assert subs[0]["chat_id"] == "chat-1"
    assert subs[0]["thread_id"] == "thread-1"
    assert subs[0]["user_id"] == "user-1"
    assert subs[0]["notifier_profile"] == "default"
    assert subs[0]["delivery_mode"] == "notify+wake"


@pytest.mark.asyncio
async def test_create_supports_kanban_board_and_task_options(tmp_path, monkeypatch):
    root = _use_kanban_home(tmp_path, monkeypatch)
    workspace = tmp_path / "work"
    workspace.mkdir()
    kb.create_board("ops")
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        "/kanban --board ops create \"investigate outage\" --assignee worker "
        f"--workspace dir:{workspace} --priority 7 --max-runtime 5m "
        "--skill translation"
    ))

    task_id = _created_task_id(reply)
    with kbc.connect_closing(board="ops") as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)

    assert task is not None
    assert task.assignee == "worker"
    assert task.title == "investigate outage"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(workspace)
    assert task.priority == 7
    assert task.max_runtime_seconds == 300
    assert task.skills == ["translation"]
    assert [sub["chat_id"] for sub in subs] == ["chat-1"]
    assert not (root / "kanban.db").exists(), "the named board must own the new task"


@pytest.mark.asyncio
async def test_create_triage_parks_an_unassigned_task(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban create "break this work into the right agents" --triage'
    ))

    task_id = _created_task_id(reply)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.assignee is None
    assert task.status == "triage"
    assert task.title == "break this work into the right agents"


@pytest.mark.asyncio
async def test_create_rejects_unknown_board_without_creating_it(tmp_path, monkeypatch):
    root = _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban --board missing create "do work" --assignee worker'
    ))

    assert "board 'missing' does not exist" in reply
    assert not (root / "kanban" / "boards" / "missing").exists()
    assert not (root / "kanban.db").exists()


@pytest.mark.asyncio
async def test_create_does_not_change_current_agent_binding(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)
    event = _make_event('/kanban create "do work" --assignee worker')
    source_key = build_source_binding_key(event.source)
    runner._source_agent_binding_store.set_binding(source_key, "current")

    await runner._handle_kanban_command(event)

    binding = runner._source_agent_binding_store.get_binding(source_key)
    assert binding is not None
    assert binding.profile_name == "current"


@pytest.mark.asyncio
async def test_create_closes_command_and_subscription_connections(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)
    real_connect = kbc.connect
    opened = []

    class ConnectionSpy:
        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def close(self):
            self.closed = True
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def connect_spy(*args, **kwargs):
        spy = ConnectionSpy(real_connect(*args, **kwargs))
        opened.append(spy)
        return spy

    monkeypatch.setattr(kbc, "connect", connect_spy)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban create "do work" --assignee worker'
    ))

    assert "Created t_" in reply
    assert len(opened) >= 2, "both command and subscription work must open connections"
    assert all(conn.closed for conn in opened)


@pytest.mark.asyncio
async def test_swarm_creates_dependency_graph_and_subscribes_synthesizer(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban swarm "ship profile polish" '
        "--worker coder:Implement:translation "
        "--verifier reviewer --synthesizer pm --priority 4"
    ))

    root_id = reply.split("Swarm root:", 1)[1].split()[0]
    worker_ids = [
        task_id.strip()
        for task_id in reply.split("Workers:", 1)[1].splitlines()[0].split(",")
    ]
    verifier_id = reply.split("Verifier:", 1)[1].split()[0]
    synthesizer_id = reply.split("Synthesizer:", 1)[1].split()[0]
    with kbc.connect_closing() as conn:
        root_task = kb.get_task(conn, root_id)
        workers = [kb.get_task(conn, task_id) for task_id in worker_ids]
        verifier = kb.get_task(conn, verifier_id)
        synthesizer = kb.get_task(conn, synthesizer_id)
        worker_parents = [kb.parent_ids(conn, task_id) for task_id in worker_ids]
        verifier_parents = kb.parent_ids(conn, verifier_id)
        synthesizer_parents = kb.parent_ids(conn, synthesizer_id)
        subs = kbn.list_notify_subs(conn)

    assert root_task is not None
    assert root_task.status == "done"
    assert root_task.priority == 4
    assert len(workers) == 1
    assert workers[0].assignee == "coder"
    assert workers[0].title == "Implement"
    assert workers[0].skills == ["translation"]
    assert worker_parents == [[root_id]]
    assert verifier.assignee == "reviewer"
    assert verifier_parents == worker_ids
    assert synthesizer.assignee == "pm"
    assert synthesizer_parents == [verifier_id]
    assert len(subs) == 1, "only the synthesizer produces the swarm's final answer"
    assert subs[0]["task_id"] == synthesizer_id
    assert subs[0]["platform"] == "dingtalk"
    assert subs[0]["chat_id"] == "chat-1"
    assert subs[0]["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_swarm_rejects_malformed_worker_without_creating_tasks(tmp_path, monkeypatch):
    _use_kanban_home(tmp_path, monkeypatch)
    runner = _make_runner(tmp_path)

    reply = await runner._handle_kanban_command(_make_event(
        '/kanban swarm "ship profile polish" '
        "--worker coder --verifier reviewer --synthesizer pm"
    ))

    assert "worker must be profile:title" in reply
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []
