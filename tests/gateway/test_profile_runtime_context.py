"""Profile-scoped gateway runtime context tests."""

from __future__ import annotations

import json
import os
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import (
    SessionSource,
    SessionStore,
    build_session_key,
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


def _make_runner(tmp_path, root):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        sessions_dir=root / "sessions",
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner.session_store = SessionStore(
        root / "sessions",
        runner.config,
        state_db_path=root / "state.db",
    )
    runner._session_db = runner.session_store._db
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


def test_bound_profile_runtime_context_uses_profile_paths(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root)
    source = _make_source()
    source_key = runner._agent_source_binding_key(source)
    runner._source_agent_binding_store.set_binding(source_key, "worker")
    before_home = os.environ.get("HERMES_HOME")

    context = runner._resolve_profile_runtime_context(source)

    assert os.environ.get("HERMES_HOME") == before_home
    assert context.profile_name == "worker"
    assert context.agent_id == "worker"
    assert context.profile_home == profile_dir
    assert context.session_key.startswith("agent:worker:")
    assert context.session_store.sessions_dir == profile_dir / "sessions"
    assert context.session_db.db_path == profile_dir / "state.db"
    assert context.config["model"]["default"] == "worker-model"
    assert context.workspace_cwd == profile_dir / "workspace"


def test_named_profile_workspace_ignores_cloned_global_terminal_cwd(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker")
    (profile_dir / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: worker-model\n"
        "terminal:\n  cwd: /shared/project\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root)
    source = _make_source()
    runner._source_agent_binding_store.set_binding(
        runner._agent_source_binding_key(source),
        "worker",
    )

    context = runner._resolve_profile_runtime_context(source)

    assert context.workspace_cwd == profile_dir / "workspace"


def test_profile_runtime_context_config_cache_invalidates_without_rebuilding_store(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker", model="first-model")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root)

    first = runner._get_profile_runtime_components("worker")
    store = first["session_store"]
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openrouter\n  default: second-model\n",
        encoding="utf-8",
    )
    os.utime(config_path, (first["config_mtime"] + 5, first["config_mtime"] + 5))

    second = runner._get_profile_runtime_components("worker")

    assert second["config"]["model"]["default"] == "second-model"
    assert second["session_store"] is store


def test_session_model_override_takes_priority_over_bound_profile_model(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "hermes-home"
    root.mkdir()
    _seed_profile(root, "worker", model="profile-model")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path, root)
    source = _make_source()
    source_key = runner._agent_source_binding_key(source)
    runner._source_agent_binding_store.set_binding(source_key, "worker")
    context = runner._resolve_profile_runtime_context(source)

    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "test-key",
            "base_url": "https://example.test",
            "api_mode": "chat_completions",
        },
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=context.session_key,
        user_config=context.config,
    )
    assert model == "profile-model"
    assert runtime["provider"] == "openrouter"

    runner._session_model_overrides[context.session_key] = {
        "model": "override-model",
        "provider": "openrouter",
    }
    model, _runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=context.session_key,
        user_config=context.config,
    )
    assert model == "override-model"


@pytest.mark.asyncio
async def test_run_agent_executes_inside_bound_profile_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    profile_dir = _seed_profile(root, "worker", model="profile-model")
    (profile_dir / "workspace").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    class CapturingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.tools = []

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            from hermes_constants import get_hermes_home
            from tools.terminal_tool import _task_env_overrides

            return {
                "final_response": json.dumps(
                    {
                        "home": str(get_hermes_home()),
                        "workspace_override": _task_env_overrides.get(task_id),
                    }
                ),
                "messages": [],
                "api_calls": 1,
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda *args, **kwargs: {
            "provider": "openrouter",
            "api_key": "test-key",
            "base_url": "https://example.test",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._draining = False
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    source = _make_source()
    runtime_context = SimpleNamespace(
        profile_name="worker",
        agent_id="worker",
        profile_home=profile_dir,
        config={
            "model": {"provider": "openrouter", "default": "profile-model"},
            "display": {"platforms": {"dingtalk": {"tool_progress": "off"}}},
        },
        session_store=None,
        session_db=None,
        session_key="agent:worker:dingtalk:group:chat-1:user-1",
        source_binding_key="source:dingtalk:group:chat-1:user-1",
        workspace_cwd=profile_dir / "workspace",
        binding=None,
        config_mtime=0,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-worker",
        session_key=runtime_context.session_key,
        runtime_context=runtime_context,
    )

    payload = json.loads(result["final_response"])
    assert payload["home"] == str(profile_dir)
    assert payload["workspace_override"] == {"cwd": str(profile_dir / "workspace")}

    from tools.terminal_tool import _task_env_overrides

    assert "sess-worker" not in _task_env_overrides
