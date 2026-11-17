"""A session whose profile was deleted under it is not flushed again and is reaped once idle. Live 2026-10-15
the reaper retried a deleted profile's transcript flush every interval for the life of the process: every
writer refuses the tombstoned home, so each retry only failed again."""

import threading
import time

import pytest

from hermes_constants import mark_named_profile_deleted
from tui_gateway import server as tui_server


class _Agent:
    def __init__(self, seen, name):
        self._session_messages = [{"role": "user", "content": "hi"}]
        self._seen, self._name = seen, name

    def _persist_session(self, _messages):
        self._seen.append(self._name)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    profiles = tmp_path / ".hermes" / "profiles"
    alive, gone = profiles / "alive", profiles / "gone"
    alive.mkdir(parents=True)
    mark_named_profile_deleted(gone)  # deleted: tombstoned, directory removed
    seen: list[str] = []
    now = time.time()
    table = {
        "s-alive": {"agent": _Agent(seen, "alive"), "profile_home": str(alive), "last_active": now, "created_at": now},
        "s-gone": {"agent": _Agent(seen, "gone"), "profile_home": str(gone), "last_active": now, "created_at": now},
    }
    monkeypatch.setattr(tui_server, "_sessions", table, raising=False)
    monkeypatch.setattr(tui_server, "_sessions_lock", threading.RLock(), raising=False)
    monkeypatch.setattr(tui_server, "_INCREMENTAL_FLUSH_INTERVAL_S", 30.0, raising=False)
    return table, seen


def test_deleted_profile_session_is_not_flushed(sessions):
    _table, seen = sessions
    assert tui_server._flush_dirty_sessions(now=1000.0) == 1
    assert seen == ["alive"]


def test_idle_deleted_profile_session_is_reaped_and_live_one_kept(sessions, monkeypatch):
    table, _seen = sessions
    closed: list[tuple[str, str]] = []

    def fake_close(sid, *, end_reason, predicate=None):
        if predicate is None or predicate(table[sid]):
            closed.append((sid, end_reason))
            table.pop(sid)
        return True

    monkeypatch.setattr(tui_server, "_close_session_by_id", fake_close)
    for name in ("_repair_missing_ws_orphan_reaps", "_enforce_session_cap", "_reclaim_orphaned_leases"):
        monkeypatch.setattr(tui_server, name, lambda: None)
    monkeypatch.setattr(tui_server, "_sessions_quiescent", lambda *a, **k: False)

    table["s-gone"]["running"] = True
    tui_server._reap_idle_sessions()
    assert closed == []  # never mid-turn: the tool guard owns a running deleted-profile turn

    table["s-gone"]["running"] = False
    tui_server._reap_idle_sessions()
    assert closed == [("s-gone", "profile_deleted")]
    assert "s-alive" in table
