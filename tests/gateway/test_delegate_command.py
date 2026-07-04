"""Gateway /delegate and /swarm Kanban wrapper tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.source_agent_binding import SourceAgentBindingStore
from hermes_cli import kanban_db as kb


def _make_runner(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._source_agent_binding_store = SourceAgentBindingStore(
        tmp_path / "bindings.sqlite"
    )
    runner._agent_audit_path = tmp_path / "agent-audit.jsonl"
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


def _seed_profile(root, name: str):
    profile_dir = root / "profiles" / name
    profile_dir.mkdir(parents=True)
    return profile_dir


@pytest.mark.asyncio
async def test_delegate_creates_kanban_task_and_subscribes_source(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_delegate_command(
        _make_event("/delegate worker summarize the incident report")
    )

    assert "Delegated task `" in result
    assert "on board `default`" in result
    assert "Subscription: current chat is subscribed" in result
    task_id = result.split("`", 2)[1]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        subs = kb.list_notify_subs(conn, task_id)
    finally:
        conn.close()

    assert task is not None
    assert task.assignee == "worker"
    assert task.title == "summarize the incident report"
    assert task.body == "summarize the incident report"
    assert subs and subs[0]["platform"] == "dingtalk"
    assert subs[0]["chat_id"] == "chat-1"
    assert subs[0]["thread_id"] == "thread-1"
    assert subs[0]["notifier_profile"] == "default"

    audit = json.loads((tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert audit["action"] == "delegate.create"
    assert audit["profile_name"] == "worker"
    assert audit["after"]["task_id"] == task_id


@pytest.mark.asyncio
async def test_delegate_supports_kanban_options(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    kb.create_board("ops")
    runner = _make_runner(tmp_path)

    result = await runner._handle_delegate_command(
        _make_event(
            "/delegate --board ops worker --workspace "
            f"dir:{workspace} --priority 7 --max-runtime 5m "
            "--skill translation investigate outage"
        )
    )

    assert "Delegated task `" in result
    assert "on board `ops`" in result
    task_id = result.split("`", 2)[1]
    conn = kb.connect(board="ops")
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert task is not None
    assert task.assignee == "worker"
    assert task.title == "investigate outage"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(workspace)
    assert task.priority == 7
    assert task.max_runtime_seconds == 300
    assert task.skills == ["translation"]

    audit = json.loads((tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert audit["after"]["board"] == "ops"
    assert audit["after"]["workspace"] == f"dir:{workspace}"
    assert audit["after"]["priority"] == 7
    assert audit["after"]["max_runtime_seconds"] == 300
    assert audit["after"]["skills"] == ["translation"]


@pytest.mark.asyncio
async def test_delegate_auto_creates_triage_task(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_delegate_command(
        _make_event("/delegate auto break this work into the right agents")
    )

    assert "Delegated triage task `" in result
    task_id = result.split("`", 2)[1]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert task is not None
    assert task.assignee is None
    assert task.status == "triage"
    assert task.title == "break this work into the right agents"

    audit = json.loads((tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert audit["profile_name"] == "auto"
    assert audit["after"]["auto_route"] is True


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_board_without_creating_it(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_delegate_command(
        _make_event("/delegate --board missing worker do work")
    )

    assert "Unknown Kanban board `missing`" in result
    assert not (root / "kanban" / "boards" / "missing").exists()


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_delegate_command(_make_event("/delegate missing do work"))

    assert "Unknown agent profile `missing`" in result
    assert not (root / "kanban.db").exists()


@pytest.mark.asyncio
async def test_delegate_does_not_change_current_agent_binding(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    _seed_profile(root, "current")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)
    event = _make_event("/delegate worker do work")
    source_key = runner._agent_source_binding_key(event.source)
    runner._source_agent_binding_store.set_binding(source_key, "current")

    await runner._handle_delegate_command(event)

    binding = runner._source_agent_binding_store.get_binding(source_key)
    assert binding.profile_name == "current"


@pytest.mark.asyncio
async def test_delegate_closes_kanban_connection(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)
    real_connect = kb.connect
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

    monkeypatch.setattr(kb, "connect", connect_spy)

    result = await runner._handle_delegate_command(_make_event("/delegate worker do work"))

    assert "Delegated task `" in result
    assert opened
    assert all(conn.closed for conn in opened)


@pytest.mark.asyncio
async def test_swarm_creates_graph_and_subscribes_source(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "coder")
    _seed_profile(root, "reviewer")
    _seed_profile(root, "pm")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_swarm_command(
        _make_event(
            '/swarm "ship profile polish" '
            "--worker coder:Implement:translation "
            "--verifier reviewer --synthesizer pm --priority 4"
        )
    )

    assert "Created swarm root `" in result
    root_id = result.split("`", 2)[1]
    conn = kb.connect()
    try:
        root_task = kb.get_task(conn, root_id)
        subs = kb.list_notify_subs(conn)
    finally:
        conn.close()

    assert root_task is not None
    assert root_task.status == "done"
    assert root_task.priority == 4
    assert len(subs) == 4
    assert {sub["platform"] for sub in subs} == {"dingtalk"}

    audit = json.loads((tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert audit["action"] == "swarm.create"
    assert audit["profile_name"] == "pm"
    assert audit["after"]["root_id"] == root_id
    assert audit["after"]["priority"] == 4


@pytest.mark.asyncio
async def test_swarm_rejects_missing_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "coder")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_swarm_command(
        _make_event(
            '/swarm "ship profile polish" '
            "--worker coder:Implement --verifier missing --synthesizer coder"
        )
    )

    assert "Unknown agent profile(s): missing" in result
