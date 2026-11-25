"""Plugins can tell which cron execution they run inside (#130722)."""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home

_PLUGIN = '''
import dataclasses, json, os
from pathlib import Path

CTX = None

def _record(**kwargs):
    ident = CTX.current_cron_execution()
    out = Path(os.environ["HERMES_HOME"]) / "seen.json"
    out.write_text(json.dumps({"tool": kwargs.get("tool_name"),
                               "identity": dataclasses.asdict(ident) if ident else None}))

def register(ctx):
    global CTX
    CTX = ctx
    ctx.register_hook("pre_tool_call", _record)
'''

_TICK = '''
import json, sys
from cron import scheduler, scheduler_authority
from cron.executions import list_executions
from tests.cron.conftest import owner_execution_runner
# Cron executes inside the profile's owner; run the owner's execution seam in-process.
scheduler_authority.run_canonical_job = owner_execution_runner()
from hermes_cli.plugins import get_plugin_manager
scheduler.tick(verbose=False, sync=True)
ctx = get_plugin_manager()._plugins["cron-identity-probe"].module.CTX
print(json.dumps({"outside": ctx.current_cron_execution(), "profile": ctx.profile_name,
                  "executions": list_executions(job_id="probe")}, default=str))
'''

_HOSTED_DELIVERY_PLUGIN = '''
import json, os
from pathlib import Path


def register(ctx):
    def record(tool_name=None, **kwargs):
        if tool_name != "cronjob_manage":
            return
        run = ctx.current_cron_execution()
        seen = {
            "identity": dict(run) if run is not None else None,
            "firing": ctx.cron_delivery_platforms(),
            "stored": ctx.cron_delivery_platforms(run.job_id) if run is not None else None,
            "result": json.loads(kwargs.get("result") or "{}"),
        }
        Path(os.environ["HERMES_HOME"], "hosted-seen.json").write_text(json.dumps(seen))
    ctx.register_hook("post_tool_call", record)
'''

_HOSTED_TICK = '''
import json
from cron import scheduler, scheduler_authority
from cron.executions import list_executions
from tests.cron.conftest import owner_execution_runner
scheduler_authority.run_canonical_job = owner_execution_runner()
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
try:
    scheduler.tick(verbose=False, sync=True)
    print(json.dumps({"executions": list_executions(job_id="probe")}, default=str))
finally:
    manager.unload()
    manager._plugin_host().shutdown()
'''


def test_tool_hook_in_a_ticked_job_sees_its_execution_and_none_outside(tmp_path):
    from hermes_time import now

    home = tmp_path / "home"
    plugin_dir = home / "plugins" / "cron-identity-probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: cron-identity-probe\nversion: 0.0.1\n")
    (plugin_dir / "__init__.py").write_text(_PLUGIN)
    note = home / "note.txt"
    with FakeLLMServer([ToolCall("read_file", {"path": str(note)}), Text("done")]) as srv:
        write_hermes_home(home, srv.base_url,
                          extra_config="plugins:\n  enabled: [cron-identity-probe]\n")
        note.write_text("hello\n")
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": "probe", "name": "identity probe", "prompt": "read the note",
            "schedule": {"kind": "interval", "minutes": 240},
            "next_run_at": (now() - timedelta(minutes=1)).isoformat(),
            "enabled": True, "state": "scheduled", "deliver": "local",
            "repeat": {"times": None, "completed": 0}}]}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERMES_", "_HERMES_"))
               and not k.endswith(("_API_KEY", "_TOKEN"))}
        env.update(HERMES_HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run([sys.executable, "-c", _TICK], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    seen = json.loads((home / "seen.json").read_text())
    (execution,) = report["executions"]

    assert seen["tool"] == "read_file"
    ident = seen["identity"]
    assert ident is not None, "pre_tool_call fired inside a cron run saw no cron identity"
    assert ident.keys() == {
        "job_id", "job_name", "execution_id", "source", "scheduled_instant", "started_at", "profile",
    }
    assert ident["job_id"] == "probe"
    assert ident["job_name"] == "identity probe"
    assert ident["execution_id"] == execution["id"]
    assert ident["source"] == execution["source"] == "builtin"
    assert ident["scheduled_instant"] == execution["scheduled_instant"] is not None
    assert ident["started_at"] == execution["started_at"]
    assert ident["profile"] == report["profile"]
    assert report["outside"] is None


def test_hosted_hook_keeps_firing_delivery_after_ticked_job_edits_itself(tmp_path):
    from hermes_time import now

    home = tmp_path / "home"
    plugin_dir = home / "plugins" / "hosted-cron-snapshot"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: hosted-cron-snapshot\nversion: 0.0.1\n")
    (plugin_dir / "__init__.py").write_text(_HOSTED_DELIVERY_PLUGIN)
    with FakeLLMServer([
        ToolCall("cronjob_manage", {"action": "update", "job_id": "probe", "deliver": "local"}),
        Text("done"),
    ]) as srv:
        write_hermes_home(
            home, srv.base_url,
            extra_config=(
                "cron:\n  allow_agent_scheduling: true\n"
                "plugins:\n  enabled: [hosted-cron-snapshot]\n  isolation: host\n"
            ),
        )
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": "probe", "name": "hosted identity probe", "prompt": "change your delivery",
            "schedule": {"kind": "interval", "minutes": 240},
            "next_run_at": (now() - timedelta(minutes=1)).isoformat(),
            "enabled": True, "state": "scheduled", "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "42"},
            "repeat": {"times": None, "completed": 0}}]}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERMES_", "_HERMES_"))
               and not k.endswith(("_API_KEY", "_TOKEN"))}
        env.update(HERMES_HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run([sys.executable, "-c", _HOSTED_TICK], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    seen = json.loads((home / "hosted-seen.json").read_text())
    (execution,) = report["executions"]
    stored = json.loads((home / "cron" / "jobs.json").read_text())["jobs"]

    assert seen["result"]["success"] is True
    assert seen["identity"]["job_id"] == "probe"
    assert seen["identity"]["execution_id"] == execution["id"]
    assert seen["identity"]["source"] == execution["source"] == "builtin"
    assert seen["firing"] == ["telegram"]
    assert seen["stored"] == []
    assert [job["deliver"] for job in stored] == ["local"]


def test_identity_is_scoped_and_not_inherited_by_delegated_children():
    from agent.delegation_context import delegated_child_context
    from cron.execution_identity import (
        _current_cron_execution_job, current_cron_execution, enter_cron_execution,
        exit_cron_execution,
    )

    record = {"source": "builtin", "scheduled_instant": None, "started_at": "2026-10-04T00:00:00"}
    assert current_cron_execution() is None
    assert _current_cron_execution_job() is None
    token = enter_cron_execution({"id": "j", "name": "J"}, "exec-1", record)
    try:
        identity = current_cron_execution()
        assert identity is not None and identity.execution_id == "exec-1"
        delivery_job = _current_cron_execution_job()
        assert delivery_job == {"id": "j", "name": "J"}
        with delegated_child_context("child"):
            assert current_cron_execution() is None
            assert _current_cron_execution_job() is None
        assert current_cron_execution() is identity
        assert _current_cron_execution_job() is delivery_job
    finally:
        exit_cron_execution(token)
    assert current_cron_execution() is None
    assert _current_cron_execution_job() is None


def test_firing_job_uses_the_delivery_resolvers_snapshot(monkeypatch):
    from cron import scheduler_delivery
    from cron.execution_identity import (
        _current_cron_execution_job, enter_cron_execution, exit_cron_execution,
    )

    snapshot = {"id": "j", "name": "J", "deliver": "local"}
    monkeypatch.setattr(scheduler_delivery, "_snapshot_delivery_job", lambda job: snapshot)
    token = enter_cron_execution({"id": "j", "name": "J"}, "exec-1", {})
    try:
        assert _current_cron_execution_job() is snapshot
    finally:
        exit_cron_execution(token)


def test_firing_job_snapshot_preserves_normal_routing_without_failure_delivery():
    from cron.execution_identity import (
        _current_cron_execution_job, enter_cron_execution, exit_cron_execution,
    )
    from cron.scheduler_delivery import _resolve_delivery_targets

    job = {
        "id": "j", "deliver": ["origin"], "failure_deliver": "local",
        "origin": {"platform": "telegram", "chat_id": "42", "thread_id": "7"},
    }
    expected = _resolve_delivery_targets(job, for_failure=False)
    assert expected and expected[0]["chat_id"] == "42"
    assert _resolve_delivery_targets(job, for_failure=True) == []
    token = enter_cron_execution(job, "exec-1", {})
    try:
        job["deliver"].clear()
        job["origin"]["chat_id"] = "99"
        snapshot = _current_cron_execution_job()
        assert snapshot is not None and "failure_deliver" not in snapshot
        assert _resolve_delivery_targets(snapshot, for_failure=False) == expected
    finally:
        exit_cron_execution(token)
