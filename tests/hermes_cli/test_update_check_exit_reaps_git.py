"""The passive update check's remote git dies with the CLI that started it.

``hermes chat`` starts ``check_for_updates`` in a daemon thread at startup; on an official
checkout it resolves the stable release and verifies the tag with ``git ls-remote --tags origin``,
which forks ``git remote-https``. A ``/exit`` while that lookup waits on GitHub abandoned the
thread, and the git process and its helper reparented to init and kept running (the 0.23.2
release E2E caught them in ``test_terminal_transcript_pty.py[cli-turns]``).

Real processes, no mocks of process semantics: a child interpreter runs the real
``resolve_source_release`` from a daemon thread against a fake ``git`` that forks a helper and
hangs, then exits the way the CLI does. Both the git and its helper must be gone afterwards.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import pid_exists_stdlib
from tests.waiting import wait_until

pytestmark = [
    pytest.mark.platforms("posix"),  # the owner-death supervisor is POSIX
    # Cleanup SIGKILLs the fake git's pids if they outlived the child: reparented to init, they are
    # outside this test's process subtree but still ours.
    pytest.mark.live_system_guard_bypass,
]

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 40

_CHILD = textwrap.dedent('''
    import sys, threading
    from hermes_cli import source_releases

    fake_git, cwd, marker = sys.argv[1:4]
    answers = {
        "https://api.github.com/repos/o/r/releases/latest":
            {"draft": False, "prerelease": False, "tag_name": "v1.2.3"},
        "https://api.github.com/repos/o/r/commits/v1.2.3": {"sha": "@SHA@"},
    }
    source_releases._json = answers.__getitem__  # GitHub's answers; only git is left to wait on

    threading.Thread(target=source_releases.resolve_source_release, daemon=True,
                     args=("stable", [fake_git], cwd), kwargs={"repository": "o/r", "pointer": False}).start()
    # Exit as /exit does, once the lookup's git and its helper are both running and the lookup is
    # waiting on them (its group handed to the owner-death supervisor): the marker lands from the
    # fake git before the spawning thread registers, so exiting on the marker alone races that.
    import pathlib
    from hermes_cli import process_identity
    from tests.waiting import wait_until
    pids = wait_until(lambda: (lambda p: len(p) >= 2 and p)(pathlib.Path(marker).read_text().split()),
                      timeout=30, what="the fake git and its helper to start")
    wait_until(lambda: int(pids[0]) in process_identity._supervised_pgids, timeout=30,
               what="the lookup's git group to be handed to the owner-death supervisor")
    with process_identity._supervisor_lock:  # the register line is written under this lock
        pass
''').replace("@SHA@", SHA)



_CHILD_SPAWN_RACE = textwrap.dedent('''
    import pathlib, sys, threading
    from hermes_cli import process_identity, source_releases

    fake_git, cwd, marker, spawned_marker = sys.argv[1:5]
    answers = {
        "https://api.github.com/repos/o/r/releases/latest":
            {"draft": False, "prerelease": False, "tag_name": "v1.2.3"},
        "https://api.github.com/repos/o/r/commits/v1.2.3": {"sha": "@SHA@"},
    }
    source_releases._json = answers.__getitem__
    spawned = threading.Event()
    real_own = process_identity.own_process_groups

    def delay_ownership(pgids):
        pathlib.Path(spawned_marker).write_text(str(next(iter(pgids))))
        spawned.set()
        threading.Event().wait(30)
        real_own(pgids)

    process_identity.own_process_groups = delay_ownership
    threading.Thread(target=source_releases.resolve_source_release, daemon=True,
                     args=("stable", [fake_git], cwd), kwargs={"repository": "o/r", "pointer": False}).start()
    spawned.wait(30)
''').replace("@SHA@", SHA)


def _fake_git(tmp_path):
    marker = tmp_path / "pids"
    marker.write_text("")
    fake_git = tmp_path / "git"
    fake_git.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        sleep 300 &
        echo $$ $! >> {marker}
        wait
    """))
    fake_git.chmod(0o755)
    return fake_git, marker


def _run_child(tmp_path, script):
    fake_git, marker = _fake_git(tmp_path)
    home = tmp_path / "hermes-home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(REPO)}
    spawned_marker = tmp_path / "spawned-pid"
    child = subprocess.run(
        [sys.executable, "-c", script, str(fake_git), str(tmp_path), str(marker), str(spawned_marker)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert child.returncode == 0, child.stderr
    return marker, spawned_marker


def _cleanup(marker, spawned_marker):
    pids = [int(p) for p in marker.read_text().split()]
    if spawned_marker.exists():
        pids.append(int(spawned_marker.read_text()))
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_exit_during_the_stable_tag_lookup_leaves_no_git_behind(tmp_path):
    marker = spawned_marker = tmp_path / "unused"
    try:
        marker, spawned_marker = _run_child(tmp_path, _CHILD)
        pids = [int(p) for p in marker.read_text().split()]
        assert len(pids) == 2, "the fake git never ran"
        wait_until(lambda: not any(map(pid_exists_stdlib, pids)), timeout=15,
                   what="the update check's git and its helper to die with the CLI")
    finally:
        _cleanup(marker, spawned_marker)


def test_exit_between_spawn_and_ownership_never_executes_remote_git(tmp_path):
    marker = spawned_marker = tmp_path / "unused"
    try:
        marker, spawned_marker = _run_child(tmp_path, _CHILD_SPAWN_RACE)
        spawned_pid = int(spawned_marker.read_text())
        wait_until(lambda: not pid_exists_stdlib(spawned_pid), timeout=15,
                   what="the unowned launch wrapper to die with the CLI")
        assert marker.read_text() == "", "the remote git ran before its ownership was registered"
    finally:
        _cleanup(marker, spawned_marker)
