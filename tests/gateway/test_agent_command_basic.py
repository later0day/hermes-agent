"""E2 handler tests for /agent use|clear|status.

Pins the phase-1 subcommand surface: use, clear, status. The webhook/list/create/
delete surface lives in E3 and is exercised by ``tests/gateway/test_agent_command.py``
once ported.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


def _seed_profile_identity(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("", encoding="utf-8")


def _source(chat_id="group-x", user_id="u1", chat_type="group"):
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


def _runner(store: SourceAgentBindingStore, *, multiplex=True, audit_path=None):
    r = object.__new__(GatewayRunner)
    cfg = SimpleNamespace(
        profile_routes=[],
        multiplex_profiles=multiplex,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    r.config = cfg
    r._source_agent_binding_store = store
    r._profiles_being_deleted = set()
    r._agent_audit_path = audit_path
    return r


def _event(source, text, raw=None):
    return SimpleNamespace(source=source, text=text, raw_message=raw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    _seed_profile_identity(root / "profiles" / "coder")
    _seed_profile_identity(root / "profiles" / "reviewer")
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    audit_path = tmp_path / "agent-audit.jsonl"
    yield SimpleNamespace(store=store, audit_path=audit_path, root=root)
    store.close()


@pytest.mark.asyncio
async def test_status_reports_no_binding_when_unbound(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent status"))
    assert "No dynamic binding" in out


@pytest.mark.asyncio
async def test_use_binds_and_status_reports_profile(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    out = await r._handle_agent_command(_event(src, "/agent use coder"))
    assert "Bound this chat to agent `coder`" in out
    # Per-user AND chat-level rows created for a group.
    assert env.store.get_binding(build_source_binding_key(src)).profile_name == "coder"
    assert (
        env.store.get_binding(
            build_source_binding_key(src, group_sessions_per_user=False)
        ).profile_name
        == "coder"
    )
    status = await r._handle_agent_command(_event(src, "/agent status"))
    assert "Profile: `coder`" in status


@pytest.mark.asyncio
async def test_clear_removes_binding_and_reports_effective(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    await r._handle_agent_command(_event(src, "/agent use coder"))
    out = await r._handle_agent_command(_event(src, "/agent clear"))
    assert "Cleared dynamic binding" in out
    assert env.store.get_binding(build_source_binding_key(src)) is None


@pytest.mark.asyncio
async def test_use_rejects_missing_profile(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent use nonexistent"))
    assert "does not exist" in out


@pytest.mark.asyncio
async def test_use_rejects_invalid_profile_name(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent use ../evil"))
    assert "Invalid profile" in out or "does not exist" in out


@pytest.mark.asyncio
async def test_multiplex_off_rejects(env):
    r = _runner(env.store, multiplex=False, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent use coder"))
    assert "Dynamic profile binding is disabled" in out


@pytest.mark.asyncio
async def test_usage_line_when_no_action(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent"))
    assert "Usage:" in out


@pytest.mark.asyncio
async def test_group_binding_shared_across_members(env):
    r = _runner(env.store, audit_path=env.audit_path)
    alice = _source(user_id="alice")
    bob = _source(user_id="bob")
    await r._handle_agent_command(_event(alice, "/agent use coder"))
    # Bob never ran /agent use but should still route to coder via chat-level key.
    assert r._binding_profile_for_source(bob) == "coder"


@pytest.mark.asyncio
async def test_audit_log_records_use_and_clear(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    await r._handle_agent_command(_event(src, "/agent use coder"))
    await r._handle_agent_command(_event(src, "/agent clear"))
    lines = env.audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import json

    payloads = [json.loads(line) for line in lines]
    actions = [p["action"] for p in payloads]
    assert actions == ["agent.use", "agent.clear"]
    assert payloads[0]["profile"] == "coder"


@pytest.mark.asyncio
async def test_status_after_clear_reports_no_binding(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    await r._handle_agent_command(_event(src, "/agent use coder"))
    await r._handle_agent_command(_event(src, "/agent clear"))
    out = await r._handle_agent_command(_event(src, "/agent status"))
    assert "No dynamic binding" in out


@pytest.mark.asyncio
async def test_use_captures_dingtalk_webhook_from_raw(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    raw = SimpleNamespace(
        session_webhook="https://api.dingtalk.com/webhook",
        session_webhook_expired_time=1234567890,
    )
    out = await r._handle_agent_command(_event(src, "/agent use coder", raw=raw))
    assert "webhook is present" in out
    binding = env.store.get_binding(build_source_binding_key(src))
    assert binding.fallback_extra["session_webhook"] == "https://api.dingtalk.com/webhook"
