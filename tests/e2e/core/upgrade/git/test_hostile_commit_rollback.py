"""Real branch, fork, and rollback crash cells for the source-update commit point."""

from __future__ import annotations

import shutil

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.pm import _pm as P
from tests.e2e.core.upgrade.git.test_hostile_commit import (
    _GIT_KILLER,
    _head,
    _hostile_git,
    _launch,
    _release,
    _stamp_commit,
    _tracked_dirty,
    _update,
)
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
    pytest.mark.skipif(I.real_uv() is None, reason="uv required"),
]


@pytest.fixture(scope="module")
def provider():
    with FakeLLMServer(default_text="fake reply for the hostile commit suite") as srv:
        yield srv


@pytest.fixture()
def world(tmp_path_factory, provider):
    root = tmp_path_factory.mktemp("hostile-commit-rollback")
    sb, origin = P.install_head(root)
    P.configure(sb, provider.base_url)
    shim = sb.root / "wrap" / "git"
    world = {"sb": sb, "origin": origin, "root": root, "shim": shim,
             "shim_text": shim.read_text(encoding="utf-8-sig"), "n": 0}
    yield world
    shim.write_text(world["shim_text"], encoding="utf-8")


def test_failed_upstream_sync_after_origin_pull_keeps_the_tail_owed(world):
    """Fork checkout: the origin pull moves the tree (committed), then the upstream fast-forward fails
    and is put back to the post-origin commit. That failure must not hand back the obligations the
    origin pull armed: a kill right after it still owes the tail."""
    sb, root = world["sb"], world["root"]
    first = _release(world, {"zzz_e2e_fork/mod.py": "V = 1\n"})
    P.ok(_update(sb), "harness: plain update to the first release failed")
    assert _head(sb) == first and _stamp_commit(sb) == first
    moved = _release(world, {"e2e_fork_origin.py": "B = 1\n"})
    upstream = root / "upstream.git"
    I.git("clone", "-q", "--bare", "--shared", str(world["origin"]), str(upstream), cwd=root)
    up = I.publish_commit(upstream, root, "upstream: touches the locked dir", {"zzz_e2e_fork/mod.py": "V = 2\n"})
    I.git("remote", "add", "upstream", str(upstream), cwd=sb.checkout)
    flag = sb.checkout / ".git" / "e2e-upstream-ff-failed"
    # origin reads as a fork (the sandbox shim otherwise reports the official URL). Kill the updater at
    # its next branch check once the upstream merge has failed (and been settled).
    _hostile_git(world, 'case " $* " in *" remote get-url origin "*) echo https://github.com/e2e-fork/hermes-agent.git; '
                        'exit 0;; esac\n'
                        f'case " $* " in *" merge --ff-only "*"{up} "*) touch "{flag}"; '
                        'echo "e2e: forced upstream fast-forward failure" >&2; exit 1;; esac\n'
                        f'if [ -e "{flag}" ]; then case " $* " in *" rev-parse --abbrev-ref HEAD "*) '
                        'kill -KILL $PPID; exit 137;; esac; fi')
    killed = _update(sb)
    _hostile_git(world, "")
    assert flag.exists() and killed.returncode != 0 and _head(sb) == moved and not _tracked_dirty(sb), (
        "harness: the upstream ff did not fail after the origin pull moved the tree\n" + I.describe(killed))
    owed = P.pending_marker(sb).exists()
    launch = _launch(sb, "first launch after a failed upstream sync")
    assert owed, ("the origin pull moved the tree but the failed upstream ff disarmed its owed tail\n"
                  + P.diagnostics(sb, killed, launch))
    assert _stamp_commit(sb) == moved and not P.pending_marker(sb).exists(), (
        "the next launch did not finish the tail for the moved tree\n" + P.diagnostics(sb, killed, launch))


def test_kill_during_branch_switch_without_local_branch_is_restored(world):
    """The parked checkout has no local ``main``: only ``origin/main``. Whichever checkout actually
    moves the tree to main must run under the marker."""
    sb = world["sb"]
    base = _head(sb)
    _release(world, {"e2e_dwim_a.py": "A = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    I.git("checkout", "-q", "-b", "e2e-parked", base, cwd=sb.checkout)
    I.git("branch", "-q", "-D", "main", cwd=sb.checkout)
    parked = _head(sb)
    _release(world, {"e2e_dwim_c.py": "C = 1\n"})
    # Die after writing one of main's files, in the checkout that moves the tree: a local main
    # exists, `-B` was given, or git may guess (no --no-guess) and create main from origin/main.
    # `-B main` names the resolved commit, not the ref (review O2), so it moves whatever comes last.
    _hostile_git(world, 'case " $* " in *" checkout "*) moves=0; case " $* " in *" -B main "*) moves=1;; esac; '
                        'case "${@: -1}" in main|origin/main) '
                        '"$REAL" rev-parse -q --verify refs/heads/main >/dev/null && moves=1; '
                        'case " $* " in *" --no-guess "*) ;; *) moves=1;; esac;; esac; '
                        'if [ $moves = 1 ]; then "$REAL" show origin/main:e2e_dwim_a.py > e2e_dwim_a.py; '
                        'kill -KILL $PPID; exit 137; fi;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert (sb.checkout / "e2e_dwim_a.py").is_file() and _head(sb) == parked, (
        "harness: the kill did not land mid-switch\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill during a guessed branch switch")
    assert _head(sb) == parked and not (sb.checkout / "e2e_dwim_a.py").exists(), (
        "a kill while git created main from origin/main left main's files in the parked checkout "
        "(no interrupted-pull marker)\n" + P.diagnostics(sb, killed, launch))


def test_kill_during_syntax_rollback_lands_on_the_pre_update_commit(world):
    """A broken release the preflight cannot see (its target read is answered with the old file)
    is rolled back by the post-pull guard; the updater dies while ``reset --hard`` is writing the old
    files back. The next launch must finish on the pre-update commit, not restore the broken one."""
    sb = world["sb"]
    pre = _head(sb)
    constants = I.git("show", "main:hermes_constants.py", cwd=world["origin"])
    broken = _release(world, {"hermes_constants.py": constants + "\ndef broken(:\n",
                              "e2e_rollback_extra.py": "X = 1\n"})
    _hostile_git(world, 'case " $* " in *" show "*":hermes_constants.py "*) "$REAL" show HEAD:hermes_constants.py; '
                        'exit $?;; '
                        '*" cat-file --batch "*) sed "s#^[^ ]*:hermes_constants.py\\$#HEAD:hermes_constants.py#" | "$REAL" "$@"; exit $?;; '
                        '*" reset --hard "*) "$REAL" show "${@: -1}:hermes_constants.py" > hermes_constants.py; '
                        'kill -KILL $PPID; exit 137;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert "syntax error" in (killed.stdout or "") + (killed.stderr or ""), (
        "harness: the post-pull guard did not fire\n" + I.describe(killed))
    assert (sb.checkout / "e2e_rollback_extra.py").exists(), "harness: the kill did not land mid-rollback\n" + I.describe(killed)
    launch = _launch(sb, "first launch after a kill during the syntax rollback")
    assert _head(sb) == pre and not _tracked_dirty(sb) and not (sb.checkout / "e2e_rollback_extra.py").exists(), (
        f"the launch after a kill mid-rollback did not land on {pre[:10]} (HEAD {_head(sb)[:10]}, broken "
        f"{broken[:10]}; tracked changes {_tracked_dirty(sb)!r})\n" + P.diagnostics(sb, killed, launch))


def test_git_killed_inside_the_rollback_reset_is_finished_by_the_next_launch(world):
    sb = world["sb"]
    pre = _head(sb)
    # A critical module the launcher imports only after the launch-time repair ran (the launcher's own
    # first import, hermes_constants, is a documented limit: a broken copy dies before any repair).
    tools = I.git("show", "main:model_tools.py", cwd=world["origin"])
    broken = _release(world, {"model_tools.py": tools + "\ndef broken(:\n",
                              "e2e_rollback_reset_extra.py": "X = 1\n"})
    killer = world["root"] / "git_killer.py"
    killer.write_text(_GIT_KILLER, encoding="utf-8")
    once = sb.root / "rollback-reset-killed"  # the sandbox can write only under its own root
    # The preflight reads the old file (so the broken release passes it); the rollback's first step,
    # `reset -q <pre>` (HEAD and index, no file), runs once under the killer.
    _hostile_git(world, 'case " $* " in *" show "*":model_tools.py "*) "$REAL" show HEAD:model_tools.py; '
                        'exit $?;; '
                        '*" cat-file --batch "*) sed "s#^[^ ]*:model_tools.py\\$#HEAD:model_tools.py#" | "$REAL" "$@"; exit $?;; '
                        f'*" reset -q {pre} "*) if [ ! -e "{once}" ]; then : > "{once}"; '
                        f'exec "{sb.python}" "{killer}" "$REAL" "$@"; fi;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    output = (killed.stdout or "") + (killed.stderr or "")
    lock = sb.checkout / ".git" / "index.lock"
    assert "syntax error" in output and once.exists(), "harness: the rollback reset never ran\n" + I.describe(killed)
    assert lock.exists() and _head(sb) == broken, (
        "harness: the kill did not land while git held index.lock\n" + I.describe(killed))
    assert killed.returncode != 0 and "Rollback complete" not in output, (
        "the updater reported a rollback that never happened\n" + I.describe(killed))
    assert (sb.checkout / ".git" / "hermes-update-pull").is_file(), (
        "the killed rollback erased its own recovery record while HEAD is still the broken release\n"
        + I.describe(killed))

    launch = _launch(sb, "first launch after git was killed inside the rollback")
    assert _head(sb) == pre and not _tracked_dirty(sb) and not lock.exists(), (
        f"the launch did not finish the rollback to {pre[:10]} (HEAD {_head(sb)[:10]}, broken {broken[:10]}, "
        f"lock {lock.exists()}, tracked changes {_tracked_dirty(sb)!r})\n" + P.diagnostics(sb, killed, launch))
    assert not (sb.checkout / "e2e_rollback_reset_extra.py").exists()
    assert not (sb.checkout / ".git" / "hermes-update-pull").exists()
