"""Two hosted agent scopes isolated by real Docker containers, not a recording backend.

Armed by ``HERMES_TEST_DOCKER_IMAGE`` (for example ``debian:13-slim``). Once armed, a
missing daemon fails the test. This proves the production scope/conformance path and
Docker backend together: same-path files, process namespaces and host process checkpoints
do not cross agents, while each agent retains normal access to its own sandbox. It says
nothing about unexecuted hosted Modal, Daytona, Vercel or Browserbase clouds.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from agent import terminal_env_registry as reg
from agent.terminal_env_provider import TerminalEnvironmentProvider
from hermes_scope import Scope
from tests.waiting import wait_until
from tools.environments import sandbox_conformance as conformance
from tools.environments.docker import DockerEnvironment
from tools.environments.sandbox_scope import (
    CrossAgentHandleRefused,
    SandboxIdentity,
    sandbox_identity,
)

_IMAGE = os.getenv("HERMES_TEST_DOCKER_IMAGE", "")
_TEST_LABEL = "hermes-test-hosted-scope"

pytestmark = pytest.mark.skipif(not _IMAGE, reason="HERMES_TEST_DOCKER_IMAGE not set")


class _RealDockerEnvironment(DockerEnvironment):
    """Hosted-interface adapter for the production Docker environment.

    Core's ``ScopedEnvironment`` remains the authority that refuses foreign handles. This
    adapter only checks the scope forwarded by that guard and removes it before calling the
    built-in backend methods that do not have a scope parameter.
    """

    def __init__(self, provider: _RealDockerProvider, identity: SandboxIdentity,
                 owner: Scope, **kwargs: Any) -> None:
        self._provider, self.identity, self._owner_scope = provider, identity, owner
        super().__init__(**kwargs)

    def _record(self, op: str, scope: Optional[Scope], command: str = "",
                stdin: Optional[str] = None) -> Scope:
        scope = scope or self._owner_scope
        if sandbox_identity(scope) != self.identity:
            raise CrossAgentHandleRefused(f"{op} reached another agent's Docker sandbox")
        self._provider.calls.append(conformance.Call(self.identity, op, scope, command, stdin or ""))
        return scope

    def execute(self, command: str, cwd: str = "", *, scope: Optional[Scope] = None,
                **kwargs: Any) -> dict:
        self._record("execute", scope, command, kwargs.get("stdin_data"))
        return super().execute(command, cwd, **kwargs)

    def get_temp_dir(self, *, scope: Optional[Scope] = None) -> str:
        if scope is not None:
            self._record("get_temp_dir", scope)
        return super().get_temp_dir()

    def fetch_realpath(self, remote_path: str, *, scope: Scope) -> Optional[str]:
        self._record("fetch_realpath", scope, remote_path)
        result = self.execute(f"readlink -f {shlex.quote(remote_path)} 2>/dev/null",
                              scope=scope, rewrite_compound_background=False)
        if int(result.get("returncode") or 0) != 0:
            return None
        return next((line.strip() for line in reversed((result.get("output") or "").splitlines())
                     if line.strip().startswith("/")), None)

    def fetch_file(self, remote_path: str, local_dest: Path, *, max_bytes: int,
                   scope: Scope) -> None:
        self._record("fetch_file", scope, remote_path)
        # BaseEnvironment.fetch_file calls self.execute; the adapter supplies its owning
        # scope to that internal hop after this public entry point has checked it.
        super().fetch_file(remote_path, local_dest, max_bytes=max_bytes)

    def start_process(self, name: str, argv: list[str], *, child_env: Optional[dict] = None,
                      scope: Scope):
        self._record("start_process", scope, " ".join(argv))
        return super().start_process(name, argv, child_env=child_env, scope=scope)

    def attach_process(self, name: str, *, scope: Scope):
        self._record("attach_process", scope, name)
        return super().attach_process(name, scope=scope)

    def stop_process(self, name: str, *, scope: Scope) -> bool:
        self._record("stop_process", scope, name)
        return super().stop_process(name, scope=scope)

    def stop_idle(self, *, scope: Optional[Scope] = None) -> None:
        self._record("stop_idle", scope)
        super().stop_idle()

    def cleanup(self, *, scope: Optional[Scope] = None, force_remove: bool = False) -> None:
        self._record("cleanup", scope)
        super().cleanup(force_remove=force_remove)


class _RealDockerProvider(TerminalEnvironmentProvider):
    """Test provider: one production Docker environment per hosted agent identity."""

    serves_hosted_scopes = True

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.created: list[SandboxIdentity] = []
        self.calls: list[conformance.Call] = []
        self.environments: dict[str, _RealDockerEnvironment] = {}
        self.scopes: dict[str, Scope] = {}

    @property
    def name(self) -> str:
        return f"real_docker_scope_{self.run_id}"

    def is_available(self) -> bool:
        return True

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None, container_config: Optional[dict] = None,
                           **kwargs: Any) -> _RealDockerEnvironment:
        scope, identity = kwargs["scope"], kwargs["sandbox"]
        assert task_id == identity.key
        assert sandbox_identity(scope) == identity
        resources = container_config or {}
        env = _RealDockerEnvironment(
            self, identity, scope,
            image=image or _IMAGE, cwd=cwd, timeout=timeout, task_id=task_id,
            cpu=resources.get("container_cpu", 0), memory=resources.get("container_memory", 0),
            disk=resources.get("container_disk", 0), persistent_filesystem=False,
            volumes=[], forward_env=[], env={}, network=False,
            persist_across_processes=True, shared_container_key=self.run_id,
            extra_args=["--label", f"{_TEST_LABEL}={self.run_id}"], image_pinned=True,
        )
        self.created.append(identity)
        self.environments[identity.key] = env
        self.scopes[identity.key] = scope
        return env

    def close(self) -> None:
        from agent.runtime_scope import bind_state
        from tools.terminal_tool_lifecycle import cleanup_vm

        try:
            for key, scope in self.scopes.items():
                with bind_state(scope):
                    cleanup_vm(key, force_remove=True)
            # An idle-suspend check may already have unregistered the environment. Its raw
            # production handle is still test-owned and must release its container too.
            for key, env in self.environments.items():
                if env._container_id:
                    with bind_state(self.scopes[key]):
                        env.cleanup(scope=self.scopes[key], force_remove=True)
        finally:
            # Even a failed assertion or teardown above reaches the label-scoped fallback.
            # The unique label makes every container selected here exclusively this test's.
            drained = DockerEnvironment.wait_for_all_teardowns(timeout=60)
            leftovers = _owned_containers(self.run_id)
            if leftovers:
                subprocess.run(["docker", "rm", "-f", *leftovers], capture_output=True,
                               timeout=60, check=False)
            assert drained and not _owned_containers(self.run_id), "test-owned Docker containers leaked"


def _owned_containers(run_id: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={_TEST_LABEL}={run_id}"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.split()


@pytest.fixture
def docker_scopes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", _IMAGE)
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE_PINNED", "1")
    monkeypatch.setenv("TERMINAL_DOCKER_ORPHAN_REAPER", "false")
    monkeypatch.setenv("TERMINAL_CONTAINER_DISK", "0")
    monkeypatch.setenv("TERMINAL_CWD", "/root")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))

    import model_tools  # registers the real tools used by conformance
    from tools import file_tools, process_registry, terminal_tool

    reg._reset_for_tests()
    run_id = uuid.uuid4().hex[:12]
    provider = _RealDockerProvider(run_id)
    reg.register_provider(provider)
    monkeypatch.setenv("TERMINAL_ENV", provider.name)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    monkeypatch.setattr(process_registry.process_registry, "_running", {})
    monkeypatch.setattr(process_registry.process_registry, "_finished", {})
    a = conformance.hosted_scope_for(f"agent-a-{run_id}", tenant="real-docker")
    b = conformance.hosted_scope_for(f"agent-b-{run_id}", tenant="real-docker")
    try:
        yield provider, a, b
    finally:
        provider.close()
        reg._reset_for_tests()


def _process_view(scope: Scope, marker: str) -> str:
    # Split the needle so the scanning shell's own cmdline cannot match the marker.
    cut = len(marker) // 2
    command = (
        f"needle={shlex.quote(marker[:cut])}; needle=\"$needle\"{shlex.quote(marker[cut:])}; "
        "for p in /proc/[0-9]*; do "
        "cmd=$(tr '\\000' ' ' < \"$p/cmdline\" 2>/dev/null); "
        "case \"$cmd\" in *\"$needle\"*) printf '%s %s\\n' \"$p\" \"$cmd\";; esac; done"
    )
    with conformance._bind(scope):
        result = conformance.call_tool("terminal", {"command": command}, task_id="shared-task")
    assert result.get("exit_code") == 0, result
    return result.get("output", "")


def test_two_hosted_agents_are_isolated_by_real_docker(docker_scopes, tmp_path, monkeypatch):
    provider, a, b = docker_scopes

    # Existing conformance checks dispatch through the real registry, scope guards and
    # production Docker backend. Both task-id confusion and foreign process handles fail closed.
    conformance.check_tools_address_own_sandbox(provider, a, b)
    conformance.check_foreign_handle_refused(provider, a, b)
    conformance.check_background_process_invisible(provider, a, b)

    env_a = provider.environments[sandbox_identity(a).key]
    env_b = provider.environments[sandbox_identity(b).key]
    assert env_a._container_id and env_b._container_id
    assert env_a._container_id != env_b._container_id

    # The same absolute path is two distinct filesystems: B can neither read A's bytes nor
    # overwrite them, and each agent reads back its own write.
    path = "/workspace/private.txt"
    a_bytes, b_bytes = f"A-PRIVATE-{uuid.uuid4().hex}", f"B-PRIVATE-{uuid.uuid4().hex}"
    with conformance._bind(a):
        a_write = conformance.call_tool("write_file", {"path": path, "content": a_bytes}, task_id="t")
    with conformance._bind(b):
        b_write = conformance.call_tool("write_file", {"path": path, "content": b_bytes}, task_id="t")
        b_read = conformance.call_tool("read_file", {"path": path}, task_id="t")
    for write in (a_write, b_write):
        assert write.get("success", True) and "error" not in write, write
    with conformance._bind(a):
        a_read = conformance.call_tool("read_file", {"path": path}, task_id="t")
    assert a_read["content"].removeprefix("1|") == a_bytes
    assert b_read["content"].removeprefix("1|") == b_bytes
    assert b_bytes not in json.dumps(a_read) and a_bytes not in json.dumps(b_read)

    # A real A process is visible in A's PID namespace and registry, but in neither view for B.
    marker = f"scopea{uuid.uuid4().hex}"
    with conformance._bind(a):
        started = conformance.call_tool(
            "terminal", {"command": f"exec -a {marker} sleep 600", "background": True},
            task_id="shared-task",
        )
    session_id = started.get("session_id")
    assert session_id, started
    try:
        wait_until(lambda: marker in _process_view(a, marker), timeout=20, interval=0.1,
                   what="A's real Docker process to start")
        assert marker not in _process_view(b, marker)
        with conformance._bind(a):
            own = conformance.call_tool(
                "process_manage", {"action": "poll", "session_id": session_id}, task_id="shared-task")
        with conformance._bind(b):
            foreign = conformance.call_tool(
                "process_manage", {"action": "poll", "session_id": session_id}, task_id="shared-task")
        assert own["status"] == "running" and foreign["status"] == "not_found"

        # An unscoped host checkpoint pass must omit the real hosted process. A restarted
        # registry therefore recovers nothing for either agent; A's live in-memory poll above
        # is the same-agent behavior that remains available.
        from tools import process_registry as process_registry_module
        from tools.process_registry import ProcessRegistry

        checkpoint = tmp_path / "processes.json"
        monkeypatch.setattr(process_registry_module, "_checkpoint_path", lambda: checkpoint)
        process_registry_module.process_registry._write_checkpoint()
        assert json.loads(checkpoint.read_text(encoding="utf-8")) == []
        assert session_id not in checkpoint.read_text(encoding="utf-8")
        restarted = ProcessRegistry()
        assert restarted.recover_from_checkpoint() == 0
        with conformance._bind(a):
            assert restarted.get(session_id) is None
        with conformance._bind(b):
            assert restarted.get(session_id) is None
    finally:
        with conformance._bind(a):
            conformance.call_tool(
                "process_manage", {"action": "kill", "session_id": session_id}, task_id="shared-task")
