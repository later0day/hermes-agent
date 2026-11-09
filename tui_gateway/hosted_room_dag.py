"""Manual task DAG dispatch through the ordinary hosted-room turn scheduler."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms, room_task_dag as dag

if TYPE_CHECKING:
    from tui_gateway.hosted_room_service import HostedRoomService

logger = logging.getLogger(__name__)


def task_owners(service: HostedRoomService, room_id: str) -> dict[str, str]:
    return {
        str(task["dispatch_thread_id"]): str(task["owner"])
        for task in dag.list_tasks(service.db_path, room_id=room_id)
        if task.get("dispatch_thread_id") and task.get("owner")
    }


def append_direct_assignment(
    service: HostedRoomService,
    *,
    room_id: str,
    event_id: str,
    payload: Mapping[str, Any],
    owner: str,
    gateway_id: str,
    epoch: int,
) -> dict[str, Any]:
    """Commit one explicit worker request as its DAG row and scheduler anchor."""
    from gateway.session_hosted_attachments import append_user_event

    task_id = f"send:{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:32]}"
    thread_id = f"dagtask:{task_id}"
    normalized = discussion.validate_user_payload({**payload, "thread_id": thread_id})

    def write_task(conn) -> None:
        dag.create_dispatched_task_locked(
            conn, room_id=room_id, task_id=task_id,
            subject=str(normalized["text"]), owner=owner, dispatch_thread_id=thread_id)

    return append_user_event(
        service, room_id=room_id, event_id=event_id, payload=normalized,
        gateway_id=gateway_id, epoch=epoch, actor_id="task-dag", write_sidecar=write_task)


def autodispatch_next(service: HostedRoomService, room: Mapping[str, Any]) -> None:
    """Claim the next unambiguous worker task and append its scheduler anchor."""
    room_id = str(room["room_id"])
    try:
        tasks = dag.list_claimable(service.db_path, room_id=room_id)
        members = discussion.validate_roster(
            room.get("members") or [], local_profiles=service.room_profiles(room))
    except Exception:
        logger.exception("could not list claimable DAG tasks for room %s", room_id)
        return
    for task in tasks:
        targets = discussion.resolve_mentions(
            (str(task.get("subject") or ""),), members, default_all=False)
        if len(targets) != 1:
            continue
        target = targets[0]
        anchor_thread = f"dagtask:{task['task_id']}"
        claimed = dag.claim_task_for_dispatch(
            service.db_path, room_id=room_id, task_id=str(task["task_id"]),
            owner=target.handle, dispatch_thread_id=anchor_thread)
        if claimed is None:
            continue
        anchor_text = str(task.get("subject") or "").strip()
        try:
            hosted_rooms.append_event(
                service.db_path, room_id=room_id,
                event_id=f"dagdispatch:{task['task_id']}", kind="message.user",
                actor={"kind": "user", "id": "task-dag"},
                payload={"text": anchor_text, "thread_id": anchor_thread},
                authority_gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]))
            service.runtime.wakeup()
        except Exception:
            logger.exception("could not append DAG dispatch anchor for room %s task %s", room_id, task["task_id"])
            try:
                dag.release_task(service.db_path, room_id=room_id, task_id=str(task["task_id"]))
            except Exception:
                logger.exception("could not release DAG dispatch for room %s task %s", room_id, task["task_id"])
        return


def sweep_completed(service: HostedRoomService, room: Mapping[str, Any]) -> None:
    """Complete a dispatch only after its claimed worker's reply is turn-settled."""
    room_id = str(room["room_id"])
    try:
        pending_dispatches = dag.list_in_progress_dispatches(service.db_path, room_id=room_id)
    except Exception:
        logger.exception("could not list active DAG dispatches for room %s", room_id)
        return
    pending_threads = {
        str(task.get("dispatch_thread_id") or "") for task in pending_dispatches
        if task.get("dispatch_thread_id")}
    if not pending_threads:
        return
    try:
        events = service._events(room_id)
    except Exception:
        logger.exception("could not read room events while sweeping DAG dispatches for %s", room_id)
        return
    member_id_by_handle = {
        str(member.get("handle") or "").casefold(): str(member.get("member_id") or "")
        for member in (room.get("members") or []) if isinstance(member, Mapping)}
    expected_member_by_thread = {
        str(task.get("dispatch_thread_id") or ""): member_id_by_handle.get(
            str(task.get("owner") or "").casefold(), "")
        for task in pending_dispatches if task.get("dispatch_thread_id")}
    committed_message_ids = {
        str((event.get("payload") or {}).get("message_event_id") or "")
        for event in events if event.get("kind") == "turn.settled"}
    settled_threads: set[str] = set()
    for event in events:
        if event.get("kind") != "message.member":
            continue
        payload = event.get("payload") or {}
        thread_id = str(payload.get("thread_id") or "")
        if thread_id not in pending_threads:
            continue
        if str(payload.get("member_id") or "") != expected_member_by_thread.get(thread_id):
            continue
        if str(event.get("event_id") or "") not in committed_message_ids:
            continue
        if discussion.is_pass_text(payload.get("text")):
            continue
        settled_threads.add(thread_id)
    for thread_id in settled_threads:
        try:
            completed = dag.complete_dispatched(
                service.db_path, room_id=room_id, dispatch_thread_id=thread_id)
        except Exception:
            logger.exception("could not complete DAG dispatch for room %s thread %s", room_id, thread_id)
            continue
        if completed is not None:
            service.runtime.wakeup()
