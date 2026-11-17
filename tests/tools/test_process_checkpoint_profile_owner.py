"""A multiplexed process serves every profile from one ProcessRegistry; each profile's processes.json must list
only the processes that profile spawned (live 2026-10-15: profile iso_b's file carried iso_a's command line, cwd
and session key), while crash recovery under each served profile still re-adopts every live process once."""

import json
import shlex
import sys
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.process_registry import ProcessRegistry


def _spawn_in(registry, home: Path, tmp_path: Path, marker: str):
    token = set_hermes_home_override(home)
    try:
        cmd = shlex.join([Path(sys.executable).as_posix(), "-c", f"import time; time.sleep(60)  # {marker}"])
        return registry.spawn_local(cmd, cwd=str(tmp_path), task_id=f"task-{marker}")
    finally:
        reset_hermes_home_override(token)


def _listed(home: Path) -> set[str]:
    return {entry["session_id"] for entry in json.loads((home / "processes.json").read_text(encoding="utf-8"))}


def _recover_in(registry, home: Path) -> int:
    token = set_hermes_home_override(home)
    try:
        return registry.recover_from_checkpoint()
    finally:
        reset_hermes_home_override(token)


def test_each_profile_checkpoint_lists_only_its_own_processes(tmp_path):
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    registry = ProcessRegistry()
    a = _spawn_in(registry, home_a, tmp_path, "ISO_A")
    b = _spawn_in(registry, home_b, tmp_path, "ISO_B")  # B writes while A's process is still running
    try:
        assert _listed(home_a) == {a.id}
        assert _listed(home_b) == {b.id}
        entries_a = json.loads((home_a / "processes.json").read_text(encoding="utf-8"))
        assert entries_a[0]["checkpoint_owner"] == str(home_a / "processes.json")
        assert "ISO_A" not in (home_b / "processes.json").read_text(encoding="utf-8")

        fresh = ProcessRegistry()  # the next gateway process: recovery replays each served home under its scope
        assert _recover_in(fresh, home_a) == 1
        assert _recover_in(fresh, home_b) == 1
        assert {a.id, b.id} <= set(fresh._running)
        assert _listed(home_a) == {a.id} and _listed(home_b) == {b.id}
    finally:
        for session in (a, b):
            session.process.kill()
            session.process.wait(timeout=10)


def _foreign_entries(home: Path, other: Path) -> list[dict]:
    return [
        {"session_id": "foreign", "pid": 123, "pid_scope": "host", "command": "foreign",
         "checkpoint_owner": str(other / "processes.json")},
        {"session_id": "hosted", "pid": 124, "pid_scope": "host", "command": "hosted",
         "sandbox_key": "hosted-agent"},
    ]


def test_recovery_refuses_foreign_and_hosted_checkpoint_entries(tmp_path, monkeypatch):
    home, other = tmp_path / "profile-a", tmp_path / "profile-b"
    home.mkdir()
    (home / "processes.json").write_text(json.dumps(_foreign_entries(home, other)), encoding="utf-8")
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_args: "running")
    assert _recover_in(registry, home) == 0
    assert registry._running == {}
    assert json.loads((home / "processes.json").read_text(encoding="utf-8")) == []


def test_log_handoffs_do_not_preserve_foreign_or_hosted_entries(tmp_path, monkeypatch):
    home, other = tmp_path / "profile-a", tmp_path / "profile-b"
    log_dir = home / "logs" / "process-output"
    log_dir.mkdir(parents=True)
    entries = _foreign_entries(home, other)
    for entry in entries:
        log = log_dir / f"{entry['session_id']}.log"
        log.write_text("", encoding="utf-8")
        entry["output_log"] = str(log)
    checkpoint = home / "processes.json"
    checkpoint.write_text(json.dumps(entries), encoding="utf-8")
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_args: "running")
    token = set_hermes_home_override(home)
    try:
        assert registry._foreign_handoffs(checkpoint, set()) == []
    finally:
        reset_hermes_home_override(token)
