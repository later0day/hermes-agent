"""Peer routes, grants, approvals, and recovery for the hosted room service."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    PROTOCOL_VERSION,
    catalog_mapping,
    issue_room_grant,
)
from persistence.gateway.hosted_rooms import HostedTaskIdentity
from persistence_assembly import open_persistence, profile_scope
from tests.tui_gateway.test_hosted_room_service import (
    _ApprovalPeerClient,
    _ExpiredGrantPeerClient,
    _ExpiredRevokePeerClient,
    _FakePeerClient,
    _FakeRPC,
    _NotAdmittedPeerClient,
    _RecoveringPeerClient,
    _RefreshingPeerClient,
    _UnavailablePeerClient,
    _UnavailableRevokePeerClient,
    _server,
    _wait_for,
)
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPError
from tui_gateway.hosted_room_peer_transport import PeerMemberRoute
from tui_gateway.hosted_room_service import HostedRoomService, _RouteStatusPeerClient


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    """A store handle on the test home, closed at teardown."""
    handle = open_persistence(profile_scope(home=tmp_path))
    yield handle
    handle.close()


def _mock_task_reads(monkeypatch, rows):
    """Give status() and approval operations the same task snapshot."""
    monkeypatch.setattr(driver, "list_tasks", lambda *_a, **_kw: list(rows))

    def room_task_summary(_store, *, placement, room_id, statuses):
        room_rows = [row for row in rows if row["identity"].room_id == room_id]
        counts = {}
        for row in room_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts, [row for row in room_rows if row["status"] in statuses]

    monkeypatch.setattr(driver, "room_task_summary", room_task_summary)


def test_local_pending_approval_requires_exact_task_generation_and_request(
    tmp_path: Path,
    store,
):
    class ApprovalRPC(_FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.approvals = []

        def approve(self, *, session_id, request_id, choice):
            self.approvals.append((session_id, request_id, choice))
            return {"resolved": 1}

    service = HostedRoomService(_server(), store=store, placement="profile")
    rpc = ApprovalRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    task = driver.list_tasks(store, placement="profile", room_id="room-1", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(
        store, placement="profile",
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="worker",
        ttl_seconds=30,
        clock=time.time,
    )
    driver.start_task(
        store,
        task["identity"],
        lease, placement="profile",
        expected_cancel_generation=0,
        clock=time.time,
    )
    task = driver.get_task(store, task["identity"], placement="profile")
    service.runtime._report_pending_action(
        task,
        session_id="ops-session",
        info={
            "pending_approval": {
                "request_id": "approval-1",
                "choices": ["once", "always", "deny"],
            }
        },
    )

    action = service.status("room-1")["pending_actions"][0]
    assert action["member_id"] == "ops"
    assert action["approval"]["choices"] == ["once", "deny"]
    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="ops",
            task_id=task["identity"].task_id,
            execution_generation=1,
            choice="once",
            request_id="wrong-request",
        )

    assert service.approve_room_task(
        "room-1",
        member_id="ops",
        task_id=task["identity"].task_id,
        execution_generation=1,
        choice="once",
        request_id="approval-1",
    ) == {"resolved": 1}
    assert rpc.approvals == [("ops-session", "approval-1", "once")]
    assert service.status("room-1")["pending_actions"] == []

def test_headless_room_publishes_peer_member_reply_without_desktop_transport(
    tmp_path: Path,
    store,
):
    peer = _FakePeerClient()
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-reviewer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        execution_policy_digest="b" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        store=store, placement="profile",
        peer_routes={("room-1", "member-reviewer"): route},
        peer_clients={"install-peer": peer},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default",)
    room = service.create_room(
        room_id="room-1",
        name="Review room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "local",
            },
            {
                "member_id": "member-reviewer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    assert room["members"][1]["target"]["kind"] == "peer"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-peer-1",
        payload={"text": "@reviewer inspect this", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=5.0)

    events = service._events("room-1")
    reply = next(event for event in events if event["kind"] == "message.member")
    assert reply["payload"]["member_id"] == "member-reviewer"
    assert reply["payload"]["text"] == "Remote review complete."
    assert reply["actor"]["connection_id"] == "peer-review"
    assert peer.dispatches[0]["target_profile"] == "reviewer"

def test_unadmitted_peer_failure_does_not_block_next_healthy_member(
    tmp_path: Path,
    store,
):
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        store=store, placement="profile",
        peer_routes={("room-1", "member-peer"): route},
        peer_clients={"install-peer": _UnavailablePeerClient()},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("local",)
    service.create_room(
        room_id="room-1",
        name="Fallback room",
        members=[
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
            {"member_id": "local", "profile": "local", "handle": "local"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-fallback-1",
        payload={"text": "Review this together", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            and event["payload"]["member_id"] == "local"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=5.0)

    events = service._events("room-1")
    assert any(
        event["kind"] == "turn.failed"
        and event["payload"]["member_id"] == "member-peer"
        for event in events
    )
    assert any(
        event["kind"] == "message.member" and event["payload"]["member_id"] == "local"
        for event in events
    )

def test_registered_peer_route_rehydrates_after_service_restart(tmp_path: Path, store):
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            target_profile="default",
            installation_id="install-peer",
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    first = HostedRoomService(_server(), store=store, placement="profile")
    first.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    restarted = HostedRoomService(_server(), store=store, placement="profile")
    restored = restarted.peer_routes[("room-1", "member-peer")]
    assert restored.target_install_id == "install-peer"
    assert restored.target_profile == "reviewer"
    assert restored.grant == "signed.room.grant"
    assert ("room-1", "member-peer") in restarted.peer_clients

def test_one_corrupt_stored_route_does_not_hide_healthy_peers(tmp_path: Path, store):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    service = HostedRoomService(_server(), store=store, placement="profile")
    for room_id, member_id in (("room-good", "member-good"), ("room-bad", "member-bad")):
        route = PeerMemberRoute(
            home_install_id=hosted_rooms.local_authority_gateway_id(),
            member_id=member_id,
            target_install_id="install-peer",
            target_profile="reviewer",
            capability_digest=catalog.catalog_digest,
            cancellation_scope_id=f"cancel-{room_id}",
            trace_id=f"trace-{room_id}",
            grant=f"grant-{room_id}",
        )
        service.register_peer_route(
            room_id=room_id,
            member_id=member_id,
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE hosted_room_links SET target_url=? WHERE room_id=?",
            ("http://public-plaintext.example.test", "room-bad"),
        )

    restarted = HostedRoomService(_server(), store=store, placement="profile")

    assert ("room-good", "member-good") in restarted.peer_routes
    assert ("room-bad", "member-bad") not in restarted.peer_routes
    assert restarted.status()["link_load_error"] == "room-bad:member-bad:invalid"

def test_unpublished_roomlink_v1_route_is_quarantined_for_reinvitation(
    tmp_path: Path,
    store,
):
    legacy_catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            target_profile="default",
            installation_id="install-peer",
            protocol_versions=(1,),
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=legacy_catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="legacy-v1-grant",
    )
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=legacy_catalog,
    )

    restarted = HostedRoomService(_server(), store=store, placement="profile")

    assert restarted.peer_routes == {}
    assert restarted.status()["link_load_error"] == (
        "room-1:member-peer:protocol-upgrade-required"
    )

def test_peer_member_without_route_fails_closed_instead_of_running_locally(
    tmp_path: Path,
    store,
):
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    with pytest.raises(RuntimeError, match="route is unavailable"):
        service._resolve_member_transport(
            service.bindings()[0],
            {
                "payload": {
                    "target_member_id": "member-peer",
                    "target_profile": "reviewer",
                    "source_event_seq": 9,
                }
            },
        )

def test_registration_disk_failure_does_not_publish_live_route(
    tmp_path: Path, monkeypatch, store
):
    service = HostedRoomService(_server(), store=store, placement="profile")
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    monkeypatch.setattr(
        "gateway.hosted_room_links.save_room_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        service.register_peer_route(
            room_id="room-1",
            member_id="member-peer",
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )
    assert ("room-1", "member-peer") not in service.peer_routes
    assert "install-peer" not in service.peer_clients

def test_room_route_revocation_is_remote_first_and_removes_local_state(
    tmp_path: Path,
    store,
):
    from gateway import hosted_room_links

    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _FakePeerClient()
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    assert service.revoke_room_routes("room-1") == 1
    assert peer.revoked == ["signed.room.grant"]
    assert ("room-1", "member-peer") not in service.peer_routes
    assert hosted_room_links.load_room_links(store, placement="profile") == ()

def test_failed_remote_revocation_preserves_route_for_retry(tmp_path: Path, store):
    from gateway import hosted_room_links

    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_UnavailableRevokePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    with pytest.raises(RuntimeError, match="offline during revocation"):
        service.revoke_room_routes("room-1")
    assert ("room-1", "member-peer") in service.peer_routes
    assert len(hosted_room_links.load_room_links(store, placement="profile")) == 1

def test_expired_remote_grant_no_longer_blocks_room_cleanup(tmp_path: Path, store):
    from gateway import hosted_room_links

    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        execution_policy_digest=catalog.execution_policy.policy_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="expired.room.grant",
    )
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_ExpiredRevokePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    assert service.revoke_room_routes("room-1") == 1
    assert ("room-1", "member-peer") not in service.peer_routes
    assert hosted_room_links.load_room_links(store, placement="profile") == ()

def test_expired_grant_surfaces_needs_reauthorization_without_secret(
    tmp_path: Path,
    store,
):
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_ExpiredGrantPeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 3,
            }
        },
    )
    with pytest.raises(PeerRunsHTTPError):
        transport.resolve_exact(
            profile="reviewer",
            title="Group: room-1",
            source="bot_room",
        )
    status = service.status("room-1")
    assert status["peer_routes"] == [
        {
            "room_id": "room-1",
            "member_id": "member-peer",
            "status": "needs_reauthorization",
        }
    ]
    assert "signed.room.grant" not in repr(status)
    restarted = HostedRoomService(_server(), store=store, placement="profile")
    assert restarted.status("room-1")["peer_routes"] == status["peer_routes"]

    rotated = PeerMemberRoute(
        home_install_id=route.home_install_id,
        member_id=route.member_id,
        target_install_id=route.target_install_id,
        target_profile=route.target_profile,
        capability_digest=route.capability_digest,
        cancellation_scope_id=route.cancellation_scope_id,
        trace_id="trace-room-rotated",
        grant="rotated.room.grant",
    )
    restarted.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=rotated,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "ready"
    after_rotation = HostedRoomService(_server(), store=store, placement="profile")
    assert after_rotation.peer_routes[("room-1", "member-peer")].grant == (
        "rotated.room.grant"
    )

def test_not_admitted_dispatch_persists_unavailable_route_until_success(
    tmp_path: Path,
    store,
):
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _NotAdmittedPeerClient()
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    binding = service.bindings()[0]
    task = {
        "identity": HostedTaskIdentity(
            "room-1", "task-peer", "thread-1", "turn-1"
        ),
        "execution_generation": 1,
        "payload": {
            "target_member_id": "member-peer",
            "target_profile": "reviewer",
            "source_event_seq": 3,
        },
    }
    transport = service._resolve_member_transport(binding, task)
    session = transport.create(
        profile="reviewer",
        title="Group: room-1",
        source="bot_room",
    )

    with pytest.raises(PeerRunsHTTPError) as caught:
        transport.submit(
            profile="reviewer",
            session_id=session["session_id"],
            prompt="Review the queued task.",
            source="bot_room",
            task=task["identity"],
            execution_generation=1,
            on_terminal=lambda _receipt: None,
        )

    assert caught.value.not_admitted is True
    assert service.status("room-1")["peer_routes"][0]["status"] == "unavailable"
    restarted = HostedRoomService(_server(), store=store, placement="profile")
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "unavailable"

    peer.offline = False
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review the queued task.",
        source="bot_room",
        task=task["identity"],
        execution_generation=2,
        on_terminal=lambda _receipt: None,
    )
    assert service.status("room-1")["peer_routes"][0]["status"] == "ready"

def test_dispatch_refresh_persists_before_remote_admission(tmp_path: Path, store):
    now = time.time()
    secret = b"s" * 32
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    old_grant = issue_room_grant(
        secret,
        grant_id="grant-old",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        execution_policy_digest=catalog.execution_policy.policy_digest,
        issued_at=now - 3700,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    new_grant = issue_room_grant(
        secret,
        grant_id="grant-new",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        execution_policy_digest=catalog.execution_policy.policy_digest,
        issued_at=now,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant=old_grant,
    )
    peer = _RefreshingPeerClient(new_grant)
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = HostedTaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 11,
            },
        },
    )
    session = transport.create(
        profile="reviewer", title="Group: room-1", source="bot_room"
    )
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review this",
        source="bot_room",
        task=identity,
        execution_generation=1,
        on_terminal=lambda _receipt: None,
    )
    assert peer.refreshed == [old_grant]
    assert peer.refresh_arguments == [
        {
            "grant": old_grant,
            "capability_digest": catalog.catalog_digest,
            "execution_policy_digest": catalog.execution_policy.policy_digest,
        }
    ]
    assert peer.dispatched_grants == [new_grant]
    assert peer.dispatches[0]["capability_digest"] == catalog.catalog_digest
    assert (
        peer.dispatches[0]["execution_policy_digest"]
        == catalog.execution_policy.policy_digest
    )
    assert HostedRoomService(_server(), store=store, placement="profile").peer_routes[
        ("room-1", "member-peer")
    ].grant == new_grant


@pytest.mark.parametrize(
    ("capability_changed", "policy_changed"),
    [(False, True), (True, False), (True, True)],
)
def test_dispatch_refresh_marks_route_for_reauthorization_on_drift(
    capability_changed,
    policy_changed,
):
    from gateway.hosted_room_execution_policy import execution_policy_mapping

    base_policy = execution_policy_mapping(
        target_profile="reviewer",
        config={"agent": {"max_turns": 20}},
    )
    changed_policy = execution_policy_mapping(
        target_profile="reviewer",
        config={"agent": {"max_turns": 21}},
    )
    base_catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            target_profile="reviewer",
            installation_id="install-peer",
            persistent_process=True,
            execution_policy=base_policy,
        )
    )
    refreshed_catalog = catalog_mapping(
            target_profile="reviewer",
        installation_id="install-peer",
        persistent_process=True,
        attachments=capability_changed,
        execution_policy=changed_policy if policy_changed else base_policy,
    )
    now = time.time()
    grant = issue_room_grant(
        b"s" * 32,
        grant_id="grant-old",
        room_id="room-1",
        home_install_id="install-home",
        authority_gateway_id="install-home",
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        execution_policy_digest=base_catalog.execution_policy.policy_digest,
        issued_at=now - 3500,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    prompt = "Review this"
    dispatch = HostedMemberDispatch(
        protocol_version=PROTOCOL_VERSION,
        room_id="room-1",
        home_install_id="install-home",
        authority_gateway_id="install-home",
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        task_id="task-1",
        execution_generation=1,
        source_event_seq=1,
        cancellation_scope_id="cancel-room-1",
        prompt=prompt,
        prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
        capability_digest=base_catalog.catalog_digest,
        execution_policy_digest=base_catalog.execution_policy.policy_digest,
        trace_id="trace-room-1",
    )
    peer = _RefreshingPeerClient(
        "replacement.room.grant",
        catalog=refreshed_catalog,
    )
    reauthorization = []
    refreshed = []
    tracked = _RouteStatusPeerClient(
        peer,
        on_ready=lambda: None,
        on_reauthorization=lambda: reauthorization.append(True),
        on_unavailable=lambda: None,
        on_refreshed=lambda *args: refreshed.append(args),
    )

    with pytest.raises(PeerRunsHTTPError) as caught:
        tracked.dispatch(dispatch=dispatch.as_mapping(), grant=grant)

    assert caught.value.needs_reauthorization is True
    assert reauthorization == [True]
    assert refreshed == []
    assert peer.dispatches == []

def test_peer_approval_is_scoped_visible_and_resolvable(tmp_path: Path, monkeypatch, store):
    _mock_task_reads(monkeypatch, [{
        "identity": HostedTaskIdentity("room-1", "task-1", "thread-1", "turn-1"),
        "execution_generation": 2,
        "status": "running",
        "payload": {"target_member_id": "member-peer", "target_profile": "reviewer"},
    }])
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _ApprovalPeerClient()
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "hermes",
            },
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            }
        ],
    )
    identity = HostedTaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 2,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 1,
            },
        },
    )

    status = transport.info(
        profile="reviewer",
        session_id="peer-group-session",
        source="bot_room",
    )
    assert status["status"] == "waiting_for_approval"
    service._set_pending_action(
        "room-1",
        "member-peer",
        {
            "kind": "approval",
            "task_id": status["task_id"],
            "execution_generation": status["execution_generation"],
            "run_id": status["run_id"],
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": status["approval"],
        },
    )
    pending = service.status("room-1")["pending_actions"]
    assert len(pending) == 1
    assert pending[0]["created_at"] > 0
    assert {key: value for key, value in pending[0].items() if key != "created_at"} == {
            "kind": "approval",
            "task_id": "task-1",
            "execution_generation": 2,
            "run_id": "run-peer-1",
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": {
                "description": "Run the focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
            "member_id": "member-peer",
        }

    assert service.approve_room_task(
        "room-1",
        member_id="member-peer",
        task_id="task-1",
        execution_generation=2,
        choice="once",
        request_id="req-peer-1",
    ) == {"resolved": 1}
    assert peer.approvals == [
        {
            "task_id": "task-1",
            "execution_generation": 2,
            "request_id": "req-peer-1",
            "choice": "once",
            "grant": "signed.room.grant",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []

def test_local_room_approval_uses_the_exact_hidden_session(tmp_path: Path, monkeypatch, store):
    service = HostedRoomService(_server(), store=store, placement="profile")
    _mock_task_reads(monkeypatch, [{
        "identity": HostedTaskIdentity("room-1", "task-local-1", "thread-1", "turn-1"),
        "execution_generation": 1, "status": "running", "payload": {"target_profile": "local"}}])
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service._set_pending_action(
        "room-1",
        "local",
        {
            "kind": "approval",
            "task_id": "task-local-1",
            "execution_generation": 1,
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "approval": {
                "description": "Run focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
        },
    )

    assert service.approve_room_task(
        "room-1",
        member_id="local",
        task_id="task-local-1",
        execution_generation=1,
        choice="once",
        request_id="approval-local-1",
    ) == {"resolved": 1}
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []

def test_pending_local_approval_survives_restart_and_generic_action_relays(
    tmp_path: Path, monkeypatch, store,
):
    first = HostedRoomService(_server(), store=store, placement="profile")
    first._set_pending_action(
        "room-1",
        "local",
        {
            "kind": "approval",
            "task_id": "task-local-1",
            "execution_generation": 3,
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "approval": {"choices": ["once", "deny"]},
        },
    )

    restarted = HostedRoomService(_server(), store=store, placement="profile")
    rpc = _FakeRPC()
    restarted.rpc = rpc
    restarted.runtime.rpc = rpc
    _mock_task_reads(monkeypatch, [{
        "identity": HostedTaskIdentity("room-1", "task-local-1", "thread-1", "turn-1"),
        "execution_generation": 3, "status": "running", "payload": {"target_profile": "local"}}])
    dashboard_action = restarted.pending_actions("room-1")["actions"][0]
    assert dashboard_action["action_id"] == "approval-local-1"
    assert dashboard_action["kind"] == "permission"
    assert dashboard_action["description"] == ""
    assert dashboard_action["detail"]["tool_name"] == "terminal"
    assert dashboard_action["detail"]["scope"] == "once"
    assert dashboard_action["created_at"] > 0

    result = restarted.handle_action(
        "room-1", "approval-local-1", "approve"
    )

    assert result["ok"] is True
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert restarted.pending_actions("room-1")["actions"] == []
    after_resolution = HostedRoomService(_server(), store=store, placement="profile")
    assert after_resolution.pending_actions("room-1")["actions"] == []

def test_stale_local_approval_cannot_resolve_replacement_request(tmp_path: Path, monkeypatch, store):
    service = HostedRoomService(_server(), store=store, placement="profile")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    _mock_task_reads(monkeypatch, [{
        "identity": HostedTaskIdentity("room-1", "task-local-1", "thread-1", "turn-1"),
        "execution_generation": 1, "status": "running", "payload": {"target_profile": "local"}}])
    action = {
        "kind": "approval",
        "task_id": "task-local-1",
        "execution_generation": 1,
        "session_id": "local-session",
        "approval": {"choices": ["once", "deny"]},
    }
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-A"}
    )
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-B"}
    )

    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="local",
            task_id="task-local-1",
            execution_generation=1,
            choice="once",
            request_id="approval-A",
        )

    assert rpc.approvals == []
    assert service.status("room-1")["pending_actions"][0]["request_id"] == (
        "approval-B"
    )

def test_peer_recovery_replays_the_same_execution_generation(tmp_path: Path, store):
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(target_profile="default", installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _RecoveringPeerClient()
    service = HostedRoomService(_server(), store=store, placement="profile")
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = HostedTaskIdentity("room-1", "task-1", "thread-1", "turn-1")

    service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "status": "indeterminate",
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 9,
                "prompt": "Recover the accepted review.",
            },
        },
    )

    assert len(peer.recoveries) == 1
    recovered = peer.recoveries[0]["dispatch"]
    assert recovered["task_id"] == "task-1"
    assert recovered["execution_generation"] == 1
    assert recovered["prompt"] == "Recover the accepted review."
