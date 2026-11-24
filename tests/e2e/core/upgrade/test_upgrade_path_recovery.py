"""Recovery and offline upgrade legs share the canonical fixtures but run within their own file budget."""

from __future__ import annotations

import shutil

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade.test_upgrade_path import (
    TRACEBACK,
    _age_git_locks,
    _freeze_update_at,
    _git,
    _publish_head,
    _refs,
    _update,
    _upgrade_prerequisites,
    assert_healthy_at_head,
    leg as _leg,
    provider as _provider,
    snapshot_state,
    template_home,
)

leg = _leg
provider = _provider

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
]


def test_kill_before_deps_then_retry_heals(leg, provider):
    (leg.root / "arm-deps").touch()
    _publish_head(leg)
    _freeze_update_at(leg, "frozen-deps")
    assert _git("rev-parse", "HEAD", cwd=leg.install) == _refs().head, "kill point was not after the code swap"
    _age_git_locks(leg.install)
    final = _update(leg)
    assert_healthy_at_head(leg, provider, final)


def test_offline_update_fails_loudly_and_changes_nothing(leg, provider):
    _publish_head(leg)
    # Origin goes unreachable (network down / repo moved). The install borrows its objects from
    # the origin store (--shared), so the store moves and the install's alternates follow it.
    store = leg.root / "offline-store.git"
    leg.origin.rename(store)
    (leg.install / ".git" / "objects" / "info" / "alternates").write_text(f"{store / 'objects'}\n")
    cp = _update(leg)
    assert cp.returncode != 0, "update against an unreachable origin reported success:\n" + H.describe(cp)
    assert TRACEBACK not in cp.stdout + cp.stderr, H.describe(cp)
    assert _git("rev-parse", "HEAD", cwd=leg.install) == _refs().base
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=leg.install) == ""
    after = snapshot_state(leg)
    for name in ("default", "work"):
        assert after[name]["config"] == leg.snapshot[name]["config"], f"{name} config touched by a failed update"
        assert after[name]["db"] == leg.snapshot[name]["db"], f"{name} state.db touched by a failed update"
    ver = leg.run("--version")
    assert ver.returncode == 0, H.describe(ver)
    n = len(provider.main_requests())
    turn = leg.run("-z", "turn after a failed update")
    assert turn.returncode == 0 and provider.default_text in turn.stdout, H.describe(turn)
    assert len(provider.main_requests()) == n + 1
