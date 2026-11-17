"""Subprocess coverage for the slash worker's live model route.

Unlike the constructor-level regression tests, these tests spawn the real
``_SlashWorker`` and inspect its OS argv. Commands execute in a real ``HermesCLI``;
no provider request is made. The successful Ultrafast route uses the offline
first-party service gate, while the restarted route uses a loopback URL that the
same gate rejects before any HTTP request.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import psutil

from tests.waiting import wait_until
from tui_gateway import server

CODEX_URL = "https://chatgpt.com/backend-api/codex"
LOOPBACK_URL = "http://127.0.0.1:9/v1"


def _worker_home(tmp_path, monkeypatch):
    home = tmp_path / "worker-home"
    home.mkdir()
    # A default route incompatible with Ultrafast makes success dependent on the
    # live route passed to the child, rather than config.yaml accidentally winning.
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: claude-sonnet-5.5\n"
        "  provider: claude-subscription\n"
        "  base_url: process://claude-subscription\n"
        "display:\n"
        "  language: en\n",
        encoding="utf-8",
    )
    # A child process is outside the test suite's network fence. Leave it with no
    # provider credentials, so the Codex account-catalog gate stays offline too.
    for key in list(os.environ):
        if key.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server, "_SLASH_WORKER_TIMEOUT_S", 120)
    return home


def _install_session(monkeypatch, home, *, model, provider, base_url):
    session = {
        "session_key": "slash-worker-subprocess-route",
        "agent": SimpleNamespace(model=model, provider=provider, base_url=base_url),
        "profile_home": str(home),
        "running": False,
    }
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    # The parent-agent mirror is independent of the subprocess route under test.
    monkeypatch.setattr(server, "_mirror_slash_side_effects", lambda *args: "")
    return session


def _slash(command: str) -> str:
    response = server._methods["slash.exec"](
        1, {"session_id": "sid", "command": command}
    )
    assert "error" not in response, response
    return response["result"]["output"]


def _os_argv(worker) -> list[str]:
    return psutil.Process(worker.proc.pid).cmdline()


def _option(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def _close_worker(session) -> None:
    if worker := session.get("slash_worker"):
        worker.close()


def test_slash_exec_spawns_real_worker_with_live_route_and_runs_real_cli(
    tmp_path, monkeypatch
):
    home = _worker_home(tmp_path, monkeypatch)
    session = _install_session(
        monkeypatch,
        home,
        model="gpt-6-astra",
        provider="openai-codex",
        base_url=CODEX_URL,
    )
    try:
        output = _slash("/fast ultrafast")

        worker = session["slash_worker"]
        argv = _os_argv(worker)
        assert _option(argv, "--model") == "gpt-6-astra"
        assert _option(argv, "--provider") == "openai-codex"
        assert _option(argv, "--base-url") == CODEX_URL
        assert "ultrafast" in output.lower()
        assert "isn't available" not in output.lower()
        # A second real CLI dispatch observes the state set by the first one.
        assert "ultrafast" in _slash("/fast status").lower()
    finally:
        _close_worker(session)


def test_real_worker_omits_empty_route_options(tmp_path, monkeypatch):
    home = _worker_home(tmp_path, monkeypatch)
    worker = server._SlashWorker(
        "slash-worker-empty-route", "", profile_home=str(home), provider="", base_url=""
    )
    try:
        argv = _os_argv(worker)
        assert "--model" not in argv
        assert "--provider" not in argv
        assert "--base-url" not in argv
        assert worker.run("/version")
    finally:
        worker.close()


def test_restart_closes_old_real_worker_and_uses_new_route(tmp_path, monkeypatch):
    home = _worker_home(tmp_path, monkeypatch)
    session = _install_session(
        monkeypatch,
        home,
        model="gpt-6-astra",
        provider="openai-codex",
        base_url=CODEX_URL,
    )
    try:
        assert _slash("/version")
        old_worker = session["slash_worker"]
        old_pid = old_worker.proc.pid

        # This is the post-switch live-agent state consumed by the same restart
        # helper called by model_switch.py after model/provider/base_url changes.
        session["agent"] = SimpleNamespace(
            model="gpt-6.1-sol", provider="openai", base_url=LOOPBACK_URL
        )
        server._restart_slash_worker("sid", session)

        wait_until(
            lambda: old_worker.proc.poll() is not None,
            timeout=10,
            what="the old slash worker to exit after restart",
        )
        new_worker = session["slash_worker"]
        assert new_worker is not old_worker
        assert new_worker.proc.pid != old_pid
        argv = _os_argv(new_worker)
        assert _option(argv, "--model") == "gpt-6.1-sol"
        assert _option(argv, "--provider") == "openai"
        assert _option(argv, "--base-url") == LOOPBACK_URL

        # The new endpoint reaches HermesCLI's real route gate. A stale worker on
        # CODEX_URL would accept Ultrafast; the loopback route must refuse it.
        output = _slash("/fast ultrafast")
        assert "isn't available" in output.lower()
        assert "gpt-6.1-sol" in output
    finally:
        _close_worker(session)
