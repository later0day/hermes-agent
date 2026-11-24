"""Messaging-facing ``/room`` commands over the hosted-room service and stores."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import shlex
import sqlite3
import uuid
from datetime import datetime

from agent.i18n import t

logger = logging.getLogger("gateway.run")

# Expected store/service failures retain the command's caller-visible error reply.
_ROOM_COMMAND_ERRORS = (
    sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError,
)


@dataclasses.dataclass(frozen=True)
class _RoomRequest:
    action: str
    args: list[str]
    source: object
    here_platform: str
    here_chat_id: str
    here_thread_id: str


@dataclasses.dataclass(frozen=True)
class _RoomMirrorRequest:
    room_id: str
    platform: str
    chat_id: str
    thread_id: str
    decider_only: bool
    all_members: bool
    inbound: bool


def _room_timestamp(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value or "")


def _room_member_handles(members) -> str:
    out = []
    for member in members or []:
        if not isinstance(member, dict):
            continue
        handle = str(member.get("handle") or "")
        profile = str(member.get("profile") or "")
        out.append(f"@{handle} ({profile})" if handle else profile)
    return ", ".join(out) if out else "—"


def _room_task_after(tokens):
    """Return ``(positional, blocked_by)`` after splitting ``--after=…`` options."""
    positional: list[str] = []
    after: list[str] = []
    for token in tokens:
        if token.startswith("--after="):
            after.extend(x for x in token[len("--after="):].split(",") if x)
        else:
            positional.append(token)
    return positional, after


def _room_task_row(task) -> str:
    return t(
        "gateway.room.task_row",
        task_id=task.get("task_id"),
        status=task.get("status"),
        owner=task.get("owner") or "—",
        subject=task.get("subject"),
        blocked_by=",".join(task.get("blockedBy") or []) or "—",
    )


class GatewayRoomCommandsMixin:
    """Thin parse/render commands; room identity and execution stay in their services."""

    async def _handle_room_command(self, event) -> str:
        """Handle ``/room`` without blocking the gateway loop on store or service work."""
        text = (event.text or "").strip()
        if text.startswith("/"):
            text = text.lstrip("/")
        if text.startswith("room"):
            text = text[len("room"):].lstrip()
        try:
            tokens = shlex.split(text) if text else []
        except ValueError:
            tokens = text.split()
        if not tokens:
            return t("gateway.room.usage")

        source = event.source
        action, args = tokens[0].lower(), tokens[1:]
        if action in ("plan", "confirm"):
            return await self._handle_room_plan(action, args, source)
        request = _RoomRequest(
            action=action,
            args=args,
            source=source,
            here_platform=source.platform.value if source and source.platform else "",
            here_chat_id=str(source.chat_id) if source and source.chat_id else "",
            here_thread_id=str(source.thread_id) if source and source.thread_id else "",
        )
        try:
            output = await asyncio.to_thread(self._run_room_action, request)
        except _ROOM_COMMAND_ERRORS as exc:
            return t("gateway.room.error_prefix", error=exc)
        if output and len(output) > 3800:
            output = output[:3800] + "\n" + t("gateway.room.truncated_suffix")
        return output or t("gateway.room.usage")

    def _run_room_action(self, request: _RoomRequest) -> str:
        from gateway.hosted_rooms import default_db_path

        handler = {
            "list": self._room_list,
            "show": self._room_show,
            "create": self._room_create,
            "add": self._room_update_members,
            "remove": self._room_update_members,
            "disband": self._room_disband,
            "mirror": self._room_mirror,
            "unmirror": self._room_mirror,
            "task": self._room_task,
        }.get(request.action)
        if handler is None:
            return t("gateway.room.unknown_action", action=request.action)
        return handler(request, default_db_path())

    def _room_list(self, request: _RoomRequest, db_path) -> str:
        from gateway.hosted_rooms import list_rooms
        from tui_gateway.methods_groups import _active_store

        rooms = list_rooms(_active_store(), placement="shared", include_disbanded="--all" in request.args)
        if not rooms:
            return t("gateway.room.no_rooms")
        lines = [t("gateway.room.list_header", count=len(rooms))]
        for room in rooms:
            lines.append(t(
                "gateway.room.list_row",
                room_id=room.get("room_id"),
                name=room.get("name"),
                members=len(room.get("members") or []),
                latest_seq=room.get("latest_seq", 0),
            ))
        return "\n".join(lines)

    def _room_show(self, request: _RoomRequest, db_path) -> str:
        from gateway.hosted_rooms import HostedRoomError, room_state
        from tui_gateway.methods_groups import _active_store, get_hosted_room_service

        if not request.args:
            return t("gateway.room.need_room_id", action="show")
        try:
            room = room_state(
                _active_store(), placement="shared", room_id=request.args[0], include_disbanded=True)
        except HostedRoomError as exc:
            return t("gateway.room.error_prefix", error=exc)
        lines = [
            t("gateway.room.show_header", name=room.get("name"), room_id=room.get("room_id")),
            t("gateway.room.show_members", count=len(room.get("members") or []),
              handles=_room_member_handles(room.get("members"))),
            t("gateway.room.show_meta", created=_room_timestamp(room.get("created_at")),
              revision=room.get("revision", 0), latest_seq=room.get("latest_seq", 0),
              epoch=room.get("authority_epoch", 0)),
        ]
        service = get_hosted_room_service()
        if service is not None and room.get("disbanded_at") is None:
            try:
                status = service.status(str(room.get("room_id")))
                running = status.get("running") if isinstance(status, dict) else status
                lines.append(t("gateway.room.show_running", status=running))
            except _ROOM_COMMAND_ERRORS:
                logger.debug("Could not read hosted-room status for /room show", exc_info=True)
        return "\n".join(lines)

    def _room_create(self, request: _RoomRequest, db_path) -> str:
        args = request.args
        if len(args) < 4:
            return t("gateway.room.need_create_args")
        room_id, name = args[0], args[1]
        decider_profile = ""
        profiles = []
        for token in args[2:]:
            if token.startswith("--decider="):
                decider_profile = token[len("--decider="):].strip()
                continue
            if token.startswith("--"):
                return t("gateway.room.unknown_create_flag", flag=token)
            profiles.append(token)
        if not profiles:
            return t("gateway.room.need_create_args")
        if decider_profile and decider_profile not in profiles:
            return t("gateway.room.decider_not_a_member", profile=decider_profile)
        return self._room_create_from_profiles(room_id, name, profiles, decider_profile)

    @staticmethod
    def _room_members_with_additions(current: list[dict], targets: list[str]) -> tuple[list[dict], list[str]]:
        existing_handles = {str(member.get("handle") or "") for member in current}
        members = list(current)
        added: list[str] = []
        for profile in targets:
            handle = profile
            suffix = 1
            while handle in existing_handles:
                suffix += 1
                handle = f"{profile}{suffix}"
            existing_handles.add(handle)
            members.append({
                "member_id": handle,
                "profile": profile,
                "handle": handle,
                "target": {"kind": "local", "profile": profile},
            })
            added.append(profile)
        return members, added

    @staticmethod
    def _room_members_without(current: list[dict], targets: list[str]) -> tuple[list[dict], list[str]]:
        wanted = set(targets)
        members = []
        removed: list[str] = []
        for member in current:
            handle = str(member.get("handle") or "")
            profile = str(member.get("profile") or "")
            if handle in wanted or profile in wanted:
                removed.append(profile or handle)
            else:
                members.append(member)
        return members, removed

    def _room_update_members(self, request: _RoomRequest, db_path) -> str:
        from gateway.hosted_rooms import HostedRoomError, room_state
        from tui_gateway.methods_groups import get_hosted_room_service

        if len(request.args) < 2:
            return t("gateway.room.need_member_args", action=request.action)
        room_id, targets = request.args[0], request.args[1:]
        service = get_hosted_room_service()
        if service is None:
            return t("gateway.room.worker_unavailable")
        try:
            room = room_state(service.store, placement=service.placement, room_id=room_id)
        except HostedRoomError as exc:
            return t("gateway.room.error_prefix", error=exc)
        current = [dict(member) for member in (room.get("members") or []) if isinstance(member, dict)]
        if request.action == "add":
            members, changed = self._room_members_with_additions(current, targets)
        else:
            members, changed = self._room_members_without(current, targets)
            if not changed:
                return t("gateway.room.no_members_matched", room_id=room_id, members=", ".join(targets))
        updated = service.update_members(
            room_id=room_id, event_id=f"members_{uuid.uuid4().hex[:16]}", members=members,
        )
        key = "members_added" if request.action == "add" else "members_removed"
        return t(f"gateway.room.{key}", room_id=updated.get("room_id"),
                 changed=", ".join(changed), members=len(updated.get("members") or []))

    @staticmethod
    def _disband_room_tombstone(service, room_id: str, state: dict | None = None) -> dict:
        from gateway.hosted_rooms import (
            AuthorityConflictError, disband_room, local_authority_gateway_id,
        )

        local_gateway_id = local_authority_gateway_id()
        if state is not None and str(state["authority_gateway_id"]) != local_gateway_id:
            raise AuthorityConflictError("This room is managed by another gateway.")
        return disband_room(
            service.store,
            placement=service.placement,
            room_id=room_id,
            expected_gateway_id=str(local_gateway_id),
            expected_epoch=int(state["authority_epoch"] if state is not None else 1),
        )

    def _room_disband(self, request: _RoomRequest, db_path) -> str:
        from gateway.hosted_rooms import (
            HostedRoomError, RoomHistoryExpiredError, RoomNotFoundError, room_state,
        )
        from tui_gateway.methods_groups import get_hosted_room_service

        if not request.args:
            return t("gateway.room.need_room_id", action="disband")
        room_id = request.args[0]
        service = get_hosted_room_service()
        if service is None:
            return t("gateway.room.worker_unavailable")
        try:
            try:
                existing = room_state(
                    service.store, placement=service.placement, room_id=room_id, include_disbanded=True)
            except RoomHistoryExpiredError:
                self._disband_room_tombstone(service, room_id)
                return t("gateway.room.disbanded", room_id=room_id, routes=0)
            if existing.get("disbanded_at") is not None:
                self._disband_room_tombstone(service, room_id, existing)
                return t("gateway.room.already_disbanded", room_id=room_id)
            service.stop_room(
                room_id, cancel_id=f"disband_{uuid.uuid4().hex[:16]}", require_acknowledged=True,
            )
            routes = service.revoke_room_routes(room_id)
            self._disband_room_tombstone(service, room_id, existing)
        except (HostedRoomError, RoomNotFoundError) as exc:
            return t("gateway.room.error_prefix", error=exc)
        return t("gateway.room.disbanded", room_id=room_id, routes=routes)

    def _parse_room_mirror(self, request: _RoomRequest) -> _RoomMirrorRequest | str:
        if not request.args:
            return t("gateway.room.need_room_id", action=request.action)
        target = ""
        decider_only = all_members = inbound = False
        for token in request.args[1:]:
            if token == "--decider-only":
                decider_only = True
            elif token == "--all-members":
                all_members = True
            elif token == "--inbound":
                inbound = True
            elif token.startswith("--"):
                return t("gateway.room.unknown_create_flag", flag=token)
            elif not target:
                target = token
        thread_id = ""
        if target:
            platform, _, chat_id = target.partition(":")
            platform, chat_id = platform.strip().lower(), chat_id.strip()
            if not platform or not chat_id:
                return t("gateway.room.mirror_bad_target", target=target)
        else:
            platform, chat_id, thread_id = (
                request.here_platform, request.here_chat_id, request.here_thread_id,
            )
        if not platform or not chat_id:
            return t("gateway.room.mirror_no_target")
        return _RoomMirrorRequest(
            room_id=request.args[0], platform=platform, chat_id=chat_id, thread_id=thread_id,
            decider_only=decider_only, all_members=all_members, inbound=inbound,
        )

    def _room_unmirror(self, db_path, mirror: _RoomMirrorRequest) -> str:
        from gateway import room_mirror_db

        removed = room_mirror_db.remove_sub(
            db_path, room_id=mirror.room_id, platform=mirror.platform,
            chat_id=mirror.chat_id, thread_id=mirror.thread_id,
        )
        if removed and hasattr(self, "_reload_room_inbound_sources"):
            self._reload_room_inbound_sources()
        key = "unmirrored" if removed else "mirror_not_found"
        return t(f"gateway.room.{key}", room_id=mirror.room_id,
                 platform=mirror.platform, chat_id=mirror.chat_id)

    def _room_mirror(self, request: _RoomRequest, db_path) -> str:
        from gateway import room_mirror_db
        from gateway.hosted_rooms import HostedRoomError, room_state
        from tui_gateway.methods_groups import _active_store

        mirror = self._parse_room_mirror(request)
        if isinstance(mirror, str):
            return mirror
        if request.action == "unmirror":
            return self._room_unmirror(db_path, mirror)
        if mirror.decider_only and mirror.all_members:
            return t("gateway.room.error_prefix",
                     error="--decider-only and --all-members are mutually exclusive")
        try:
            room = room_state(_active_store(), placement="shared", room_id=mirror.room_id)
        except HostedRoomError as exc:
            return t("gateway.room.error_prefix", error=exc)
        decider = next(
            (member for member in (room.get("members") or [])
             if isinstance(member, dict) and member.get("role") == "decider"),
            None,
        )
        if mirror.decider_only and decider is None:
            return t("gateway.room.mirror_no_decider", room_id=mirror.room_id)
        member_filter = None
        decider_only = mirror.decider_only
        if decider is not None and not mirror.all_members:
            member_filter = str(decider.get("member_id") or "")
            decider_only = True
        mirror_here = (mirror.platform, mirror.chat_id) == (request.here_platform, request.here_chat_id)
        owner = (
            self._transport_owner(request.source)
            if mirror_here and callable(getattr(request.source, "_transport_adapter_ref", None))
            else None
        )
        room_mirror_db.create_sub(
            db_path, room_id=mirror.room_id, platform=mirror.platform, chat_id=mirror.chat_id,
            thread_id=mirror.thread_id, member_filter=member_filter,
            notifier_profile=owner[1] if isinstance(owner, tuple) else None,
            inbound=True if mirror.inbound else None,
        )
        if hasattr(self, "_reload_room_inbound_sources"):
            self._reload_room_inbound_sources()
        if mirror.inbound:
            key = "mirrored_inbound"
        else:
            key = "mirrored_decider" if decider_only else "mirrored"
        return t(f"gateway.room.{key}", room_id=mirror.room_id,
                 platform=mirror.platform, chat_id=mirror.chat_id)

    def _room_task(self, request: _RoomRequest, db_path) -> str:
        from gateway import room_task_dag

        if not request.args:
            return t("gateway.room.task_usage")
        subcommand = request.args[0].lower()
        handler = {
            "list": self._room_task_list,
            "add": self._room_task_add,
            "dep": self._room_task_dep,
            "claim": self._room_task_claim,
            "assign": self._room_task_assign,
            "done": self._room_task_done,
            "release": self._room_task_release,
        }.get(subcommand)
        if handler is None:
            return t("gateway.room.task_usage")
        try:
            return handler(request.args[1:], db_path, room_task_dag)
        except room_task_dag.RoomTaskError as exc:
            return t("gateway.room.task_error", error=exc)

    def _room_task_list(self, rest: list[str], db_path, dag) -> str:
        if not rest:
            return t("gateway.room.need_room_id", action="task list")
        if "--manual" in rest[1:]:
            return self._room_manual_task_list(rest[0], db_path, dag)
        return self._room_live_task_list(rest[0], db_path)

    def _room_manual_task_list(self, room_id: str, db_path, dag) -> str:
        tasks = dag.list_tasks(db_path, room_id=room_id)
        if not tasks:
            return t("gateway.room.task_none", room_id=room_id)
        lines = [t("gateway.room.task_header", room_id=room_id, count=len(tasks))]
        lines.extend(_room_task_row(task) for task in tasks)
        return "\n".join(lines)

    def _room_local_profiles(self) -> list[str]:
        from tui_gateway.methods_groups import get_hosted_room_service

        service = get_hosted_room_service()
        if service is not None:
            return service.local_profiles()
        from hermes_constants import get_hermes_home
        from tui_gateway.hosted_room_service import local_profiles_under

        home = get_hermes_home()
        return local_profiles_under(home.parent.parent if home.parent.name == "profiles" else home)

    def _room_live_task_list(self, room_id: str, db_path) -> str:
        from gateway import hosted_room_discussion
        from gateway.hosted_rooms import HostedRoomError, MAX_LOG_LIMIT, read_events, room_state
        from tui_gateway.methods_groups import _active_store

        profiles = self._room_local_profiles()
        try:
            store = _active_store()
            room = room_state(store, placement="shared", room_id=room_id)
            event_page = read_events(
                store, placement="shared", room_id=room_id, since_seq=0, limit=MAX_LOG_LIMIT)
            projected = hosted_room_discussion.project_task_dag(
                room, event_page.get("events") or [], local_profiles=profiles,
            )
        except HostedRoomError as exc:
            return t("gateway.room.error_prefix", error=exc)
        if not projected:
            return t("gateway.room.task_none_live", room_id=room_id)
        lines = [t("gateway.room.task_header", room_id=room_id, count=len(projected))]
        for task in projected:
            subject = " ".join(task.subject.split())
            if len(subject) > 60:
                subject = subject[:57] + "…"
            lines.append(t(
                "gateway.room.task_row", task_id=f"r{task.round_index}:@{task.owner_handle}",
                status=task.status, owner=task.owner_handle, subject=subject,
                blocked_by=",".join(task.blocked_by) or "—",
            ))
        return "\n".join(lines)

    def _room_task_add(self, rest: list[str], db_path, dag) -> str:
        if len(rest) < 2:
            return t("gateway.room.task_need_add")
        room_id = rest[0]
        positional, after = _room_task_after(rest[1:])
        description = ""
        subject_tokens = []
        for token in positional:
            if token.startswith("--desc="):
                description = token[len("--desc="):]
            else:
                subject_tokens.append(token)
        subject = " ".join(subject_tokens).strip()
        if not subject:
            return t("gateway.room.task_need_add")
        task = dag.create_task(
            db_path, room_id=room_id, subject=subject, description=description, blocked_by=tuple(after),
        )
        return t("gateway.room.task_added", room_id=room_id,
                 task_id=task["task_id"], subject=task["subject"])

    def _room_task_dep(self, rest: list[str], db_path, dag) -> str:
        positional, after = _room_task_after(rest)
        if len(positional) < 1 or not after:
            return t("gateway.room.task_need_dep")
        room_id = positional[0]
        task_id = positional[1] if len(positional) > 1 else None
        if not task_id:
            return t("gateway.room.task_need_dep")
        for dependency in after:
            dag.add_dependency(db_path, room_id=room_id, task_id=task_id, blocked_by=dependency)
        return t("gateway.room.task_dep_added", task_id=task_id, blocked_by=",".join(after))

    def _room_task_claim(self, rest: list[str], db_path, dag) -> str:
        if len(rest) < 2:
            return t("gateway.room.task_need_claim")
        room_id, owner = rest[0], rest[1]
        task_id = rest[2] if len(rest) > 2 else None
        if task_id:
            task = dag.claim_task(db_path, room_id=room_id, task_id=task_id, owner=owner)
        else:
            task = dag.claim_next(db_path, room_id=room_id, owner=owner)
            if task is None:
                return t("gateway.room.task_none_available", room_id=room_id)
        return t("gateway.room.task_claimed", task_id=task["task_id"],
                 owner=task["owner"], subject=task["subject"])

    def _room_task_assign(self, rest: list[str], db_path, dag) -> str:
        if len(rest) < 3:
            return t("gateway.room.task_need_assign")
        room_id, task_id, owner = rest[0], rest[1], rest[2]
        task = dag.assign_task(db_path, room_id=room_id, task_id=task_id, owner=owner)
        return t("gateway.room.task_assigned", task_id=task_id,
                 owner=task["owner"], subject=task["subject"])

    def _room_task_done(self, rest: list[str], db_path, dag) -> str:
        if len(rest) < 2:
            return t("gateway.room.need_room_id", action="task done")
        task = dag.complete_task(db_path, room_id=rest[0], task_id=rest[1])
        return t("gateway.room.task_done", task_id=task["task_id"], subject=task["subject"])

    def _room_task_release(self, rest: list[str], db_path, dag) -> str:
        if len(rest) < 2:
            return t("gateway.room.need_room_id", action="task release")
        task = dag.release_task(db_path, room_id=rest[0], task_id=rest[1])
        return t("gateway.room.task_released", task_id=task["task_id"], subject=task["subject"])
