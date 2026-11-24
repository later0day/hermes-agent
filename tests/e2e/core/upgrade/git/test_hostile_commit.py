"""Real git-swap crash cells for the source-update commit point (lane LP-COMMIT).

ZIP and rollback cells live in sibling modules; this module owns their real-install helpers.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.pm import _pm as P
from tests.e2e.core.upgrade.test_upgrade_path import _RETRY_PREFIX
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
    pytest.mark.skipif(I.real_uv() is None, reason="uv required"),
]

REAL_GIT = shutil.which("git") or "git"
ARTIFACT_SUFFIXES = (".hermes-update-staging", ".hermes-update-old")


@pytest.fixture(scope="module")
def provider():
    with FakeLLMServer(default_text="fake reply for the hostile commit suite") as srv:
        yield srv


@pytest.fixture()
def world(tmp_path_factory, provider):
    root = tmp_path_factory.mktemp("hostile-commit")
    sb, origin = P.install_head(root)
    P.configure(sb, provider.base_url)
    shim = sb.root / "wrap" / "git"
    world = {"sb": sb, "origin": origin, "root": root, "shim": shim, "shim_text": shim.read_text(encoding="utf-8-sig"), "n": 0}
    yield world
    shim.write_text(world["shim_text"], encoding="utf-8")


def _hostile_git(world, body: str) -> None:
    """Prefix the sandbox's git shim with ``body`` (bash; ``$REAL`` is the real git, ``$PPID`` the
    updater process that spawned git)."""
    text = world["shim_text"]
    head, _, rest = text.partition("\n")
    world["shim"].write_text(f'{head}\nREAL="{REAL_GIT}"\n{body}\n{rest}', encoding="utf-8")


def _release(world, files: dict[str, str]) -> str:
    world["n"] += 1
    return I.publish_commit(world["origin"], world["root"], f"release: hostile commit {world['n']}", files)


def _head(sb) -> str:
    return I.git("rev-parse", "HEAD", cwd=sb.checkout)


def _tracked_dirty(sb) -> str:
    return I.git("status", "--porcelain", "--untracked-files=no", cwd=sb.checkout)


def _stamp_commit(sb) -> str:
    try:
        return json.loads((sb.checkout / "install-stamp.json").read_text(encoding="utf-8-sig")).get("commit") or ""
    except (OSError, ValueError):
        return ""


def _launch(sb, marker: str) -> subprocess.CompletedProcess:
    """A later real launch by the user (fresh pid, lazy installs on: the real launch path)."""
    return P.run_env(sb, [*_RETRY_PREFIX, sb.hermes, "-z", marker], P.lazy_env(sb), timeout=P.UPDATE_TIMEOUT)


def _update(sb) -> subprocess.CompletedProcess:
    return P.run_env(sb, [*_RETRY_PREFIX, sb.hermes, "update", "--yes", "--branch", "main", "--no-gateway-restart"],
                     sb.env, timeout=P.UPDATE_TIMEOUT)


def _artifacts(sb) -> list[str]:
    return sorted(p.name for p in sb.checkout.iterdir() if p.name.endswith(ARTIFACT_SUFFIXES))


def test_kill_after_tree_moved_still_owes_the_tail(world):
    sb = world["sb"]
    pre = _head(sb)
    assert _stamp_commit(sb) == pre and not P.pending_marker(sb).exists(), "harness: install not settled"
    target = _release(world, {"e2e_hostile_release.py": "RELEASE = 1\n"})
    # git completes the fast-forward, then the updater dies before it can spawn the completion child.
    _hostile_git(world, 'case " $* " in *" merge --ff-only "*) "$REAL" "$@"; rc=$?; kill -KILL $PPID; exit $rc;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert _head(sb) == target, "harness: the kill did not land after the tree moved\n" + I.describe(killed)
    owed = P.pending_marker(sb).exists()

    launch = _launch(sb, "first launch after a kill past the swap")
    assert owed, ("the tree moved to the release but no source-completion tail was owed: the kill "
                  "before the completion child lost it\n" + P.diagnostics(sb, killed, launch))
    assert not P.pending_marker(sb).exists() and _stamp_commit(sb) == target, (
        "the next launch did not finish the owed tail for the moved tree\n" + P.diagnostics(sb, killed, launch))


def test_torn_tree_on_git_failure_is_restored(world):
    sb = world["sb"]
    ok = _release(world, {"aaa_e2e_first.py": "V = 1\n", "zzz_e2e_locked/mod.py": "V = 1\n"})
    P.ok(_update(sb), "harness: plain update to the first release failed")
    assert _head(sb) == ok
    _release(world, {"aaa_e2e_first.py": "V = 2\n", "zzz_e2e_locked/mod.py": "V = 2\n"})
    _hostile_git(world, 'case " $* " in *" merge --ff-only "*) '
                        'printf "V = 2\\n" > zzz_e2e_locked/mod.py; '
                        'echo "e2e: forced mid-pull git failure" >&2; exit 1;; esac')
    failed = _update(sb)
    _hostile_git(world, "")
    assert failed.returncode != 0, "harness: the forced mid-pull git failure did not fire\n" + I.describe(failed)
    launch = _launch(sb, "first launch after a torn pull")
    assert _head(sb) == ok and not _tracked_dirty(sb), (
        "git failed mid-pull and the checkout was left torn (marker dropped, nothing restores it):\n"
        + _tracked_dirty(sb) + "\n" + P.diagnostics(sb, failed, launch))


def test_kill_during_branch_switch_is_restored(world):
    sb = world["sb"]
    base = _head(sb)
    _release(world, {"e2e_cp0_a.py": "A = 1\n", "e2e_cp0_b.py": "B = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    # Park the checkout on a fully merged branch one release behind main.
    I.git("checkout", "-q", "-b", "e2e-parked", base, cwd=sb.checkout)
    parked = _head(sb)
    _release(world, {"e2e_cp0_c.py": "C = 1\n"})
    # CP0: `git checkout main` dies after rewriting one file of main's tree.
    _hostile_git(world, 'if [ "${@: -1}" = main ] && case " $* " in *" checkout "*) true;; *) false;; esac; then '
                        '"$REAL" show main:e2e_cp0_a.py > e2e_cp0_a.py; kill -KILL $PPID; exit 137; fi')
    killed = _update(sb)
    _hostile_git(world, "")
    assert (sb.checkout / "e2e_cp0_a.py").is_file() and _head(sb) == parked, (
        "harness: the kill did not land mid-switch\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill during the branch switch")
    assert _head(sb) == parked and not (sb.checkout / "e2e_cp0_a.py").exists(), (
        "a kill during the CP0 branch switch left main's files in the parked checkout\n"
        + P.diagnostics(sb, killed, launch))


def test_syntax_error_target_is_refused_before_head_moves(world):
    sb = world["sb"]
    pre = _head(sb)
    constants = I.git("show", "main:hermes_constants.py", cwd=world["origin"])
    _release(world, {"hermes_constants.py": constants + "\ndef broken(:\n"})
    reflog_before = I.git("reflog", "-n", "20", "--format=%H %gs", cwd=sb.checkout)
    refused = _update(sb)
    reflog_after = I.git("reflog", "-n", "20", "--format=%H %gs", cwd=sb.checkout)
    assert refused.returncode != 0 and _head(sb) == pre, I.describe(refused)
    assert reflog_after == reflog_before, (
        "HEAD moved onto the uncompilable release before the syntax guard refused it:\n"
        + reflog_after.replace(reflog_before, "").strip() + "\n" + I.describe(refused))


_ZIP_DRIVER = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[3])  # the install's checkout (the venv's own path points at its workspace)
from hermes_cli import update_cmd_zip as z
import hermes_cli.main as m
assert str(m.PROJECT_ROOT) == sys.argv[3], m.PROJECT_ROOT
swaps, kill_at = [0], sys.argv[2]
def killing(real):
    def move(src, dst):
        real(src, dst)
        if kill_at.startswith("after:"):  # right after the named entry's replacement landed
            if os.path.basename(str(src)) == kill_at[6:] + ".hermes-update-staging":
                os.kill(os.getpid(), signal.SIGKILL)
        elif kill_at == "hermes_cli-moved-aside":  # the live package renamed away, its replacement not yet in
            if os.path.basename(str(src)) == "hermes_cli" and str(dst).endswith(".hermes-update-old"):
                os.kill(os.getpid(), signal.SIGKILL)
        elif str(src).endswith(".hermes-update-staging"):  # one entry swapped in
            swaps[0] += 1
            if swaps[0] >= int(kill_at):
                os.kill(os.getpid(), signal.SIGKILL)
    return move
os.rename, os.replace = killing(os.rename), killing(os.replace)
z._download_and_swap_zip("main", sys.argv[1])
"""


def _zip_killed_at(world, kill_at: str, files: dict[str, str] | None = None):
    """A ZIP update of a fresh release, SIGKILLed at ``kill_at`` (N-th entry swapped in, or a named point)."""
    sb = world["sb"]
    target = _release(world, {"e2e_zip_release.py": f"Z = {world['n']}\n", "hermes_cli/e2e_zip_marker.py": "M = 1\n",
                              **(files or {})})
    archive = world["root"] / f"release-{world['n']}.zip"
    I.git("archive", "--format=zip", "--prefix=hermes-agent-main/", "-o", str(archive), target, cwd=world["origin"])
    driver = world["root"] / "zip_driver.py"
    driver.write_text(_ZIP_DRIVER, encoding="utf-8")
    return P.run_env(sb, [sb.python, str(driver), archive.as_uri(), kill_at, str(sb.checkout)], sb.env,
                     timeout=P.UPDATE_TIMEOUT)












# Run by the git shim inside the sandbox: start the real git under an inotify watch on its git dir and
# SIGKILL it the moment ``index.lock`` is created (git then holds the lock and has not moved HEAD).
_GIT_KILLER = r"""
import ctypes, os, signal, struct, subprocess, sys
real, args = sys.argv[1], sys.argv[2:]
git_dir = subprocess.run([real, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.inotify_init1(0)
libc.inotify_add_watch(fd, git_dir.encode(), 0x100)  # IN_CREATE
git = subprocess.Popen([real, *args])
while git.poll() is None:
    buf, i = os.read(fd, 4096), 0
    while i < len(buf):
        length = struct.unpack_from("iIII", buf, i)[3]
        name, i = buf[i + 16:i + 16 + length].rstrip(b"\0"), i + 16 + length
        if name == b"index.lock":
            os.kill(git.pid, signal.SIGKILL)
            git.wait()
            sys.exit(137)
sys.exit(git.returncode)
"""
