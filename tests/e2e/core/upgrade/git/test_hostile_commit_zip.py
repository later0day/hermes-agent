"""Real ZIP-swap crash cells for the source-update commit point."""

from __future__ import annotations

import shutil

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.pm import _pm as P
from tests.e2e.core.upgrade.git.test_hostile_commit import (
    _artifacts,
    _head,
    _launch,
    _release,
    _tracked_dirty,
    _update,
    _zip_killed_at,
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
    root = tmp_path_factory.mktemp("hostile-commit-zip")
    sb, origin = P.install_head(root)
    P.configure(sb, provider.base_url)
    shim = sb.root / "wrap" / "git"
    world = {"sb": sb, "origin": origin, "root": root, "shim": shim,
             "shim_text": shim.read_text(encoding="utf-8-sig"), "n": 0}
    yield world
    shim.write_text(world["shim_text"], encoding="utf-8")


def test_kill_mid_zip_swap_is_settled_by_the_next_launch(world):
    sb = world["sb"]
    pre = _head(sb)
    # Killed right after a TRACKED file's new bytes landed: the swap order follows the archive's
    # directory listing, so a "kill at the N-th swap" can land on N unchanged entries only.
    agents = I.git("show", "HEAD:AGENTS.md", cwd=world["origin"]) + "\n<!-- e2e zip swap -->\n"
    killed = _zip_killed_at(world, "after:AGENTS.md", {"AGENTS.md": agents})
    at_kill = {"artifacts": _artifacts(sb), "dirty": _tracked_dirty(sb),
               "new_entry": (sb.checkout / "e2e_zip_release.py").exists()}
    assert at_kill["artifacts"] and at_kill["dirty"], (
        f"harness: the ZIP swap was not killed mid-rename ({at_kill['dirty']!r})\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill mid ZIP swap")
    leftovers = _artifacts(sb)
    assert not leftovers, (f"{len(leftovers)} staging/backup siblings leaked and wedge the next update: "
                           f"{leftovers[:5]} (at kill: {len(at_kill['artifacts'])} siblings, "
                           f"tracked changes {at_kill['dirty']!r})\n" + I.describe(launch))
    assert _head(sb) == pre and not _tracked_dirty(sb), (
        "the interrupted ZIP swap left a mixed tree:\n" + _tracked_dirty(sb) + "\n" + I.describe(launch))


def test_kill_mid_zip_swap_with_hermes_cli_moved_aside_reaches_recovery(world):
    """The one window where the recovery code itself is gone: the swap renamed ``hermes_cli/`` aside
    and died before its replacement landed. The next launch must still reach the journal-driven
    restore (from the moved-aside copy) and run, not die on ``No module named 'hermes_cli'``."""
    sb = world["sb"]
    pre = _head(sb)
    killed = _zip_killed_at(world, "hermes_cli-moved-aside")
    assert not (sb.checkout / "hermes_cli").exists() and (sb.checkout / "hermes_cli.hermes-update-old").is_dir(), (
        "harness: the ZIP swap was not killed with hermes_cli/ moved aside\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill with hermes_cli moved aside")
    assert launch.returncode == 0 and (sb.checkout / "hermes_cli" / "main.py").is_file(), (
        "the launch after a kill with hermes_cli/ moved aside could not reach recovery\n" + I.describe(launch))
    assert not _artifacts(sb) and _head(sb) == pre and not _tracked_dirty(sb), (
        f"left {_artifacts(sb)[:5]} / tracked changes {_tracked_dirty(sb)!r}\n" + I.describe(launch))
    P.ok(_update(sb), "the update after the restored swap failed")


def test_kill_mid_zip_swap_over_leftover_backup_keeps_the_entry(world):
    """A pre-journal crash left ``e2e_zip_lost.hermes-update-old`` as the ONLY copy of a tracked dir.
    The next swap heals it, swaps it, and is killed right after its replacement landed: recovery
    must put the old dir back, never drop both copies."""
    sb = world["sb"]
    pre = _release(world, {"e2e_zip_lost/m.py": "OLD = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    lost = sb.checkout / "e2e_zip_lost"
    lost.rename(sb.checkout / "e2e_zip_lost.hermes-update-old")
    killed = _zip_killed_at(world, "after:e2e_zip_lost", {"e2e_zip_lost/m.py": "NEW = 1\n"})
    assert (lost / "m.py").read_text(encoding="utf-8-sig") == "NEW = 1\n" and (sb.checkout / "e2e_zip_lost.hermes-update-old").is_dir(), (
        "harness: the swap was not killed right after e2e_zip_lost landed\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill over a leftover backup")
    assert (lost / "m.py").is_file() and (lost / "m.py").read_text(encoding="utf-8-sig") == "OLD = 1\n", (
        "recovery deleted both copies of an entry whose backup was healed before the swap: "
        f"{sorted(p.name for p in sb.checkout.iterdir() if p.name.startswith('e2e_zip'))}\n" + I.describe(launch))
    assert not _artifacts(sb) and _head(sb) == pre and not _tracked_dirty(sb), (
        f"left {_artifacts(sb)[:5]} / tracked changes {_tracked_dirty(sb)!r}\n" + I.describe(launch))
