"""Hosted-room dashboard routes (fork: Rooms / agent team).

Read-only inspector over the hosted-room store and the process-owned
HostedRoomService: roster, authority/driver status and event replay. The whole
``/api/`` tree is gated by the dashboard auth middleware; every payload here is
non-secret (members carry no tokens, peer-route status is a classification).
"""

import asyncio
from typing import List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_routers._common import log as _log

router = APIRouter()


# -- Team workspace mutations: thin adapters over HostedRoomService (same narrow waist as groups.* RPC). The
# dashboard does not recreate chat; it only admits team setup and explicit user work requests into the durable
# Room scheduler.

class RoomMemberInput(BaseModel):
    profile: str
    handle: str
    display_name: str = ""
    role: Literal["worker", "decider"] = "worker"


class RoomCreateInput(BaseModel):
    room_id: str
    name: str
    members: list[RoomMemberInput]


class RoomMessageInput(BaseModel):
    text: str
    recipient: str
    event_id: str
    thread_id: str


def _room_member_payload(member: RoomMemberInput, index: int) -> dict:
    return {
        "member_id": f"member-{index + 1}-{member.handle}",
        "profile": member.profile,
        "handle": member.handle,
        "display_name": member.display_name,
        "role": member.role,
    }


@router.post("/api/rooms")
async def create_hosted_room(body: RoomCreateInput):
    """Create a persistent 2-6 member team through HostedRoomService."""

    def _run():
        from gateway.hosted_room_discussion import DiscussionValidationError
        from gateway.hosted_rooms import HostedRoomError, RoomConflictError, RoomNotFoundError
        from tui_gateway.methods_groups import get_hosted_room_service

        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        try:
            room = service.create_room(
                room_id=body.room_id,
                name=body.name,
                members=[_room_member_payload(member, index) for index, member in enumerate(body.members)],
            )
            return {"room": room}
        except RoomNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RoomConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (HostedRoomError, DiscussionValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return await asyncio.to_thread(_run)


@router.post("/api/rooms/{room_id}/messages")
async def send_hosted_room_message(room_id: str, body: RoomMessageInput):
    """Admit one explicit user work request into the Room scheduler."""

    def _run():
        from gateway.hosted_room_discussion import DiscussionValidationError
        from gateway.hosted_rooms import HostedRoomError, RoomConflictError, RoomNotFoundError
        from tui_gateway.methods_groups import get_hosted_room_service

        service = get_hosted_room_service()
        if service is None:
            raise HTTPException(status_code=503, detail="room service not available")
        recipient = body.recipient.strip().lstrip("@")
        text = body.text.strip()
        if not recipient:
            raise HTTPException(status_code=400, detail="recipient is required")
        if not text:
            raise HTTPException(status_code=400, detail="task text is required")
        try:
            event = service.send(
                room_id=room_id,
                event_id=body.event_id,
                payload={"text": f"@{recipient} {text}", "thread_id": body.thread_id},
                recipient=recipient,
            )
            return {"event": event, "accepted": True}
        except RoomNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RoomConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (HostedRoomError, DiscussionValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return await asyncio.to_thread(_run)


@router.get("/api/rooms")
async def list_hosted_rooms(all: bool = False):
    """Return one bounded page of Rooms enriched with durable workspace summaries."""

    def _run():
        from gateway.hosted_room_views import build_room_workspace, room_summary
        from gateway.hosted_rooms import default_db_path, list_rooms

        db_path = default_db_path()
        enriched = []
        for room in list_rooms(db_path, include_disbanded=bool(all)):
            try:
                workspace = build_room_workspace(db_path, room_id=str(room["room_id"]))
                enriched.append(room_summary(room, tasks=workspace["tasks"], actions=workspace["pending_actions"]))
            except Exception:
                # One malformed sidecar must not hide the entire Room inbox.
                _log.exception("failed to build workspace summary for Room %s", room.get("room_id"))
                enriched.append({**room, "workspace": None})
        return {"rooms": enriched}

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


@router.get("/api/rooms/{room_id}/workspace")
async def get_hosted_room_workspace(room_id: str):
    """Return the bounded durable task, attempt, conversation, and activity view."""

    def _run():
        from gateway.hosted_room_views import build_room_workspace
        from gateway.hosted_rooms import HostedRoomError, RoomNotFoundError, default_db_path

        try:
            return build_room_workspace(default_db_path(), room_id=room_id)
        except RoomNotFoundError:
            raise HTTPException(status_code=404, detail="room not found")
        except HostedRoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

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
