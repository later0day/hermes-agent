"""Tests for gateway liveness resolution and strict process identity."""

import json
from pathlib import Path

import pytest

from gateway import status


class TestResolveGatewayLiveness:
    """The single liveness ladder both dashboard status surfaces share.

    Before this existed, /api/status and /api/messaging/platforms each
    open-coded their own ladder and disagreed on the same page load — the
    sidebar read "running" while the Channels page rendered "The gateway is
    not running." These pin the ladder's ordering and its fail-open signal.
    """

    def test_pid_rung_wins_and_skips_later_rungs(self):
        calls = {"health": 0, "runtime_pid": 0}

        def _health():
            calls["health"] += 1
            return True, {"pid": 999}

        def _runtime_pid(runtime, **kw):
            calls["runtime_pid"] += 1
            return 888

        result = status.resolve_gateway_liveness(
            runtime=None,
            health_probe=_health,
            pid_probe=lambda *a, **k: 4242,
            runtime_pid_probe=_runtime_pid,
        )

        assert result.running is True
        assert result.pid == 4242
        assert result.source == "pid"
        # The authoritative rung answered: no lower rung should have run.
        assert calls == {"health": 0, "runtime_pid": 0}


    def test_probe_exception_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        """A raising rung must fall through, never propagate.

        Status endpoints poll this constantly; an exotic /proc or a
        permissions error must not turn into a 500.
        """
        # Empty rendezvous dir: no host gateway owns the role, so the multiplexer rung stays quiet.
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))

        def _boom(*a, **k):
            raise RuntimeError("probe exploded")

        result = status.resolve_gateway_liveness(
            runtime=None,
            health_probe=None,
            pid_probe=_boom,
            runtime_pid_probe=lambda *a, **k: None,
        )

        assert result.running is False
        # probe_error distinguishes "down" from "couldn't tell" for callers
        # that must fail OPEN (the kanban dispatcher warning).
        assert result.probe_error is True


    def test_profile_dir_scopes_every_read_to_that_profile(self, tmp_path):
        """Gateway identity files live in the per-profile home.

        The status readers resolve process-level paths and deliberately
        ignore the HERMES_HOME contextvar override (#56986), so the profile
        directory must be threaded through explicitly or a scoped request
        silently reports a DIFFERENT profile's gateway (#71211).
        """
        profile_dir = tmp_path / "profiles" / "worker"
        profile_dir.mkdir(parents=True)
        seen = {}

        def _pid(pid_path=None, **kw):
            seen["pid_path"] = pid_path

        def _reader(path=None):
            seen["status_path"] = path

        def _runtime_pid(runtime, *, expected_home=None):
            seen["expected_home"] = expected_home

        status.resolve_gateway_liveness(
            profile_dir=profile_dir,
            health_probe=None,
            pid_probe=_pid,
            runtime_reader=_reader,
            runtime_pid_probe=_runtime_pid,
        )

        assert seen["pid_path"] == profile_dir / "gateway.pid"
        assert seen["status_path"] == profile_dir / "gateway_state.json"
        # expected_home is what stops a recycled PID belonging to another
        # profile's live gateway from being reported as this profile's.
        assert seen["expected_home"] == profile_dir


def test_strict_gateway_identity_returns_none_for_confirmed_absence(tmp_path):
    assert status.get_running_pid_identity_strict(tmp_path / "gateway.pid") is None


def test_strict_gateway_identity_raises_when_metadata_stat_is_denied(
    tmp_path, monkeypatch
):
    pid_path = tmp_path / "gateway.pid"
    original_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self == pid_path:
            raise PermissionError("denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)

    with pytest.raises(RuntimeError, match="not inspectable"):
        status.get_running_pid_identity_strict(pid_path)


def test_strict_gateway_identity_raises_on_malformed_active_metadata(
    tmp_path, monkeypatch
):
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    pid_path.write_text("bad", encoding="utf-8")
    lock_path.write_text("bad", encoding="utf-8")
    monkeypatch.setattr(status, "_get_gateway_lock_path", lambda _path=None: lock_path)
    monkeypatch.setattr(status, "_is_gateway_runtime_lock_active_strict", lambda _path=None: True)

    with pytest.raises(RuntimeError, match="malformed"):
        status.get_running_pid_identity_strict(pid_path)


def test_strict_gateway_identity_rejects_reused_pid(tmp_path, monkeypatch):
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    record = {"pid": 123, "start_time": 10.0, "kind": "hermes-gateway"}
    pid_path.write_text(json.dumps(record), encoding="utf-8")
    lock_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(status, "_get_gateway_lock_path", lambda _path=None: lock_path)
    monkeypatch.setattr(status, "_is_gateway_runtime_lock_active_strict", lambda _path=None: True)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 20.0)

    with pytest.raises(RuntimeError, match="identity changed"):
        status.get_running_pid_identity_strict(pid_path)


def test_strict_gateway_identity_adopts_held_lock_when_pid_file_is_gone(tmp_path, monkeypatch):
    """A held lock whose record validates against the live process IS the gateway's identity: a
    --replace relaunch (or a launch-service gateway, #110166) leaves gateway.lock without
    gateway.pid, and raising there aborted every following `hermes update` (#123430)."""
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    record = {"pid": 123, "start_time": 10.0, "kind": "hermes-gateway",
              "argv": ["python", "-m", "hermes_cli.main", "gateway", "run", "--replace"]}
    lock_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(status, "_get_gateway_lock_path", lambda _path=None: lock_path)
    monkeypatch.setattr(status, "_is_gateway_runtime_lock_active_strict", lambda _path=None: True)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 10.0)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda _pid: "python -m hermes_cli.main gateway run --replace")
    monkeypatch.setattr(status, "_IS_WINDOWS", False)

    assert status.get_running_pid_identity_strict(pid_path) == (123, 10.0)
    # The lock alone is still held to the full proof: a recycled PID is refused, not adopted.
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 20.0)
    with pytest.raises(RuntimeError, match="identity changed"):
        status.get_running_pid_identity_strict(pid_path)


def test_pid_record_names_the_entry_point_under_an_inline_launcher(tmp_path, monkeypatch):
    """The published launcher script runs `python -I -c <script> gateway run`, so sys.argv[0] is
    "-c"; the persisted record must still read as a gateway when the live cmdline is unreadable
    (#124029, #123151)."""
    import types

    entry = types.SimpleNamespace(__file__="/opt/Hermes Agent/hermes-agent/hermes_cli/main.py")
    monkeypatch.setitem(status.sys.modules, "hermes_cli.main", entry)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status.sys, "argv", ["-c", "gateway", "run"])
    record = status._build_pid_record()
    assert record["argv"] == [entry.__file__, "gateway", "run"]
    assert status._record_looks_like_gateway(record)


def test_retained_gateway_state_keeps_watchdog_degraded_like_startup_failed():
    """A watchdog-stamped ``degraded`` of a dead process is a current failure under the same rule as
    ``startup_failed`` (#113372): kept while the operator wants the gateway running, ``stopped`` once
    ``hermes gateway stop`` records the intent. The startup-time ``degraded`` (retryable platforms, no
    watchdog exit_reason) of a dead process is just ``stopped``."""
    watchdog = {"gateway_state": "degraded", "exit_reason": "loop_liveness_watchdog"}
    assert status.retained_gateway_state(watchdog) == "degraded"
    assert status.retained_gateway_state({**watchdog, "exit_reason": "shutdown_watchdog"}) == "degraded"
    assert status.retained_gateway_state({**watchdog, "desired_state": "stopped"}) == "stopped"
    assert status.retained_gateway_state({"gateway_state": "degraded", "exit_reason": None}) == "stopped"
    assert status.retained_gateway_state({"gateway_state": "startup_failed", "exit_reason": "x"}) == "startup_failed"
