"""Disbanding a hosted room stops its members' background processes.

A member's ``terminal(background=true)`` job is registered under the member's room session key and is not
tied to any turn, so stopping the room's turns used to leave dev servers and watchers running forever.
Every process serving rooms notices the disband on its next cycle and reaps its own registry.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import hosted_rooms
from tools.process_registry import process_registry
from tui_gateway.hosted_room_driver import room_session_title
from tui_gateway.hosted_room_service import HostedRoomService


class _RPC:
    def __init__(self, sessions):
        self.sessions = sessions

    def resolve_exact(self, *, profile, title, source):
        key = self.sessions.get((profile, title))
        return {"session_id": key, "title": title} if key else None


def _running(proc_id: str) -> bool:
    session = process_registry.get(proc_id)
    return session is not None and not session.exited


@pytest.mark.platforms("linux", "macos")
def test_disband_reaps_member_background_processes_only(tmp_path: Path):
    service = HostedRoomService(
        SimpleNamespace(_sessions={}, _sessions_lock=threading.RLock()), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("lead", "worker")
    room = service.create_room(
        room_id="room-1", name="Room",
        members=[{"member_id": "lead", "profile": "lead", "handle": "lead", "role": "decider"},
                 {"member_id": "worker", "profile": "worker", "handle": "worker"}])
    service.rpc = _RPC({("worker", room_session_title("room-1")): "worker-room-session"})
    member_job = process_registry.spawn_local("sleep 300", cwd=str(tmp_path), session_key="worker-room-session")
    unrelated = process_registry.spawn_local("sleep 300", cwd=str(tmp_path), session_key="someone-elses-chat")
    try:
        assert [b.room_id for b in service._served_bindings()] == ["room-1"]
        assert _running(member_job.id)  # serving the room never touches its processes

        hosted_rooms.disband_room(service.db_path, room_id="room-1",
                                  expected_gateway_id=room["authority_gateway_id"], expected_epoch=1)
        assert service._served_bindings() == ()

        deadline = time.monotonic() + 10
        while _running(member_job.id) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _running(member_job.id)
        assert _running(unrelated.id)
    finally:
        for proc in (member_job, unrelated):
            process_registry.kill_process(proc.id, source="test")
