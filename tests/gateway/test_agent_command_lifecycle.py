"""E3 handler tests for /agent webhook|list|create|delete.

Complements ``test_agent_command_basic.py`` (use|clear|status) with the
profile-lifecycle subcommands: proactive webhook refresh across chat-level
rows, ``list`` with real model/skill counts and template marker, ``create``
with the .env/skills gate + --template + --orchestrator, and the ``delete``
two-step confirm flow with cross-chat notification.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


def _seed_profile_identity(profile_dir: Path, *, template: bool = False,
                           config: dict | None = None,
                           skills: list[str] | None = None) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    if config is None:
        (profile_dir / "config.yaml").write_text("", encoding="utf-8")
    else:
        (profile_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if template:
        (profile_dir / "profile.yaml").write_text(
            yaml.safe_dump({"template": True}), encoding="utf-8",
        )
    if skills:
        skills_dir = profile_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for s in skills:
            (skills_dir / s).mkdir(parents=True, exist_ok=True)


def _source(chat_id="group-x", user_id="u1", chat_type="group"):
    return SessionSource(
        platform=Platform.DINGTALK, chat_id=chat_id,
        chat_type=chat_type, user_id=user_id,
    )


def _runner(store, *, multiplex=True, audit_path=None, adapters=None):
    r = object.__new__(GatewayRunner)
    r.config = SimpleNamespace(
        profile_routes=[], multiplex_profiles=multiplex,
        group_sessions_per_user=True, thread_sessions_per_user=False,
    )
    r._source_agent_binding_store = store
    r._profiles_being_deleted = set()
    r._agent_audit_path = audit_path
    r.adapters = adapters or {}
    return r


def _event(source, text, raw=None):
    return SimpleNamespace(source=source, text=text, raw_message=raw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    yield SimpleNamespace(store=store, audit=tmp_path / "audit.jsonl", root=root)
    store.close()


# ── list ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_reports_real_model_and_skills_and_template_marker(env):
    _seed_profile_identity(
        env.root / "profiles" / "coder",
        config={"model": "claude-opus-4-6"},
        skills=["skill-a", "skill-b"],
    )
    _seed_profile_identity(env.root / "profiles" / "librarian", template=True)
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent list"))
    assert "coder" in out and "claude-opus-4-6" in out and "skills: 2" in out
    assert "librarian" in out and "template" in out


# ── create ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_without_with_env_omits_env_and_skills(env, tmp_path):
    default_home = env.root
    (default_home / ".env").write_text("SECRET=abc\n", encoding="utf-8")
    (default_home / "config.yaml").write_text("model: source-model\n", encoding="utf-8")
    (default_home / "skills").mkdir(parents=True, exist_ok=True)
    (default_home / "skills" / "leaked").mkdir()
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent create newproj"))
    assert "Created agent profile `newproj`" in out
    assert ".env not copied" in out
    new_dir = env.root / "profiles" / "newproj"
    # skills directory should exist (default create) but WITHOUT the source's "leaked"
    assert not (new_dir / "skills" / "leaked").exists()
    # .env exists as a placeholder seed (upstream always seeds a placeholder), but
    # its contents must NOT be the source's secret.
    if (new_dir / ".env").exists():
        assert "SECRET=abc" not in (new_dir / ".env").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_create_with_env_copies_env_and_skills(env):
    default_home = env.root
    (default_home / ".env").write_text("MY_KEY=abc\n", encoding="utf-8")
    (default_home / "config.yaml").write_text("model: source-model\n", encoding="utf-8")
    (default_home / "skills").mkdir(parents=True, exist_ok=True)
    (default_home / "skills" / "shared").mkdir()
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent create clone --with-env"))
    assert "Created agent profile `clone`" in out
    assert ".env copied" in out
    new_dir = env.root / "profiles" / "clone"
    assert "MY_KEY=abc" in (new_dir / ".env").read_text(encoding="utf-8")
    assert (new_dir / "skills" / "shared").exists()


@pytest.mark.asyncio
async def test_create_orchestrator_folds_kanban_into_toolsets(env):
    (env.root / "config.yaml").write_text("model: m\ntoolsets:\n - core\n", encoding="utf-8")
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent create orch --orchestrator"))
    assert "kanban orchestrator tools enabled" in out
    cfg = yaml.safe_load(
        (env.root / "profiles" / "orch" / "config.yaml").read_text(encoding="utf-8")
    )
    assert "kanban" in cfg.get("toolsets", [])


@pytest.mark.asyncio
async def test_create_template_marks_meta_and_from_template_requires_marker(env):
    (env.root / "config.yaml").write_text("model: m\n", encoding="utf-8")
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent create tmpl --template"))
    assert "Marked as template" in out
    from hermes_cli.profiles import get_profile_dir, read_profile_meta
    assert read_profile_meta(get_profile_dir("tmpl")).get("template") is True

    # From-template rejects a non-template source.
    _seed_profile_identity(env.root / "profiles" / "nontmpl")
    out = await r._handle_agent_command(
        _event(_source(), "/agent create clone --from-template nontmpl")
    )
    assert "not marked as a template" in out

    # ...and accepts a real template source.
    out = await r._handle_agent_command(
        _event(_source(), "/agent create clone --from-template tmpl")
    )
    assert "Created agent profile `clone`" in out


# ── delete ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_refuses_default(env):
    r = _runner(env.store, audit_path=env.audit)
    out = await r._handle_agent_command(_event(_source(), "/agent delete default"))
    assert "Refusing" in out


@pytest.mark.asyncio
async def test_delete_two_step_confirm_flow(env):
    _seed_profile_identity(env.root / "profiles" / "victim", config={"model": "m"})
    r = _runner(env.store, audit_path=env.audit)
    # inject a deterministic factory so we can assert exactness
    r._agent_delete_code_factory = lambda: "DEADBE"
    src = _source()
    step1 = await r._handle_agent_command(_event(src, "/agent delete victim"))
    assert "Confirm within 5m" in step1 and "DEADBE" in step1
    step2 = await r._handle_agent_command(_event(src, "/agent delete victim DEADBE"))
    assert "Deleted agent profile `victim`" in step2
    assert not (env.root / "profiles" / "victim").exists()


@pytest.mark.asyncio
async def test_delete_wrong_code_leaves_profile_intact(env):
    _seed_profile_identity(env.root / "profiles" / "victim", config={"model": "m"})
    r = _runner(env.store, audit_path=env.audit)
    r._agent_delete_code_factory = lambda: "GOOD01"
    src = _source()
    await r._handle_agent_command(_event(src, "/agent delete victim"))
    out = await r._handle_agent_command(_event(src, "/agent delete victim WRONG9"))
    assert "Code incorrect" in out
    assert (env.root / "profiles" / "victim").exists()


@pytest.mark.asyncio
async def test_delete_expired_confirmation(env, monkeypatch):
    _seed_profile_identity(env.root / "profiles" / "victim", config={"model": "m"})
    r = _runner(env.store, audit_path=env.audit)
    r._agent_delete_code_factory = lambda: "AAAAAA"
    src = _source()
    await r._handle_agent_command(_event(src, "/agent delete victim"))
    # force expiry
    r._agent_delete_confirmations[
        build_source_binding_key(src)
    ]["expires_at"] = 0
    out = await r._handle_agent_command(_event(src, "/agent delete victim AAAAAA"))
    assert "expired" in out.lower()
    assert (env.root / "profiles" / "victim").exists()


@pytest.mark.asyncio
async def test_delete_notifies_other_bound_chats_once_and_not_issuer(env):
    _seed_profile_identity(env.root / "profiles" / "shared", config={"model": "m"})
    # Two OTHER groups bound to `shared`, plus the issuing chat also bound.
    other_a = _source(chat_id="group-a", user_id="ua")
    other_b = _source(chat_id="group-b", user_id="ub")
    issuer = _source(chat_id="group-issuer", user_id="ui")
    for src in (other_a, other_b, issuer):
        env.store.set_binding(
            build_source_binding_key(src),
            "shared",
            fallback_target=src.to_dict(),
        )
        env.store.set_binding(
            build_source_binding_key(src, group_sessions_per_user=False),
            "shared",
            fallback_target=SessionSource(
                platform=src.platform, chat_id=src.chat_id, chat_type="group",
            ).to_dict(),
        )
    sent = []

    class _Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, metadata))

    r = _runner(env.store, audit_path=env.audit,
                adapters={Platform.DINGTALK: _Adapter()})
    r._agent_delete_code_factory = lambda: "ZZZZZZ"
    await r._handle_agent_command(_event(issuer, "/agent delete shared"))
    out = await r._handle_agent_command(_event(issuer, "/agent delete shared ZZZZZZ"))
    assert "Deleted agent profile `shared`" in out

    # Notified other groups exactly once each; issuer never notified.
    chats_notified = [c for (c, _, _) in sent]
    assert chats_notified.count("group-a") == 1
    assert chats_notified.count("group-b") == 1
    assert "group-issuer" not in chats_notified


# ── webhook ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_refreshes_both_user_and_chat_rows(env):
    _seed_profile_identity(env.root / "profiles" / "wh", config={"model": "m"})
    r = _runner(env.store, audit_path=env.audit)
    src = _source()
    await r._handle_agent_command(_event(src, "/agent use wh"))
    raw = SimpleNamespace(
        session_webhook="https://api.dingtalk.com/wh-v2",
        session_webhook_expired_time=999,
    )
    out = await r._handle_agent_command(_event(src, "/agent webhook", raw=raw))
    assert "Stored DingTalk fallback webhook" in out
    # per-user
    user_binding = env.store.get_binding(build_source_binding_key(src))
    assert user_binding.fallback_extra["session_webhook"] == "https://api.dingtalk.com/wh-v2"
    # chat-level (present because src is a group)
    chat_binding = env.store.get_binding(
        build_source_binding_key(src, group_sessions_per_user=False)
    )
    assert chat_binding.fallback_extra["session_webhook"] == "https://api.dingtalk.com/wh-v2"


@pytest.mark.asyncio
async def test_webhook_without_binding_reports_no_agent(env):
    r = _runner(env.store, audit_path=env.audit)
    raw = SimpleNamespace(session_webhook="x", session_webhook_expired_time=0)
    out = await r._handle_agent_command(_event(_source(), "/agent webhook", raw=raw))
    assert "No agent bound" in out
