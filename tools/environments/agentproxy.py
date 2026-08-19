"""AgentProxy execution environment.

Runs every shell command *inside a Docker container on a remote AgentProxy
agent* (e.g. ``home``) — WITHOUT SSH.  The only transport is the AgentProxy
Dashboard task API (``POST /api/tasks/run``, SSE), the same channel the
``ap`` CLI (``/opt/agentproxy/run``) uses.

Design (structurally identical to the SSH backend, different transport):

    hermes  ──HTTPS/SSE──▶  AgentProxy Cloud  ──gRPC──▶  home agent (zsh)
                                                            │
                                                    docker exec <container> bash -c '<script>'
                                                            │
                                                    container (bash)  ◀── runs the wrapped
                                                                          script BaseEnvironment
                                                                          generates

BaseEnvironment already provides cross-command CWD persistence (in-band stdout
marker) and env/alias/function snapshots (a file in the container's /tmp).  So
even though each ``ap`` call is a one-shot shell, working directory and
environment carry across commands exactly like the SSH backend's
spawn-per-call model.

Two things this transport needs that SSH doesn't:

1. The ``ap`` CLI mixes human decorations (``✓ 完成 (123ms)``) into stdout and
   does NOT surface the remote exit code — so we bypass the CLI and call the
   Dashboard API directly, appending an exit-code marker to the remote command
   and parsing it back out of the SSE ``content`` stream.
2. home is macOS/zsh; the real work runs in a Linux container, so every
   command is wrapped as ``PATH=/usr/local/bin:$PATH; docker exec <container>
   bash -c '<script>'``.
"""

import json
import logging
import os
import re
import shlex
import ssl
import threading
import urllib.request

from tools.environments.base import (
    BaseEnvironment,
    EnvironmentConnectionError,
    _ThreadedProcessHandle,
)

logger = logging.getLogger(__name__)

# Exit-code sentinel appended to every remote command.  Chosen to be extremely
# unlikely to appear in normal output; parsed out (and stripped) by _ap_exec.
_EC_MARK = "__HERMES_AP_EC__"
_EC_RE = re.compile(re.escape(_EC_MARK) + r"(-?\d+)" + re.escape(_EC_MARK))


def _read_token(env_file: str) -> str:
    """Resolve the Dashboard bearer token.

    Order: ``$DASHBOARD_TOKEN`` env var → ``DASHBOARD_TOKEN=`` line in
    *env_file* (the systemd EnvironmentFile that is the cloud's own source of
    truth).  Mirrors the resolution the ``ap`` CLI now uses so token rotation
    is picked up automatically.
    """
    tok = os.getenv("DASHBOARD_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(env_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("DASHBOARD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class AgentProxyEnvironment(BaseEnvironment):
    """Run commands in a Docker container on a remote AgentProxy agent.

    Spawn-per-call via _ThreadedProcessHandle wrapping a blocking Dashboard
    API round-trip.  CWD/env persist via BaseEnvironment's snapshot + marker
    machinery (state lives in the container's /tmp, not on the host).
    """

    # No real stdin channel over the task API — embed stdin as a heredoc,
    # same as the Modal/Daytona SDK backends.
    _stdin_mode = "heredoc"
    # Remote container cold-start / first snapshot can be slow.
    _snapshot_timeout = 60

    def __init__(
        self,
        agent_id: str = "home",
        container: str = "hermes-reverse",
        image: str = "nikolaik/python-nodejs:python3.11-nodejs20",
        cloud_url: str = "https://127.0.0.1:8080",
        env_file: str = "/opt/agentproxy/.env",
        path_prefix: str = "/usr/local/bin",
        docker_run_args: str = "",
        cwd: str = "/root",
        timeout: int = 180,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.agent_id = agent_id
        self.container = container
        self.image = image
        self.cloud_url = cloud_url.rstrip("/")
        self.env_file = env_file
        # Prepended to every home-side command so the Docker CLI is on PATH
        # under home's non-interactive zsh (Docker Desktop lives in
        # /usr/local/bin, not on the default non-login PATH).
        self.path_prefix = path_prefix
        self.docker_run_args = docker_run_args
        self._lock = threading.Lock()

        self._token = _read_token(env_file)
        if not self._token:
            raise EnvironmentConnectionError(
                "AgentProxy backend: no DASHBOARD_TOKEN found "
                f"(env var or {env_file})",
                retry_hint="Set DASHBOARD_TOKEN or fix the AgentProxy .env file.",
            )
        # Accept the cloud's self-signed cert on loopback.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

        self._ensure_container()
        self.init_session()

    # ------------------------------------------------------------------
    # Transport primitive: run a command ON the agent (home), return (out, ec)
    # ------------------------------------------------------------------

    def _ap_exec(self, home_command: str, timeout: int) -> tuple[str, int]:
        """Run *home_command* on the agent via the Dashboard task API.

        Returns ``(stdout, exit_code)``.  ``exit_code`` is parsed from the
        ``_EC_MARK`` sentinel the caller is expected to append; if the marker
        is absent (agent/transport failure) the API-level success flag decides
        (0 on success, 1 otherwise).
        """
        body = json.dumps(
            {"agent_id": self.agent_id, "prompt": home_command, "mode": "shell"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cloud_url}/api/tasks/run",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )

        chunks: list[str] = []
        api_success: bool | None = None
        api_error = ""
        try:
            with urllib.request.urlopen(
                req, context=self._ssl_ctx, timeout=timeout + 30
            ) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if not line or line.startswith("event:"):
                        continue
                    if line.startswith("data: "):
                        line = line[len("data: ") :]
                    try:
                        evt = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(evt, dict):
                        continue
                    if evt.get("content"):
                        chunks.append(evt["content"])
                    if evt.get("success") is not None:
                        api_success = bool(evt["success"])
                    if evt.get("error_message"):
                        api_error = str(evt["error_message"])
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise EnvironmentConnectionError(
                f"AgentProxy API HTTP {e.code}: {detail}",
                retry_hint="Check DASHBOARD_TOKEN and that the cloud service is up.",
            )
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise EnvironmentConnectionError(
                f"AgentProxy API unreachable: {e}",
                retry_hint=f"Verify {self.cloud_url} and the '{self.agent_id}' agent are online.",
            )

        out = "".join(chunks)
        m = None
        for m in _EC_RE.finditer(out):
            pass  # keep the last match
        if m is not None:
            ec = int(m.group(1))
            out = _EC_RE.sub("", out)
        else:
            # No marker: agent/transport-level failure. Trust API flags.
            ec = 0 if api_success else 1
            if api_error:
                out = (out + f"\n[agentproxy] {api_error}").strip()
        return out, ec

    # ------------------------------------------------------------------
    # Container lifecycle (on home)
    # ------------------------------------------------------------------

    def _ensure_container(self) -> None:
        """Create/start the long-lived work container on the agent (idempotent)."""
        extra = f" {self.docker_run_args}" if self.docker_run_args else ""
        script = (
            f"export PATH={shlex.quote(self.path_prefix)}:$PATH\n"
            f"c={shlex.quote(self.container)}\n"
            'if [ "$(docker inspect -f {{.State.Running}} "$c" 2>/dev/null)" = "true" ]; then\n'
            '  echo already-running\n'
            'else\n'
            '  docker rm -f "$c" >/dev/null 2>&1 || true\n'
            f'  docker run -d --name "$c" -w {shlex.quote(self.cwd)}{extra} '
            f'{shlex.quote(self.image)} sleep infinity >/dev/null 2>&1 '
            '&& echo started || { echo "run-failed"; exit 1; }\n'
            'fi\n'
            f'printf "{_EC_MARK}%s{_EC_MARK}" "$?"\n'
        )
        out, ec = self._ap_exec(script, timeout=120)
        if ec != 0:
            raise EnvironmentConnectionError(
                f"AgentProxy backend: failed to bring up container "
                f"'{self.container}' on '{self.agent_id}': {out.strip()}",
                retry_hint="Verify Docker is running on the agent and the image is pullable.",
            )
        logger.info(
            "AgentProxy: container '%s' ready on '%s' (%s)",
            self.container, self.agent_id, out.strip(),
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120, stdin_data: str | None = None):
        """Return a _ThreadedProcessHandle running *cmd_string* in the container.

        The BaseEnvironment-generated script (``cmd_string``) is executed by
        ``bash`` INSIDE the container via ``docker exec``.  The container's
        exit code is captured and echoed via the sentinel so _ap_exec can
        recover the true returncode.
        """
        inner = f"bash -l -c {shlex.quote(cmd_string)}" if login \
            else f"bash -c {shlex.quote(cmd_string)}"
        home_command = (
            f"export PATH={shlex.quote(self.path_prefix)}:$PATH; "
            f"docker exec {shlex.quote(self.container)} {inner}; "
            f'__ec=$?; printf "{_EC_MARK}%s{_EC_MARK}" "$__ec"'
        )

        def exec_fn() -> tuple[str, int]:
            with self._lock:
                return self._ap_exec(home_command, timeout=timeout)

        return _ThreadedProcessHandle(exec_fn)

    def cleanup(self):
        """No-op by default: the container is long-lived and reused.

        The container is intentionally NOT torn down here so state persists
        across sessions (like docker_persist_across_processes). Remove it
        manually with ``ap <agent> 'docker rm -f <container>'`` if needed.
        """
        return
