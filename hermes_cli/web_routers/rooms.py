"""Hosted-room dashboard routes (fork: Rooms / agent team).

Read-only inspector over the hosted-room store and the process-owned
HostedRoomService: roster, authority/driver status and event replay. The whole
``/api/`` tree is gated by the dashboard auth middleware; every payload here is
non-secret (members carry no tokens, peer-route status is a classification).
"""

import asyncio

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/rooms")
async def list_hosted_rooms(all: bool = False):
    """Return one bounded page of hosted rooms for the inspector list."""

    def _run():
        from gateway.hosted_rooms import default_db_path, list_rooms

        rooms = list_rooms(default_db_path(), include_disbanded=bool(all))
        return {
            "rooms": [
                {
                    "room_id": r.get("room_id"),
                    "name": r.get("name"),
                    "members": r.get("members") or [],
                    "revision": r.get("revision", 0),
                    "latest_seq": r.get("latest_seq", 0),
                    "authority_epoch": r.get("authority_epoch", 0),
                    "authority_gateway_id": r.get("authority_gateway_id"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "disbanded_at": r.get("disbanded_at"),
                }
                for r in rooms
            ]
        }

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}")
async def get_hosted_room(room_id: str):
    """Return one room's replay/authority state plus live driver status."""

    def _run():
        from gateway.hosted_rooms import (
            HostedRoomError,
            RoomNotFoundError,
            default_db_path,
            room_state,
        )
        from tui_gateway.methods_groups import get_hosted_room_service

        try:
            room = room_state(
                default_db_path(), room_id=room_id, include_disbanded=True
            )
        except RoomNotFoundError:
            raise HTTPException(status_code=404, detail="room not found")
        except HostedRoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        driver_status = None
        service = get_hosted_room_service()
        if service is not None and room.get("disbanded_at") is None:
            try:
                driver_status = service.status(str(room.get("room_id")))
            except Exception:
                driver_status = None
        return {"room": room, "driver_status": driver_status}

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/log")
async def get_hosted_room_log(room_id: str, since_seq: int = 0, limit: int = 100):
    """Return a bounded, monotonic slice of the append-only room event log."""

    def _run():
        from gateway.hosted_rooms import (
            HostedRoomError,
            RoomNotFoundError,
            default_db_path,
            read_events,
        )

        try:
            return read_events(
                default_db_path(),
                room_id=room_id,
                since_seq=since_seq,
                limit=limit,
                include_disbanded=True,
            )
        except RoomNotFoundError:
            raise HTTPException(status_code=404, detail="room not found")
        except HostedRoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return await asyncio.to_thread(_run)


# -- Agent-team inspector: topology, pending actions, mailboxes, observer, peer grants, replication, policy trace.

@router.get("/api/rooms/{room_id}/topology")
async def get_room_topology(room_id: str):
    """Return the team topology with member roles, statuses, and current tasks."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        try:
            topology = service.topology(room_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if topology is None:
            raise HTTPException(status_code=404, detail="room not found")
        return topology

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/pending-actions")
async def get_room_pending_actions(room_id: str):
    """Return pending actions (permission/plan/shutdown) for a room."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.pending_actions(room_id)

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/pending-actions/{action_id}/approve")
async def approve_room_action(room_id: str, action_id: str):
    """Approve a pending action (permission, plan, or shutdown)."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.handle_action(room_id, action_id, "approve")

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/pending-actions/{action_id}/deny")
async def deny_room_action(room_id: str, action_id: str):
    """Deny a pending action."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.handle_action(room_id, action_id, "deny")

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/members/{member_id}/mailbox")
async def get_member_mailbox(room_id: str, member_id: str):
    """Return mailbox messages for a member."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.mailbox(room_id, member_id)

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/members/{member_id}/mailbox/read")
async def mark_mailbox_read(room_id: str, member_id: str):
    """Mark all messages in a member's mailbox as read."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.mark_mailbox_read(room_id, member_id)

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/observer")
async def get_observer_status(room_id: str):
    """Return observer state machine status for a room."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.observer_status(room_id)

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/observer/pause")
async def pause_observer(room_id: str):
    """Pause the observer for a room."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.pause_observer(room_id)

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/observer/resume")
async def resume_observer(room_id: str):
    """Resume the observer for a room."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.resume_observer(room_id)

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/peer-grants")
async def get_room_peer_grants(room_id: str):
    """Return peer route grants with status and catalog info."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.peer_grants(room_id)

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/replication-health")
async def get_room_replication_health(room_id: str):
    """Return replication health derived from peer route statuses."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.replication_health(room_id)

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/policy-trace")
async def get_room_policy_trace(room_id: str):
    """Return policy checkpoint snapshot for a room."""

    def _run():
        from tui_gateway.methods_groups import get_hosted_room_service
        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        return service.policy_trace(room_id)

    return await asyncio.to_thread(_run)
