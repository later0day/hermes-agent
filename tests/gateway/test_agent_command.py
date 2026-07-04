"""Gateway /agent command tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


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
    runner.adapters = {}
    return runner


def _make_event(text: str, *, raw_message=None) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.DINGTALK,
            chat_id="chat-1",
            chat_type="group",
            user_id="user-1",
            user_name="Alice",
        ),
        raw_message=raw_message,
    )


def _seed_profile(root, name: str):
    profile_dir = root / "profiles" / name
    (profile_dir / "skills").mkdir(parents=True)
    (profile_dir / "workspace").mkdir()
    return profile_dir


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="msg-1")


@pytest.mark.asyncio
async def test_agent_use_status_and_clear(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    event = _make_event("/agent use worker")
    result = await runner._handle_agent_command(event)

    source_key = build_source_binding_key(event.source)
    binding = runner._source_agent_binding_store.get_binding(source_key)
    assert "Bound this chat to agent `worker`." in result
    assert "DingTalk fallback webhook is missing" in result
    assert binding.profile_name == "worker"
    assert binding.agent_id == "worker"

    status = await runner._handle_agent_command(_make_event("/agent status"))
    assert "Profile: `worker`" in status
    assert "DingTalk fallback webhook: missing" in status

    clear = await runner._handle_agent_command(_make_event("/agent clear"))
    assert "uses `default`" in clear
    assert runner._source_agent_binding_store.get_binding(source_key) is None

    audit_lines = (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["action"] for line in audit_lines] == [
        "agent.use",
        "agent.clear",
    ]


@pytest.mark.asyncio
async def test_agent_webhook_stores_dingtalk_fallback(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    await runner._handle_agent_command(_make_event("/agent use worker"))
    raw = SimpleNamespace(
        session_webhook="https://api.dingtalk.com/robot/sendBySession?session=abc",
        session_webhook_expired_time=9999999999999,
    )
    result = await runner._handle_agent_command(
        _make_event("/agent webhook", raw_message=raw)
    )

    binding = runner._source_agent_binding_store.get_binding(
        build_source_binding_key(_make_event("/agent status").source)
    )
    assert "Stored DingTalk fallback webhook for agent `worker`." in result
    assert binding.fallback_extra == {
        "session_webhook": "https://api.dingtalk.com/robot/sendBySession?session=abc",
        "session_webhook_expired_time": 9999999999999,
    }


@pytest.mark.asyncio
async def test_agent_create_clones_profile_without_env_or_skills_by_default(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    (root / "config.yaml").write_text("model:\n  default: gpt-test\n", encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (root / "SOUL.md").write_text("Default soul\n", encoding="utf-8")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_agent_command(
        _make_event("/agent create worker --description Handles queued work")
    )

    profile_dir = root / "profiles" / "worker"
    assert "Created agent profile `worker`" in result
    assert ".env not copied" in result
    assert "no skills copied" in result
    assert (profile_dir / "config.yaml").exists()
    assert not (profile_dir / ".env").exists()
    assert not (profile_dir / "skills" / "demo" / "SKILL.md").exists()
    assert "Handles queued work" in (profile_dir / "profile.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_create_orchestrator_enables_kanban_toolset(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    (root / "config.yaml").write_text(
        "model:\n  default: gpt-test\ntoolsets:\n  - hermes-cli\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    plain = await runner._handle_agent_command(_make_event("/agent create worker"))
    orchestrator = await runner._handle_agent_command(
        _make_event("/agent create lead --orchestrator")
    )

    plain_cfg = yaml.safe_load(
        (root / "profiles" / "worker" / "config.yaml").read_text(encoding="utf-8")
    )
    lead_cfg = yaml.safe_load(
        (root / "profiles" / "lead" / "config.yaml").read_text(encoding="utf-8")
    )
    audit_after = [
        json.loads(line)["after"]
        for line in (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert "Created agent profile `worker`" in plain
    assert "kanban orchestrator tools enabled" not in plain
    assert "kanban" not in plain_cfg.get("toolsets", [])
    assert "Created agent profile `lead`" in orchestrator
    assert "kanban orchestrator tools enabled" in orchestrator
    assert lead_cfg["toolsets"] == ["hermes-cli", "kanban"]
    assert [entry["profile_name"] for entry in audit_after] == ["worker", "lead"]
    assert [entry["orchestrator"] for entry in audit_after] == [False, True]


@pytest.mark.asyncio
async def test_agent_create_with_env_is_explicit(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    (root / "config.yaml").write_text("model:\n  default: gpt-test\n", encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_agent_command(_make_event("/agent create secret --with-env"))

    assert ".env copied" in result
    assert (root / "profiles" / "secret" / ".env").exists()
    assert not (root / "profiles" / "secret" / "skills" / "demo" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_agent_create_from_template_requires_template_flag(tmp_path, monkeypatch):
    from hermes_cli.profiles import write_profile_meta

    root = tmp_path / "hermes-home"
    root.mkdir()
    template_dir = _seed_profile(root, "template_agent")
    (template_dir / "config.yaml").write_text("model:\n  default: gpt-template\n", encoding="utf-8")
    (template_dir / "SOUL.md").write_text("Template soul\n", encoding="utf-8")
    (template_dir / "skills" / "demo").mkdir(parents=True)
    (template_dir / "skills" / "demo" / "SKILL.md").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    rejected = await runner._handle_agent_command(
        _make_event("/agent create cloned --from-template template_agent")
    )
    write_profile_meta(template_dir, template=True)
    created = await runner._handle_agent_command(
        _make_event("/agent create cloned --from-template template_agent")
    )

    cloned_dir = root / "profiles" / "cloned"
    audit_actions = [
        json.loads(line)["action"]
        for line in (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "not marked as a template" in rejected
    assert "Created agent profile `cloned`" in created
    assert (cloned_dir / "config.yaml").exists()
    assert not (cloned_dir / ".env").exists()
    assert not (cloned_dir / "skills" / "demo" / "SKILL.md").exists()
    assert audit_actions == ["agent.template_clone"]


@pytest.mark.asyncio
async def test_agent_list_marks_template_profiles(tmp_path, monkeypatch):
    from hermes_cli.profiles import write_profile_meta

    root = tmp_path / "hermes-home"
    root.mkdir()
    starter_dir = _seed_profile(root, "starter")
    write_profile_meta(starter_dir, template=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_agent_command(_make_event("/agent list"))

    starter_line = next(line for line in result.splitlines() if "`starter`" in line)
    assert starter_line == "- `starter` (model unset, skills: 0, template)"


@pytest.mark.asyncio
async def test_agent_delete_requires_confirmation_and_removes_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    profile_dir = _seed_profile(root, "worker")
    (profile_dir / "workspace" / "note.txt").write_text("state", encoding="utf-8")
    runner = _make_runner(tmp_path)
    runner._agent_delete_code_factory = lambda: "ABC123"

    await runner._handle_agent_command(_make_event("/agent use worker"))
    request = await runner._handle_agent_command(_make_event("/agent delete worker"))
    wrong = await runner._handle_agent_command(_make_event("/agent delete worker BAD999"))
    confirmed = await runner._handle_agent_command(_make_event("/agent delete worker ABC123"))

    source_key = build_source_binding_key(_make_event("/agent status").source)
    assert "Confirm within" in request
    assert "/agent delete worker ABC123" in request
    assert "incorrect" in wrong
    assert "Deleted agent profile `worker`" in confirmed
    assert not profile_dir.exists()
    assert runner._source_agent_binding_store.get_binding(source_key) is None
    audit_actions = [
        json.loads(line)["action"]
        for line in (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "agent.delete.request" in audit_actions
    assert "agent.delete.failed" in audit_actions
    assert "agent.delete" in audit_actions


@pytest.mark.asyncio
async def test_agent_delete_notifies_other_bound_im_sessions(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _seed_profile(root, "worker")
    runner = _make_runner(tmp_path)
    runner._agent_delete_code_factory = lambda: "ABC123"
    fake_adapter = _FakeAdapter()
    runner.adapters = {Platform.DINGTALK: fake_adapter}

    await runner._handle_agent_command(_make_event("/agent use worker"))
    other_source = SessionSource(
        platform=Platform.DINGTALK,
        chat_id="chat-2",
        chat_type="group",
        user_id="user-2",
        user_name="Bob",
    )
    other_key = build_source_binding_key(other_source)
    runner._source_agent_binding_store.set_binding(
        other_key,
        "worker",
        agent_id="worker",
        fallback_target=other_source.to_dict(),
        fallback_extra={
            "session_webhook": "https://api.dingtalk.com/robot/sendBySession?session=other",
            "session_webhook_expired_time": 9999999999999,
        },
    )

    await runner._handle_agent_command(_make_event("/agent delete worker"))
    confirmed = await runner._handle_agent_command(_make_event("/agent delete worker ABC123"))
    audit_events = [
        json.loads(line)
        for line in (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert "Deleted agent profile `worker`" in confirmed
    assert len(fake_adapter.sent) == 1
    assert fake_adapter.sent[0][0] == "chat-2"
    assert fake_adapter.sent[0][2] == {
        "session_webhook": "https://api.dingtalk.com/robot/sendBySession?session=other",
    }
    assert runner._source_agent_binding_store.list_bindings(profile_name="worker") == []
    assert audit_events[-1]["extra"]["removed_bindings"] == 2
    assert "session=other" not in json.dumps(audit_events[-1])


@pytest.mark.asyncio
async def test_agent_delete_protects_default_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path)

    result = await runner._handle_agent_command(_make_event("/agent delete default"))

    assert "Refusing to delete `default`" in result
    assert root.exists()


@pytest.mark.asyncio
async def test_agent_delete_confirmation_expires(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _seed_profile(root, "worker")
    runner = _make_runner(tmp_path)
    runner._agent_delete_code_factory = lambda: "ABC123"

    await runner._handle_agent_command(_make_event("/agent delete worker"))
    for request in runner._agent_delete_confirmations.values():
        request["expires_at"] = 0

    result = await runner._handle_agent_command(_make_event("/agent delete worker ABC123"))

    assert "expired" in result
    assert (root / "profiles" / "worker").exists()
