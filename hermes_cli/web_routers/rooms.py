"""Hosted-room dashboard routes (fork: Rooms / agent team).

Read-only inspector over the hosted-room store and the process-owned
HostedRoomService: roster, authority/driver status and event replay. The whole
``/api/`` tree is gated by the dashboard auth middleware; every payload here is
non-secret (members carry no tokens, peer-route status is a classification).
"""

import asyncio
import logging
import re
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("hermes_cli.web_server")
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


def _room_store():
    from gateway.agent_room_store import AgentRoomStore

    return AgentRoomStore()


# Pending plans are process-local and keyed by the authenticated dashboard
# session. A server restart simply requires the user to plan again.
_pending_room_plans: dict[str, object] = {}


class _RoomPlanRequest(BaseModel):
    requirement: str
    max_members: int = 5


class _RoomPlanConfirmRequest(BaseModel):
    room_name: Optional[str] = None


def _current_session_token(request: Request) -> str:
    from hermes_cli.web_server import _SESSION_HEADER_NAME

    return request.headers.get(_SESSION_HEADER_NAME, "anonymous")


def _rollback_created_profiles(created_profiles: list[str]) -> None:
    from hermes_cli.profiles import delete_profile

    for created in created_profiles:
        try:
            delete_profile(created, yes=True)
        except (OSError, RuntimeError, ValueError):
            logger.exception("room plan confirm: rollback of profile %s failed", created)


@router.post("/api/rooms/plan")
async def post_rooms_plan(request: Request, body: _RoomPlanRequest):
    """Plan a room from a natural-language requirement for later confirmation."""
    from hermes_cli.profiles import get_profile_dir, list_profiles, read_profile_meta
    from gateway.agent_room_planner import plan_room

    try:
        all_profiles = list_profiles()
    except OSError:
        logger.debug("room plan: profile enumeration failed", exc_info=True)
        all_profiles = []

    roster: list[tuple[str, str]] = []
    for profile in all_profiles:
        if profile.name == "default":
            continue
        meta = read_profile_meta(get_profile_dir(profile.name))
        description = str(meta.get("description") or "")
        roster.append((profile.name, description))

    plan = plan_room(
        body.requirement,
        roster,
        max_members=min(body.max_members, 5),
    )
    if not plan.is_actionable:
        raise HTTPException(status_code=422, detail=f"Planning failed: {plan.rationale}")

    _pending_room_plans[_current_session_token(request)] = plan
    return {"plan": plan.to_dict()}


@router.post("/api/rooms/plan/confirm")
async def post_rooms_plan_confirm(request: Request, body: _RoomPlanConfirmRequest):
    """Create the new profiles, observer, and room in the pending plan."""
    from hermes_cli.profiles import (
        get_profile_dir,
        profile_exists,
        write_profile_meta,
    )
    from hermes_cli.profile_provision import ProfileCreateRequest, provision_profile
    from gateway.agent_room_bootstrapper import (
        BootstrapError,
        build_observer_profile,
        observer_profile_name_for,
        teardown_observer_profile,
    )
    from gateway.agent_room_planner import RoomPlan
    from gateway.agent_room_store import AgentRoomError

    plan = _pending_room_plans.pop(_current_session_token(request), None)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail="No pending room plan. Call POST /api/rooms/plan first.",
        )
    assert isinstance(plan, RoomPlan)
    if not plan.is_actionable:
        raise HTTPException(status_code=422, detail="Pending plan is not actionable.")

    created_profiles: list[str] = []
    for member in plan.new_profiles:
        try:
            provision_profile(ProfileCreateRequest(
                name=member.name,
                clone_from=None,
                clone_all=False,
                clone_config=False,
                clone_env=False,
                clone_skills=False,
                alias=False,
                no_skills=True,
                mirror_credentials=False,
            ))
            write_profile_meta(
                get_profile_dir(member.name),
                description=member.description,
                description_auto=True,
            )
            created_profiles.append(member.name)
        except (OSError, ValueError) as exc:
            logger.warning("room plan confirm: create profile %s failed: %s", member.name, exc)
            _rollback_created_profiles(created_profiles)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create profile '{member.name}': {exc}",
            )

    room_name = body.room_name or ""
    if not room_name:
        room_name = plan.room_description[:30].replace(" ", "_").lower()
        room_name = re.sub(r"[^a-z0-9_-]", "", room_name) or "planned_room"

    store = _room_store()
    try:
        if store.get_room_by_name(room_name) is not None:
            room_name = f"{room_name}_{uuid.uuid4().hex[:6]}"

        member_names = [m.profile if not m.is_new else m.name for m in plan.members]
        members_with_desc = [
            (m.profile if not m.is_new else m.name, m.description) for m in plan.members
        ]
        observer_name = observer_profile_name_for(room_name)
        if profile_exists(observer_name):
            _rollback_created_profiles(created_profiles)
            raise HTTPException(
                status_code=409,
                detail=f"Observer profile '{observer_name}' already exists.",
            )

        try:
            build_observer_profile(
                room_name=room_name,
                room_description=plan.room_description,
                members=members_with_desc,
                default_member=member_names[0],
                observer_name=observer_name,
            )
        except BootstrapError as exc:
            _rollback_created_profiles(created_profiles)
            raise HTTPException(status_code=500, detail=str(exc))

        room_id = f"room_{uuid.uuid4().hex[:12]}"
        try:
            room = store.create_room(
                room_id,
                room_name,
                observer_profile=observer_name,
                members=member_names,
                description=plan.room_description,
                default_member=member_names[0],
                actor="dashboard",
            )
        except AgentRoomError as exc:
            try:
                teardown_observer_profile(observer_name)
            except (OSError, RuntimeError, ValueError):
                logger.exception("room plan confirm: observer profile rollback failed")
            _rollback_created_profiles(created_profiles)
            raise HTTPException(status_code=400, detail=str(exc))

        return {"ok": True, "room": room.to_dict(), "new_profiles": created_profiles}
    finally:
        store.close()


@router.post("/api/rooms")
async def create_hosted_room(body: RoomCreateInput):
    """Create a persistent 2-6 member team through HostedRoomService."""

    def _run():
        from gateway.hosted_room_discussion import DiscussionValidationError
        from persistence.gateway.hosted_rooms import HostedRoomError, RoomConflictError, RoomNotFoundError
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
        from persistence.gateway.hosted_rooms import HostedRoomError, RoomConflictError, RoomNotFoundError
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
        from tui_gateway.methods_groups import _active_store

        db_path = default_db_path()
        store = _active_store()
        enriched = []
        for room in list_rooms(store, placement="shared", include_disbanded=bool(all)):
            try:
                workspace = build_room_workspace(
                    store, placement="shared", db_path=db_path, room_id=str(room["room_id"]))
                enriched.append(room_summary(room, tasks=workspace["tasks"], actions=workspace["pending_actions"]))
            except Exception:
                # One malformed sidecar must not hide the entire Room inbox.
                logger.exception("failed to build workspace summary for Room %s", room.get("room_id"))
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
            room_state,
        )
        from tui_gateway.methods_groups import _active_store, get_hosted_room_service

        try:
            room = room_state(
                _active_store(), placement="shared", room_id=room_id, include_disbanded=True
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
                logger.exception("failed to load driver status for Room %s", room_id)
                driver_status = None
        return {"room": room, "driver_status": driver_status}

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/workspace")
async def get_hosted_room_workspace(room_id: str):
    """Return the bounded durable task, attempt, conversation, and activity view."""

    def _run():
        from gateway.hosted_room_views import build_room_workspace
        from gateway.hosted_rooms import HostedRoomError, RoomNotFoundError, default_db_path
        from tui_gateway.methods_groups import _active_store

        try:
            return build_room_workspace(
                _active_store(), placement="shared", db_path=default_db_path(), room_id=room_id)
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
            read_events,
        )
        from tui_gateway.methods_groups import _active_store

        try:
            return read_events(
                _active_store(),
                placement="shared",
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
            logger.exception("failed to load topology for Room %s", room_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
