"""Killing a sandbox background process stops the real command, not just its wrapper.

``spawn_via_env`` launches ``( nohup bash -lc CMD ...; ) &`` and records ``$!`` — the wrapper
subshell. Killing only that PID reparents ``bash -lc CMD`` (and its children, e.g. a dev server) to
the sandbox's init, where they keep running: ``process kill`` reported success while the server
stayed up (seen live in a Docker room sandbox).
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import uuid

import pytest

from tools.process_registry import ProcessRegistry


class _ExecPerCommandEnv:
    """Each execute() is its own process group, like ``docker exec`` / an SSH command."""

    def execute(self, command, timeout=None, **_kw):
        # Output goes through a file, not a pipe: the backgrounded job inherits stdout and would
        # hold a pipe open until it exits (real backends read their own framed output instead).
        with tempfile.TemporaryFile("w+") as out:
            proc = subprocess.run(["bash", "-c", command], stdout=out, stderr=subprocess.STDOUT,
                                  timeout=timeout or 30, start_new_session=True)
            out.seek(0)
            return {"output": out.read(), "returncode": proc.returncode}


def _alive(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


@pytest.mark.platforms("linux")
def test_killing_a_sandbox_job_stops_its_whole_tree(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *a, **k: None)
    marker = f"sleep 30{uuid.uuid4().int % 1000:03d}"  # unique argv to find our own processes
    # "; true" keeps `bash -lc` as the parent of sleep (no exec optimisation): a real child tree.
    session = registry.spawn_via_env(_ExecPerCommandEnv(), f"{marker}; true")
    assert session.pid, session.output_buffer
    try:
        deadline = time.monotonic() + 10
        while not _alive(marker) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _alive(marker)

        registry.kill_process(session.id, source="test")

        deadline = time.monotonic() + 10
        while _alive(marker) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(marker), "the command outlived the kill (only the wrapper subshell died)"
    finally:
        subprocess.run(["pkill", "-f", f"^{marker}$"], capture_output=True)


@pytest.mark.platforms("linux")
def test_owner_tag_reaches_jobs_a_restarted_registry_forgot(monkeypatch):
    """After a host-process restart the in-memory registry is empty, but the sandbox jobs live on in a
    persistent container. The owner tag lets a sweep stop exactly one session's jobs."""
    from tools.process_registry import sandbox_owner_kill_command

    env = _ExecPerCommandEnv()
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *a, **k: None)
    mine, theirs = (f"sleep 31{uuid.uuid4().int % 1000:03d}", f"sleep 32{uuid.uuid4().int % 1000:03d}")
    registry.spawn_via_env(env, f"{mine}; true", session_key="room-session-a")
    registry.spawn_via_env(env, f"{theirs}; true", session_key="other-chat-b")
    try:
        deadline = time.monotonic() + 10
        while not (_alive(mine) and _alive(theirs)) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _alive(mine) and _alive(theirs)
        del registry  # the restarted process knows nothing about either job

        out = env.execute(sandbox_owner_kill_command("room-session-a"))["output"]

        assert any(line.strip().isdigit() for line in out.splitlines())
        deadline = time.monotonic() + 10
        while _alive(mine) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(mine)
        assert _alive(theirs)
    finally:
        for marker in (mine, theirs):
            subprocess.run(["pkill", "-f", f"^{marker}$"], capture_output=True)
