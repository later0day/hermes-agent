"""E3-level tests for /agent create|delete|list|webhook subcommands.

Extends ``test_agent_command_basic.py`` (which covers use/clear/status).
All tests use the ``object.__new__(GatewayRunner)`` + ``_handle_agent_command``
pattern so they exercise the real slash-command handler without a running gateway.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_profile_identity(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")


def _source(chat_id="group-x", user_id="u1", chat_type="group"):
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


def _runner(store: SourceAgentBindingStore, *, multiplex=True, audit_path=None):
    r = object.__new__(GatewayRunner)
    # Handler behavior for an authorized caller; the admin gate itself is pinned in
    # test_agent_command_dispatch.py through the real dispatch + slash-access policy.
    r._resume_caller_is_admin = lambda source: True
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
    # _agent_delete_confirmations dict for delete flow
    r._agent_delete_confirmations = {}
    return r


def _event(source, text, raw=None):
    return SimpleNamespace(source=source, text=text, raw_message=raw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Provides a seeded profile directory, binding store, and audit path."""
    root = tmp_path / "hermes-home"
    profiles = root / "profiles"
    profiles.mkdir(parents=True)

    # Seed default + a template profile
    _seed_profile_identity(profiles / "default")
    template_dir = profiles / "template-worker"
    _seed_profile_identity(template_dir)
    # Mark as template
    template_yaml = template_dir / "profile.yaml"
    template_yaml.write_text("template: true\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    audit_path = tmp_path / "agent-audit.jsonl"
    yield SimpleNamespace(store=store, audit_path=audit_path, root=root, profiles=profiles)
    store.close()


# ── /agent list ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_shows_profiles(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent list"))
    assert "default" in out


@pytest.mark.asyncio
async def test_list_marks_template_profiles(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent list"))
    assert "template-worker" in out
    assert "template" in out


# ── /agent webhook ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_no_binding_reports_error(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent webhook"))
    assert "No agent bound" in out


@pytest.mark.asyncio
async def test_webhook_updates_existing_binding(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    # Seed a profile first
    _seed_profile_identity(env.profiles / "coder")
    await r._handle_agent_command(_event(src, "/agent use coder"))

    raw = SimpleNamespace(
        session_webhook="https://api.dingtalk.com/webhook/updated",
        session_webhook_expired_time=9999999999,
    )
    out = await r._handle_agent_command(_event(src, "/agent webhook", raw=raw))
    assert "Stored DingTalk fallback webhook" in out
    binding = env.store.get_binding(build_source_binding_key(src))
    assert binding.fallback_extra["session_webhook"] == "https://api.dingtalk.com/webhook/updated"


@pytest.mark.asyncio
async def test_webhook_no_raw_message(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    _seed_profile_identity(env.profiles / "coder")
    await r._handle_agent_command(_event(src, "/agent use coder"))

    out = await r._handle_agent_command(_event(src, "/agent webhook"))  # No raw_message
    assert "No webhook found" in out


# ── /agent create ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_basic(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent create my-agent"))
    assert "Created agent profile `my-agent`" in out
    assert (env.profiles / "my-agent").is_dir()


@pytest.mark.asyncio
async def test_create_with_description(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(
        _event(_source(), "/agent create my-agent --description a test agent")
    )
    assert "Created agent profile `my-agent`" in out
    # profile.yaml should have description set
    pyaml = env.profiles / "my-agent" / "profile.yaml"
    assert pyaml.is_file()
    content = pyaml.read_text(encoding="utf-8")
    assert "a test agent" in content


@pytest.mark.asyncio
async def test_create_as_template(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(
        _event(_source(), "/agent create my-template --template --description a template")
    )
    assert "Created agent profile `my-template`" in out
    assert "Marked as template" in out
    pyaml = env.profiles / "my-template" / "profile.yaml"
    assert pyaml.is_file()
    content = pyaml.read_text(encoding="utf-8")
    assert "template: true" in content


@pytest.mark.asyncio
async def test_create_from_template_rejects_non_template(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(
        _event(_source(), "/agent create cloned --from-template default")
    )
    assert "not marked as a template" in out or "Profile" in out


@pytest.mark.asyncio
async def test_create_usage_without_name(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent create"))
    assert "Usage:" in out


# ── /agent delete ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_refuses_default(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent delete default"))
    assert "Refusing to delete" in out


@pytest.mark.asyncio
async def test_delete_requires_confirmation(env):
    r = _runner(env.store, audit_path=env.audit_path)
    _seed_profile_identity(env.profiles / "to-delete")
    out = await r._handle_agent_command(_event(_source(), "/agent delete to-delete"))
    assert "Confirm within 5m" in out


@pytest.mark.asyncio
async def test_delete_wrong_code_rejected(env):
    r = _runner(env.store, audit_path=env.audit_path)
    _seed_profile_identity(env.profiles / "to-delete")
    # First, request deletion to get a code
    await r._handle_agent_command(_event(_source(), "/agent delete to-delete"))
    # Then provide wrong code
    out = await r._handle_agent_command(
        _event(_source(), "/agent delete to-delete WRONGCODE")
    )
    assert "Code incorrect" in out


@pytest.mark.asyncio
async def test_delete_expired_confirmation(env):
    r = _runner(env.store, audit_path=env.audit_path)
    _seed_profile_identity(env.profiles / "to-delete")
    await r._handle_agent_command(_event(_source(), "/agent delete to-delete"))
    # Expire the confirmation
    src_key = build_source_binding_key(_source())
    r._agent_delete_confirmations[src_key]["expires_at"] = time.time() - 1
    out = await r._handle_agent_command(
        _event(_source(), "/agent delete to-delete ANYCODE")
    )
    assert "expired" in out.lower()


@pytest.mark.asyncio
async def test_delete_no_pending_for_profile(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(
        _event(_source(), "/agent delete other-profile ANYCODE")
    )
    assert "No pending deletion" in out


@pytest.mark.asyncio
async def test_audit_log_records_create_and_delete_flow(env):
    r = _runner(env.store, audit_path=env.audit_path)
    src = _source()
    await r._handle_agent_command(_event(src, "/agent create test-audit --template"))
    await r._handle_agent_command(_event(src, "/agent delete test-audit"))

    lines = env.audit_path.read_text(encoding="utf-8").strip().splitlines()
    payloads = [json.loads(line) for line in lines]
    actions = [p["action"] for p in payloads]
    assert "agent.create" in actions
    assert "agent.delete.request" in actions


# ── /agent unrecognized action ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unrecognized_action_shows_usage(env):
    r = _runner(env.store, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent unknown_subcommand"))
    assert "Usage:" in out


# ── multiplex_off ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_rejected_when_multiplex_off(env):
    r = _runner(env.store, multiplex=False, audit_path=env.audit_path)
    out = await r._handle_agent_command(_event(_source(), "/agent create test"))
    assert "Dynamic profile binding is disabled" in out