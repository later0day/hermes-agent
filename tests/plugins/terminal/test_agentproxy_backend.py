"""AgentProxy terminal backend plugin, driven end to end.

The only stand-in is the AgentProxy Dashboard: a local HTTP server speaking the same
``POST /api/tasks/run`` + SSE protocol that runs the agent-side shell command it receives, with a
``docker`` shim on the configured PATH prefix that turns ``docker exec <c> bash -c …`` into a local
bash run. Everything else is the real chain: bundled plugin discovery, ``TERMINAL_ENV=agentproxy``
resolved through the terminal environment registry, the terminal tool, and AgentProxyEnvironment's
exit-code sentinel and cwd tracking.
"""

from __future__ import annotations

import json
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

TOKEN = "ap-test-token"

DOCKER_SHIM = """#!/bin/bash
# Container cwd /root maps to a scratch dir on the test host.
case "$1" in
  inspect) echo true ;;
  rm|run) exit 0 ;;
  exec) shift 2; last="${@: -1}"; set -- "${@:1:$#-1}" "${last//\\/root/$FAKE_ROOT}"; exec "$@" ;;
esac
"""


@pytest.fixture
def fake_dashboard(tmp_path):
    fake_root = tmp_path / "container-root"
    (fake_root / "sub").mkdir(parents=True)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "docker"
    shim.write_text(DOCKER_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    seen = {"auth": set(), "agents": set()}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["auth"].add(self.headers.get("Authorization"))
            seen["agents"].add(body["agent_id"])
            run = subprocess.run(["bash", "-c", body["prompt"]], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin", "FAKE_ROOT": str(fake_root)}, stdin=subprocess.DEVNULL)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in ({"content": run.stdout + run.stderr}, {"success": True}):
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "shim_dir": str(shim_dir),
           "fake_root": fake_root, "seen": seen}
    server.shutdown()


@pytest.fixture
def agentproxy_selected(fake_dashboard, monkeypatch):
    from hermes_cli.plugins import _ensure_plugins_discovered

    monkeypatch.setenv("TERMINAL_ENV", "agentproxy")
    monkeypatch.setenv("TERMINAL_AP_CLOUD_URL", fake_dashboard["url"])
    monkeypatch.setenv("TERMINAL_AP_PATH_PREFIX", fake_dashboard["shim_dir"])
    monkeypatch.setenv("TERMINAL_AP_AGENT", "edge-1")
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    _ensure_plugins_discovered(force=True)
    yield fake_dashboard
    from tools.terminal_tool_lifecycle import cleanup_all_environments

    cleanup_all_environments()


def test_terminal_tool_runs_through_the_agentproxy_plugin(agentproxy_selected):
    from tools.terminal_tool import terminal_tool

    ok = json.loads(terminal_tool("cd sub && pwd", task_id="ap-e2e"))
    assert ok["exit_code"] == 0
    assert ok["output"].strip() == str(agentproxy_selected["fake_root"] / "sub")

    # cwd carries over between one-shot task API calls; the remote exit code comes back intact.
    again = json.loads(terminal_tool("pwd; exit 3", task_id="ap-e2e"))
    assert again["output"].strip() == str(agentproxy_selected["fake_root"] / "sub")
    assert again["exit_code"] == 3

    assert agentproxy_selected["seen"]["auth"] == {f"Bearer {TOKEN}"}
    assert agentproxy_selected["seen"]["agents"] == {"edge-1"}


def test_dashboard_token_never_reaches_agent_subprocesses(agentproxy_selected):
    from agent.terminal_env_registry import plugin_strip_env_keys

    assert "DASHBOARD_TOKEN" in plugin_strip_env_keys()


def test_each_profile_reaches_agentproxy_with_its_own_token_and_agent(fake_dashboard, tmp_path, monkeypatch):
    """One multiplexed process serving two profiles, A→B→A: every call carries the running
    profile's own DASHBOARD_TOKEN and agent, never the other profile's or the launch env's."""
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from gateway.run import _profile_runtime_scope
    from hermes_cli.plugins import discover_plugins
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_lifecycle import cleanup_all_environments

    for var in ("TERMINAL_ENV", "DASHBOARD_TOKEN", "TERMINAL_AP_AGENT"):
        monkeypatch.delenv(var, raising=False)
    homes = {}
    for name in ("alpha", "beta"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("terminal:\n  backend: agentproxy\n")
        (home / ".env").write_text(
            f"DASHBOARD_TOKEN=token-{name}\nTERMINAL_AP_AGENT=agent-{name}\n"
            f"TERMINAL_AP_CLOUD_URL={fake_dashboard['url']}\nTERMINAL_AP_PATH_PREFIX={fake_dashboard['shim_dir']}\n")
        homes[name] = home

    token = set_multiplex_context(True)
    try:
        for name in ("alpha", "beta", "alpha"):
            fake_dashboard["seen"]["auth"].clear()
            fake_dashboard["seen"]["agents"].clear()
            with _profile_runtime_scope(homes[name]):
                discover_plugins()  # as agent_init does for each profile's turn
                result = json.loads(terminal_tool("echo hi", task_id=f"ap-{name}"))
            assert result["exit_code"] == 0, result
            assert fake_dashboard["seen"]["auth"] == {f"Bearer token-{name}"}
            assert fake_dashboard["seen"]["agents"] == {f"agent-{name}"}
    finally:
        reset_multiplex_context(token)
        cleanup_all_environments()
