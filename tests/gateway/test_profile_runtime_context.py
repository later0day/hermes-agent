"""Profile-scoped gateway runtime context tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import (
    SessionSource,
    SessionStore,
    build_session_key,
    build_source_binding_key,
    profile_scoped_session_key,
)
from gateway.source_agent_binding import SourceAgentBindingStore


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id="chat-1",
        chat_type="group",
        user_id="user-1",
        user_name="Alice",
    )


def _make_runner(tmp_path, root, monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        sessions_dir=root / "sessions",
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=True,
    )
    runner.session_store = SessionStore(
        root / "sessions",
        runner.config,
    )
    runner._session_db = runner.session_store.persistence
    runner._source_agent_binding_store = SourceAgentBindingStore(
        tmp_path / "bindings.sqlite"
    )
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    return runner


def _seed_profile(root, name: str, model: str = "worker-model"):
    profile_dir = root / "profiles" / name
    (profile_dir / "sessions").mkdir(parents=True)
    (profile_dir / "skills").mkdir()
    (profile_dir / "config.yaml").write_text(
        f"model:\n  provider: openrouter\n  default: {model}\n",
        encoding="utf-8",
    )
    return profile_dir


def test_profile_scoped_session_key_keeps_default_backward_compatible():
    source = _make_source()
    base_key = build_session_key(source)

    assert profile_scoped_session_key(base_key, "default") == base_key
    assert profile_scoped_session_key(base_key, "worker").startswith("agent:worker:")


def _bind_source(runner, source, profile="worker"):
    runner._source_agent_binding_store.set_binding(
        build_source_binding_key(source), profile,
    )
    source.profile = runner._profile_name_for_source(source)
    return source


def test_bound_profile_runtime_uses_profile_home_and_session_namespace(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root, monkeypatch)
    source = _bind_source(runner, _make_source())
    before_home = os.environ.get("HERMES_HOME")

    session_key = build_session_key(source, profile=source.profile)

    assert source.profile == "worker"
    assert runner._resolve_profile_home_for_source(source) == profile_dir
    assert session_key.startswith("agent:worker:")
    with runner._profile_scope_for_source(source):
        from hermes_constants import get_hermes_home

        assert get_hermes_home() == profile_dir
        assert runner.session_store.persistence.scope.profile_dir == profile_dir
        assert gateway_run._load_gateway_config()["model"]["default"] == "worker-model"
    assert os.environ.get("HERMES_HOME") == before_home


def test_named_profile_scope_does_not_inherit_launch_terminal_cwd(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker")
    (root / "config.yaml").write_text(
        "terminal:\n  cwd: /default/project\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", "/launch/project")
    runner = _make_runner(tmp_path, root, monkeypatch)
    source = _bind_source(runner, _make_source())

    with runner._profile_scope_for_source(source):
        from tools.terminal_scope import terminal_env

        assert terminal_env("TERMINAL_CWD") == str(Path.home())
    assert os.environ["TERMINAL_CWD"] == "/launch/project"


def test_profile_config_reload_reuses_store_and_scoped_db_handle(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker", model="first-model")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root, monkeypatch)
    source = _bind_source(runner, _make_source())
    store = runner.session_store

    with runner._profile_scope_for_source(source):
        first_config = gateway_run._load_gateway_config()
        first_db = store.persistence

    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openrouter\n  default: second-model\n",
        encoding="utf-8",
    )
    mtime = config_path.stat().st_mtime
    os.utime(config_path, (mtime + 5, mtime + 5))

    with runner._profile_scope_for_source(source):
        second_config = gateway_run._load_gateway_config()
        second_db = store.persistence

    assert first_config["model"]["default"] == "first-model"
    assert second_config["model"]["default"] == "second-model"
    assert runner.session_store is store
    assert second_db is first_db
    assert second_db.scope.profile_dir == profile_dir


def test_session_model_override_takes_priority_over_bound_profile_model(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker", model="profile-model")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root, monkeypatch)
    source = _bind_source(runner, _make_source())
    session_key = build_session_key(source, profile=source.profile)

    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "test-key",
            "base_url": "https://example.test",
            "api_mode": "chat_completions",
        },
    )
    with runner._profile_scope_for_source(source):
        user_config = gateway_run._load_gateway_config()
        model, runtime = runner._resolve_session_agent_runtime(
            source=source,
            session_key=session_key,
            user_config=user_config,
        )
        assert model == "profile-model"
        assert runtime["provider"] == "openrouter"

        runner._session_model_overrides[session_key] = {
            "model": "override-model",
            "provider": "openrouter",
        }
        model, _runtime = runner._resolve_session_agent_runtime(
            source=source,
            session_key=session_key,
            user_config=user_config,
        )
    assert model == "override-model"


@pytest.mark.asyncio
async def test_run_agent_executes_inside_bound_profile_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker", model="profile-model")
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = _make_runner(tmp_path, root, monkeypatch)
    source = _bind_source(runner, _make_source())
    session_key = build_session_key(source, profile=source.profile)
    captured = {}

    async def capture_turn(message, context_prompt, history, source, session_id, **kwargs):
        from hermes_constants import get_hermes_home

        captured["home"] = get_hermes_home()
        return {"final_response": "ok"}

    runner._run_agent_inner = capture_turn
    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-worker",
        session_key=session_key,
    )

    from hermes_constants import get_hermes_home

    assert result["final_response"] == "ok"
    assert captured["home"] == profile_dir
    assert get_hermes_home() == root
    assert os.environ["HERMES_HOME"] == str(root)
