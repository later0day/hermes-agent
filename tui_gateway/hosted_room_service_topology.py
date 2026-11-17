"""Operator-facing topology and policy-trace projections for hosted rooms."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms

logger = logging.getLogger(__name__)

_ROOM_READ_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error)
_ROLE_NAMES = {
    discussion.DECIDER_ROLE: "team_lead",
    discussion.WORKER_ROLE: "teammate",
}
_ROLE_ORDER = {"coordinator": 0, "team_lead": 1, "teammate": 2, "observer": 3}


def _observer_details(service: Any, room_id: str, _member_id: str, _handle: str) -> dict[str, Any]:
    return {
        "observer_state": _derive_observer_state(service, room_id),
        "activity_level": None,
    }


def _teammate_details(service: Any, room_id: str, member_id: str, handle: str) -> dict[str, Any]:
    details: dict[str, Any] = {
        "activity_level": _derive_activity_level(service, room_id, member_id),
    }
    task = _current_task_for_member(service, room_id, handle)
    if task:
        details["current_task"] = task.get("subject")
        details["current_task_id"] = task.get("task_id")
    return details


_ROLE_DETAILS = {
    "observer": _observer_details,
    "teammate": _teammate_details,
}


def _current_discussion_position(service: Any, room_id: str) -> tuple[int | None, int | None, int | None]:
    current_turn = None
    current_round = None
    max_rounds = None
    try:
        tasks = driver.list_tasks(service.db_path, room_id=room_id)
        for task in tasks:
            if task.get("status") in {"running", "queued"}:
                identity = task.get("identity")
                if identity and hasattr(identity, "turn_id"):
                    parsed = discussion._TURN_ID_RE.match(identity.turn_id)
                    if parsed:
                        current_turn = int(parsed.group("turn"))
                        current_round = int(parsed.group("round"))
                break
        max_rounds = getattr(discussion, "MAX_DISCUSSION_ROUNDS", 5)
    except _ROOM_READ_ERRORS:
        logger.warning("hosted room %s: reading the current discussion position failed", room_id, exc_info=True)
    return current_turn, current_round, max_rounds


def topology(service: Any, room_id: str) -> dict[str, Any] | None:
    """Return team topology: members with roles, states, and current tasks."""
    room = hosted_rooms.room_state(service.db_path, room_id=room_id)
    if room is None:
        return None
    members = room.get("members") or []
    if not members:
        return None

    member_roles: list[dict[str, Any]] = []
    leader_ids = {"coordinator": None, "team_lead": None}
    for member in members:
        handle = str(member.get("handle") or "")
        member_id = str(member.get("member_id") or "")
        stored_role = str(member.get("role") or "worker")
        role = _ROLE_NAMES.get(stored_role, stored_role)
        entry: dict[str, Any] = {
            "member_id": member_id,
            "handle": handle,
            "profile": str(member.get("profile") or ""),
            "role": role,
            "observer_state": None,
            "activity_level": None,
            "current_task": None,
            "current_task_id": None,
        }
        if role in leader_ids:
            leader_ids[role] = member_id
        detail_builder = _ROLE_DETAILS.get(role)
        if detail_builder is not None:
            entry.update(detail_builder(service, room_id, member_id, handle))
        member_roles.append(entry)

    member_roles.sort(key=lambda member: _ROLE_ORDER.get(str(member["role"]), 99))
    current_turn, current_round, max_rounds = _current_discussion_position(service, room_id)
    return {
        "room_id": room_id,
        "members": member_roles,
        "coordinator_id": leader_ids["coordinator"],
        "team_lead_id": leader_ids["team_lead"],
        "current_turn": current_turn,
        "current_round": current_round,
        "max_rounds": max_rounds,
    }


def _derive_observer_state(service: Any, room_id: str) -> str | None:
    """Derive observer state-machine state from driver / policy."""
    with service._policy_lock, service._state_lock:
        for (action_room_id, _member_id), action in service._pending_actions.items():
            if action_room_id == room_id and action.get("kind") == "observer":
                return "armed"
    try:
        room = hosted_rooms.room_state(service.db_path, room_id=room_id)
        if room:
            has_observer = any(
                str(member.get("role") or "") == "observer"
                for member in (room.get("members") or [])
            )
            if has_observer:
                return "armed"
    except _ROOM_READ_ERRORS:
        logger.warning("hosted room %s: reading observer state failed", room_id, exc_info=True)
    return None


def _derive_activity_level(service: Any, room_id: str, member_id: str) -> int | None:
    """Derive a 0-100 activity level for a teammate."""
    try:
        tasks = driver.list_tasks(service.db_path, room_id=room_id)
        member_tasks = [
            task for task in tasks
            if task.get("identity") and getattr(task["identity"], "member_id", None) == member_id
        ]
        total = len(tasks)
        if total == 0:
            return None
        completed = sum(1 for task in member_tasks if task.get("status") == "settled")
        return int((completed / max(total, 1)) * 100)
    except _ROOM_READ_ERRORS:
        logger.warning(
            "hosted room %s: reading activity for member %s failed", room_id, member_id, exc_info=True)
        return None


def _current_task_for_member(service: Any, room_id: str, handle: str) -> dict[str, Any] | None:
    """Return the current in-progress task for a member from the C3 DAG."""
    from gateway.room_task_dag import current_task_for_owner

    try:
        return current_task_for_owner(service.db_path, room_id=room_id, owner=handle)
    except _ROOM_READ_ERRORS:
        logger.warning("hosted room %s: reading the current task for %s failed", room_id, handle, exc_info=True)
        return None


def policy_trace(service: Any, room_id: str) -> dict[str, Any]:
    """Return policy checkpoint snapshot for a room."""
    try:
        room = hosted_rooms.room_state(service.db_path, room_id=room_id)
        latest_seq = int(room.get("latest_seq") or 0) if room else 0
        snapshot = service.policy_checkpoint.snapshot(room_id=room_id, latest_seq=latest_seq)
        return {
            "room_id": room_id,
            "through_seq": snapshot.through_seq,
            "stopped_through_seq": snapshot.stopped_through_seq,
            "event_count": len(snapshot.events),
            "events": [
                {
                    "seq": event.get("seq"),
                    "kind": event.get("kind"),
                    "actor": event.get("actor"),
                    "created_at": event.get("created_at"),
                }
                for event in snapshot.events
            ],
            "watermarks": {
                f"{key[0]}::{key[1]}": value
                for key, value in (snapshot.watermarks or {}).items()
            },
        }
    except _ROOM_READ_ERRORS:
        logger.warning("hosted room %s: reading the policy trace failed", room_id, exc_info=True)
        return {"room_id": room_id, "error": "policy trace unavailable"}
