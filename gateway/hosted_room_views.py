"""Dashboard read model for the hosted Room task-first workspace.

This module joins the two durable task views without owning state: the shared
manual DAG describes user-visible work, while the driver ledger describes exact
turn attempts and generations.  Room events remain the authoritative history.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway import room_task_dag as dag
from gateway.hosted_room_actions import load_pending_actions


def _identity_dict(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    return {
        key: str(getattr(value, key))
        for key in ("room_id", "task_id", "thread_id", "turn_id")
        if getattr(value, key, None) is not None
    }


_ATTEMPT_METADATA = {
    "status",
    "execution_generation",
    "cancel_generation",
    "created_at",
    "updated_at",
    "started_at",
    "terminal_at",
    "indeterminate_at",
    "settlement_status",
    "error",
    "terminal_reason",
}


def _serialise_driver_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Project only operator-safe attempt metadata, never prompts or tool payloads."""

    projected = {key: task.get(key) for key in _ATTEMPT_METADATA if key in task}
    return {
        **projected,
        "identity": _identity_dict(task.get("identity")),
        "redacted": True,
    }


def _action_task_id(action: Mapping[str, Any]) -> str:
    detail = action.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    return str(action.get("task_id") or detail.get("task_id") or "")


def task_visual_state(
    task: Mapping[str, Any], *, task_statuses: Mapping[str, str]
) -> str:
    status = str(task.get("status") or "pending")
    if status == "completed":
        return "completed"
    if status == "in_progress":
        return "in_progress"
    dependencies = [str(value) for value in task.get("blockedBy") or []]
    if any(task_statuses.get(dep) != "completed" for dep in dependencies):
        return "blocked"
    return "ready"


def project_manual_tasks(
    tasks: Sequence[Mapping[str, Any]],
    *,
    actions: Sequence[Mapping[str, Any]] = (),
    driver_tasks: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    statuses = {str(task.get("task_id")): str(task.get("status")) for task in tasks}
    actions_by_task: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        task_id = _action_task_id(action)
        if task_id:
            actions_by_task.setdefault(task_id, []).append(dict(action))
    driver_by_thread = {
        str(task.get("identity", {}).get("thread_id") or ""): task
        for task in driver_tasks
        if isinstance(task.get("identity"), Mapping)
    }
    result: list[dict[str, Any]] = []
    for source in tasks:
        task = dict(source)
        task_id = str(task.get("task_id") or "")
        thread_id = str(task.get("dispatch_thread_id") or "")
        attempt = driver_by_thread.get(thread_id)
        visual_state = task_visual_state(task, task_statuses=statuses)
        task_actions = actions_by_task.get(task_id, [])
        if task_actions:
            visual_state = "needs_action"
        result.append(
            {
                **task,
                "visual_state": visual_state,
                "pending_actions": task_actions,
                "latest_attempt": attempt,
            }
        )
    return result


def project_activity(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    labels = {
        "turn.started": "started",
        "turn.settled": "completed",
        "turn.failed": "failed",
        "turn.cancelled": "cancelled",
        "turn.deferred": "needs retry",
        "room.stop_requested": "stop requested",
        "authority.claimed": "authority moved",
        "authority.lost": "authority released",
    }
    for event in events:
        kind = str(event.get("kind") or "")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        actor = event.get("actor")
        actor = actor if isinstance(actor, Mapping) else {}
        task_id = str(payload.get("task_id") or "") or None
        member = str(payload.get("member_id") or actor.get("id") or "") or None
        if kind == "message.user":
            text = str(payload.get("text") or "User message")
            title = text
            category = "messages"
        elif kind == "message.member":
            text = str(payload.get("text") or "Member reported a result")
            title = f"@{member} reported" if member else "Member reported"
            category = "messages"
        elif kind in labels:
            subject = f"{task_id} " if task_id else ""
            prefix = f"@{member} " if member and kind.startswith("turn.") else ""
            title = f"{prefix}{subject}{labels[kind]}".strip()
            text = str(payload.get("reason") or payload.get("error") or "")
            category = "errors" if kind in {"turn.failed", "turn.cancelled", "turn.deferred"} else "tasks"
        else:
            title = kind.replace(".", " ") or "Room event"
            text = ""
            category = "system"
        items.append(
            {
                "event_id": str(event.get("event_id") or ""),
                "seq": int(event.get("seq") or 0),
                "kind": kind,
                "category": category,
                "title": title,
                "summary": text,
                "task_id": task_id,
                "thread_id": str(payload.get("thread_id") or "") or None,
                "member_id": member,
                "created_at": float(event.get("created_at") or 0),
                "raw_event": {
                    "room_id": str(event.get("room_id") or ""),
                    "seq": int(event.get("seq") or 0),
                    "event_id": str(event.get("event_id") or ""),
                    "kind": kind,
                    "actor": {
                        "kind": str(actor.get("kind") or ""),
                        "id": str(actor.get("id") or ""),
                    },
                    "authority_epoch": event.get("authority_epoch"),
                    "created_at": float(event.get("created_at") or 0),
                    "redacted": True,
                },
            }
        )
    return items


def _turn_coordinate(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(payload.get("task_id") or ""),
        str(payload.get("thread_id") or ""),
        str(payload.get("turn_id") or ""),
    )


def project_conversation(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    terminal_by_turn: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in {"turn.settled", "turn.failed", "turn.cancelled", "turn.deferred"}:
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            coordinate = _turn_coordinate(payload)
            if all(coordinate):
                terminal_by_turn[coordinate] = event
    turns: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in {"message.user", "message.member"}:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        actor = event.get("actor")
        actor = actor if isinstance(actor, Mapping) else {}
        task_id = str(payload.get("task_id") or "") or None
        coordinate = _turn_coordinate(payload)
        terminal = terminal_by_turn.get(coordinate) if all(coordinate) else None
        terminal_kind = str(terminal.get("kind")) if terminal else None
        turns.append(
            {
                "event_id": str(event.get("event_id") or ""),
                "kind": kind,
                "actor_id": str(actor.get("id") or payload.get("member_id") or ""),
                "actor_kind": str(actor.get("kind") or ""),
                "text": str(payload.get("text") or ""),
                "task_id": task_id,
                "thread_id": str(payload.get("thread_id") or "") or None,
                "turn_id": str(payload.get("turn_id") or "") or None,
                "terminal_kind": terminal_kind,
                "created_at": float(event.get("created_at") or 0),
            }
        )
    return turns


def room_summary(
    room: Mapping[str, Any],
    *,
    tasks: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    driver_status: Mapping[str, Any] | None = None,
    peer_routes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    states = Counter(str(task.get("visual_state") or task.get("status") or "pending") for task in tasks)
    critical = bool(
        states.get("failed")
        or states.get("cancelled")
        or states.get("indeterminate")
    )
    peer_warning = any(str(route.get("status") or "ready") != "ready" for route in peer_routes)
    warning = bool(actions or states.get("blocked") or states.get("needs_action") or peer_warning)
    health = "critical" if critical else "warning" if warning else "healthy"
    current = next((task for task in tasks if task.get("visual_state") in {"needs_action", "in_progress"}), None)
    completed = states.get("completed", 0)
    result = dict(room)
    result["workspace"] = {
        "state": "needs_action" if actions else "failed" if critical else "running" if current else "idle",
        "health": health,
        "task_counts": {"total": len(tasks), "completed": completed, **dict(states)},
        "pending_action_count": len(actions),
        "active_member_count": sum(1 for task in tasks if task.get("visual_state") == "in_progress"),
        "current_task": ({"task_id": current.get("task_id"), "subject": current.get("subject"), "assignee": current.get("owner")} if current else None),
        "last_activity_at": float(room.get("updated_at") or 0),
        "driver_running": bool((driver_status or {}).get("running")),
    }
    return result


def build_room_workspace(db_path: Path | str, *, room_id: str) -> dict[str, Any]:
    """Load existing stores into a bounded, disposable Room read model."""
    room = hosted_rooms.room_state(db_path, room_id=room_id, include_disbanded=True)
    manual = dag.list_tasks(db_path, room_id=room_id)
    attempts = [_serialise_driver_task(task) for task in driver.list_tasks(db_path, room_id=room_id)]
    persisted_actions = []
    for (action_room, member_id), value in load_pending_actions(db_path).items():
        if action_room != room_id:
            continue
        action = {**value, "room_id": action_room, "member_id": member_id}
        action_id = str(action.get("action_id") or action.get("request_id") or "")
        persisted_actions.append(
            {
                "room_id": action_room,
                "member_id": member_id,
                "action_id": action_id,
                "kind": "permission" if action.get("kind") == "approval" else action.get("kind"),
                "task_id": str(action.get("task_id") or "") or None,
                "description": str(action.get("description") or "Action requires attention"),
                "from_handle": str(action.get("from_handle") or member_id),
                "detail": {
                    "redacted": True,
                    "request_id": action_id,
                    "tool_name": str(
                        (action.get("detail") or action.get("approval") or {}).get("tool_name")
                        or (action.get("detail") or action.get("approval") or {}).get("tool")
                        or ""
                    ) or None,
                },
                "created_at": float(action.get("created_at") or 0),
                "redacted": True,
            }
        )
    for attempt in attempts:
        if str(attempt.get("status") or "") not in {"indeterminate", "deferred"}:
            continue
        identity = attempt.get("identity") or {}
        task_id = str(identity.get("task_id") or "")
        execution_generation = int(attempt.get("execution_generation") or 0)
        cancel_generation = int(attempt.get("cancel_generation") or 0)
        persisted_actions.append(
            {
                "room_id": room_id,
                "member_id": "",
                "action_id": (
                    f"retry:{task_id}:{execution_generation}:{cancel_generation}"
                ),
                "kind": "retry",
                "task_id": task_id,
                "description": "Retry uncertain Room turn after restart",
                "from_handle": "",
                "detail": {
                    "task_id": task_id,
                    "thread_id": str(identity.get("thread_id") or ""),
                    "status": str(attempt.get("status") or ""),
                    "execution_generation": execution_generation,
                },
                "created_at": float(
                    attempt.get("indeterminate_at")
                    or attempt.get("updated_at")
                    or 0
                ),
                "redacted": True,
            }
        )
    log = hosted_rooms.read_events(
        db_path, room_id=room_id, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT,
        include_disbanded=True,
    )
    tasks = project_manual_tasks(manual, actions=persisted_actions, driver_tasks=attempts)
    manual_threads = {
        str(task.get("dispatch_thread_id") or "")
        for task in tasks
        if task.get("dispatch_thread_id")
    }
    manual_attempt_ids = {
        str((task.get("latest_attempt") or {}).get("identity", {}).get("task_id") or "")
        for task in tasks
        if task.get("latest_attempt")
    }
    for attempt in attempts:
        identity = attempt.get("identity") or {}
        task_id = str(identity.get("task_id") or "")
        thread_id = str(identity.get("thread_id") or "")
        if (
            task_id
            and task_id not in manual_attempt_ids
            and thread_id not in manual_threads
        ):
            tasks.append({
                "task_id": task_id, "subject": task_id,
                "description": "Driver-managed Room task",
                "status": attempt.get("status"),
                "visual_state": ("in_progress" if attempt.get("status") == "running" else attempt.get("status")),
                "pending_actions": [], "latest_attempt": attempt,
                "driver_only": True,
            })
    safe_log = {
        **log,
        "events": [item["raw_event"] for item in project_activity(log["events"])],
        "redacted": True,
    }
    return {
        "room": room, "tasks": tasks, "attempts": attempts,
        "pending_actions": persisted_actions,
        "conversation": project_conversation(log["events"]),
        "activity": project_activity(log["events"]), "log": safe_log,
    }
