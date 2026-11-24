"""Desktop ``groups.approve`` reaches a member turn that the OTHER process runs.

The gateway and the dashboard both run room turns. ``handle_action`` (dashboard) re-read durable actions
and queued the decision for the owning process; ``approve_room_task`` (the desktop RPC) read only its
own memory, so approving a turn held elsewhere failed with "no longer pending" until the turn timed out.
"""
from __future__ import annotations

import time

import pytest

from gateway import hosted_room_driver as driver
from persistence_assembly import open_persistence, profile_scope
from tests.tui_gateway.test_hosted_room_service import _FakeRPC, _server
from tui_gateway.hosted_room_service import HostedRoomService


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    """A store handle on the test home, closed at teardown."""
    handle = open_persistence(profile_scope(home=tmp_path))
    yield handle
    handle.close()


def test_desktop_approval_is_queued_for_the_process_that_owns_the_turn(store):
    gateway = HostedRoomService(_server(), store=store, placement="profile")
    gateway.rpc = _FakeRPC()
    gateway.runtime.rpc = gateway.rpc
    gateway.rpc.owns_session = lambda session_id: session_id == "owner-session"
    gateway.local_profiles = lambda: ("default", "ops")
    gateway.create_room(room_id="room-1", name="x", members=[
        {"member_id": "default", "profile": "default", "handle": "default"},
        {"member_id": "ops", "profile": "ops", "handle": "ops"}])
    gateway.send(room_id="room-1", event_id="user-1", payload={"text": "@ops inspect", "thread_id": "t1"})
    task = driver.list_tasks(store, placement="profile", room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        store,
        placement="profile",
        room_id="room-1",
        gateway_id=gateway.bindings()[0].gateway_id,
        authority_epoch=1,
        process_generation="gateway-process",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        store,
        task["identity"],
        lease,
        placement="profile",
        expected_cancel_generation=0,
        clock=time.time,
    )
    member_id = str(task["payload"]["target_member_id"])
    gateway._set_pending_action("room-1", member_id, {
        "kind": "approval", "task_id": task["identity"].task_id,
        "execution_generation": attempt.execution_generation, "session_id": "owner-session",
        "request_id": "request-1", "approval": {"request_id": "request-1", "description": "dangerous command"}})

    desktop = HostedRoomService(  # a separate process: empty in-memory actions
        _server(), store=store, placement="profile"
    )
    desktop.rpc = _FakeRPC()
    desktop.runtime.rpc = desktop.rpc
    desktop.rpc.owns_session = lambda _session_id: False

    result = desktop.approve_room_task("room-1", member_id=member_id, task_id=task["identity"].task_id,
                                       execution_generation=attempt.execution_generation, choice="once",
                                       request_id="request-1")

    assert result["queued"] is True and desktop.rpc.approvals == []
    assert gateway._relay_action_decisions_once() == 1
    assert gateway.rpc.approvals == [{"session_id": "owner-session", "request_id": "request-1", "choice": "once"}]
