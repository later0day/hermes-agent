"""Interface-level acceptance flows for multi-agent profiles."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import quote

import pytest


@pytest.fixture()
def multi_agent_interface_env(tmp_path, monkeypatch):
    from hermes_cli import profiles

    root = tmp_path / "hermes-home"
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "sessions").mkdir()
    (root / "skills").mkdir()
    (root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: nous/default\n"
        "gateway:\n  multiplex_profiles: true\n",
        encoding="utf-8",
    )
    (root / "SOUL.md").write_text("Default identity\n", encoding="utf-8")
    (root / ".env").write_text("ANTHROPIC_API_KEY=test-worker-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: root / "profiles")
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _name: "skip wrapper")
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda _name: None)

    return {"root": root, "bindings_db": root / "gateway_source_agent_bindings.sqlite"}


@pytest.fixture()
def dashboard_client(multi_agent_interface_env):
    from starlette.testclient import TestClient

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_dashboard_rest_full_multi_agent_flow(dashboard_client, multi_agent_interface_env):
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session

    root = multi_agent_interface_env["root"]

    create_profile = dashboard_client.post(
        "/api/profiles",
        json={
            "name": "worker",
            "clone_from_default": True,
            "no_skills": False,
        },
    )
    assert create_profile.status_code == 200, create_profile.text
    worker_home = root / "profiles" / "worker"
    assert worker_home.is_dir()
    assert not any((worker_home / "skills").rglob("SKILL.md"))

    profiles = dashboard_client.get("/api/profiles")
    assert profiles.status_code == 200, profiles.text
    assert "worker" in {p["name"] for p in profiles.json()["profiles"]}

    set_model = dashboard_client.post(
        "/api/profiles/worker/model",
        json={"provider": "anthropic", "model": "claude-worker"},
    )
    assert set_model.status_code == 200, set_model.text
    assert "claude-worker" in (worker_home / "config.yaml").read_text(encoding="utf-8")

    source_key = "source:dingtalk:group:chat-1:user-1"
    bind = dashboard_client.post(
        "/api/source-bindings",
        json={"source_binding_key": source_key, "profile_name": "worker"},
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["binding"]["profile_name"] == "worker"

    with open_persistence(profile_scope(home=root)) as store:
        seed_session(
            store,
            "agent:worker:dingtalk:group:chat-1:user-1",
            "dingtalk",
            model="claude-worker",
        )

    sessions = dashboard_client.get("/api/sessions?profile=default&limit=10&offset=0")
    assert sessions.status_code == 200, sessions.text
    session = sessions.json()["sessions"][0]
    assert session["source_binding_key"] == source_key
    assert session["session_profile"] == "worker"
    assert session["bound_profile"] == "worker"

    worker_cron = dashboard_client.post(
        "/api/cron/jobs?profile=worker",
        json={
            "prompt": "worker owned scheduled task",
            "schedule": "every 1h",
            "name": "worker-owned",
            "deliver": "local",
        },
    )
    assert worker_cron.status_code == 200, worker_cron.text
    worker_cron_job = worker_cron.json()
    assert worker_cron_job["owner_profile"] == "worker"
    assert worker_cron_job["run_profile"] == "worker"

    default_owned_worker_run = dashboard_client.post(
        "/api/cron/jobs?profile=default",
        json={
            "prompt": "default owned task executed by worker",
            "schedule": "every 2h",
            "name": "default-owned-worker-run",
            "deliver": "local",
            "run_profile": "worker",
        },
    )
    assert default_owned_worker_run.status_code == 200, default_owned_worker_run.text
    split_job = default_owned_worker_run.json()
    assert split_job["owner_profile"] == "default"
    assert split_job["run_profile"] == "worker"

    worker_jobs = dashboard_client.get("/api/cron/jobs?profile=worker").json()
    default_jobs = dashboard_client.get("/api/cron/jobs?profile=default").json()
    assert [job["id"] for job in worker_jobs] == [worker_cron_job["id"]]
    assert [job["id"] for job in default_jobs] == [split_job["id"]]
    worker_stored = json.loads((worker_home / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    default_stored = json.loads((root / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert [job["id"] for job in worker_stored] == [worker_cron_job["id"]]
    assert [job["id"] for job in default_stored] == [split_job["id"]]
    assert worker_stored[0]["owner_profile"] == "worker"
    assert default_stored[0]["run_profile"] == "worker"

    details = dashboard_client.get("/api/profiles/worker/details")
    assert details.status_code == 200, details.text
    payload = details.json()
    assert payload["model"] == {"provider": "anthropic", "model": "claude-worker"}
    assert payload["bindings"][0]["source_binding_key"] == source_key
    assert payload["cron"]["owner_job_count"] == 1
    assert payload["paths"]["workspace"].endswith("profiles/worker/workspace")

    clear = dashboard_client.delete(f"/api/source-bindings/{quote(source_key, safe='')}")
    assert clear.status_code == 200, clear.text
    assert clear.json() == {"ok": True, "deleted": True}
    sessions_after_clear = dashboard_client.get("/api/sessions?profile=default&limit=10&offset=0").json()
    assert sessions_after_clear["sessions"][0]["bound_profile"] == "default"


def _make_runner(tmp_path):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.source_agent_binding import SourceAgentBindingStore

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=True,
        platforms={Platform.DINGTALK: {"group_allow_admin_from": ["user-1"]}},
    )
    runner._source_agent_binding_store = SourceAgentBindingStore(
        tmp_path / "gateway-bindings.sqlite"
    )
    runner._agent_audit_path = tmp_path / "agent-audit.jsonl"
    runner._agent_delete_code_factory = lambda: "ABC123"
    runner._kanban_notifier_profile = "default"
    runner.adapters = {}
    return runner


def _gateway_event(text: str, raw_message=None):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.DINGTALK,
            chat_id="chat-1",
            chat_type="group",
            user_id="user-1",
            user_name="Alice",
            thread_id="topic-1",
        ),
        raw_message=raw_message,
    )


@pytest.mark.asyncio
async def test_gateway_im_command_full_multi_agent_flow(tmp_path, monkeypatch):
    from gateway.session import build_source_binding_key
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    root = tmp_path / "hermes-home"
    root.mkdir()
    (root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: nous/default\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (root / "SOUL.md").write_text("Default identity\n", encoding="utf-8")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("name: demo\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = _make_runner(tmp_path)

    create = await runner._handle_agent_command(
        _gateway_event("/agent create worker --description Handles IM jobs")
    )
    worker_home = root / "profiles" / "worker"
    assert "Created agent profile `worker`" in create
    assert (worker_home / "config.yaml").exists()
    assert not (worker_home / "skills" / "demo" / "SKILL.md").exists()
    worker_env = (worker_home / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=secret" not in worker_env

    use = await runner._handle_agent_command(_gateway_event("/agent use worker"))
    assert "Bound this chat to agent `worker`" in use

    raw = SimpleNamespace(
        session_webhook="https://api.dingtalk.com/robot/sendBySession?session=abc",
        session_webhook_expired_time=9999999999999,
    )
    webhook = await runner._handle_agent_command(
        _gateway_event("/agent webhook", raw_message=raw)
    )
    assert "Stored DingTalk fallback webhook" in webhook

    status = await runner._handle_agent_command(_gateway_event("/agent status"))
    assert "Profile: `worker`" in status
    assert "DingTalk fallback webhook: present" in status

    delegate = await runner._handle_kanban_command(
        _gateway_event(
            '/kanban create "summarize the escalation" '
            '--body "summarize the escalation" --assignee worker'
        )
    )
    task_match = re.search(r"\bCreated\s+(t_[0-9a-f]+)\b", delegate)
    assert task_match, delegate
    task_id = task_match.group(1)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)
    assert task is not None
    assert task.assignee == "worker"
    assert task.body == "summarize the escalation"
    assert subs[0]["platform"] == "dingtalk"
    assert subs[0]["chat_id"] == "chat-1"

    source_key = build_source_binding_key(_gateway_event("/agent status").source)
    binding = runner._source_agent_binding_store.get_binding(source_key)
    assert binding.profile_name == "worker"
    assert binding.fallback_extra["session_webhook"].startswith("https://api.dingtalk.com/")

    delete_request = await runner._handle_agent_command(_gateway_event("/agent delete worker"))
    assert "/agent delete worker ABC123" in delete_request
    delete_confirm = await runner._handle_agent_command(
        _gateway_event("/agent delete worker ABC123")
    )
    assert "Deleted agent profile `worker`" in delete_confirm
    assert not worker_home.exists()
    assert runner._source_agent_binding_store.get_binding(source_key) is None

    audit_actions = [
        json.loads(line)["action"]
        for line in (tmp_path / "agent-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audit_actions == [
        "agent.create",
        "agent.use",
        "agent.delete.request",
        "agent.delete",
    ]
