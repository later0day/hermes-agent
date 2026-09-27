"""Shared helpers for delegating IM/dashboard work into Kanban tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gateway.session import SessionSource


class DelegateTaskError(ValueError):
    """User-facing validation error for Kanban delegation requests."""


@dataclass(frozen=True)
class DelegatedKanbanTask:
    task_id: str
    board_slug: str
    assignee: str | None
    auto_route: bool
    title: str
    body: str
    workspace: str
    workspace_kind: str
    workspace_path: str | None
    priority: int
    max_runtime_seconds: int | None
    skills: list[str]
    subscribed: bool


def create_delegated_kanban_task(
    *,
    assignee: str | None,
    task_text: str,
    board: str | None = None,
    workspace: str = "scratch",
    priority: int = 0,
    max_runtime_arg: str | None = None,
    skills: Iterable[str] | None = None,
    source: SessionSource | None = None,
    created_by: str = "gateway",
    notifier_profile: str | None = None,
    auto_route: bool = False,
) -> DelegatedKanbanTask:
    """Create a Kanban task and optionally subscribe an IM source."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban import _parse_duration, _parse_workspace_flag
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    body = str(task_text or "").strip()
    if not body:
        raise DelegateTaskError("Task text is required.")

    resolved_assignee: str | None = None
    if not auto_route:
        try:
            resolved_assignee = normalize_profile_name(assignee or "")
        except ValueError as exc:
            raise DelegateTaskError(str(exc)) from exc
        if not profile_exists(resolved_assignee):
            raise DelegateTaskError(
                f"Unknown agent profile `{resolved_assignee}`. "
                f"Create it with `/agent create {resolved_assignee}` first."
            )

    title = body.splitlines()[0].strip()
    if len(title) > 180:
        title = title[:177] + "..."

    board_slug = kb._normalize_board_slug(board) if board else None
    if board_slug and board_slug != kb.DEFAULT_BOARD and not kb.board_exists(board_slug):
        raise DelegateTaskError(
            f"Unknown Kanban board `{board_slug}`. "
            f"Create it with `hermes kanban boards create {board_slug}` first."
        )

    try:
        workspace_kind, workspace_path = _parse_workspace_flag(workspace)
        max_runtime_seconds = _parse_duration(max_runtime_arg)
    except Exception as exc:
        raise DelegateTaskError(f"Invalid delegate option: {exc}") from exc

    skill_list = [str(skill).strip() for skill in (skills or []) if str(skill).strip()]
    conn = kb.connect(board=board_slug)
    subscribed = False
    try:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=resolved_assignee,
            created_by=created_by,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            priority=priority,
            triage=auto_route,
            max_runtime_seconds=max_runtime_seconds,
            skills=skill_list or None,
            initial_status="running",
            board=board_slug,
        )
        if source and source.platform and source.chat_id:
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform=source.platform.value,
                chat_id=source.chat_id,
                thread_id=source.thread_id,
                user_id=source.user_id,
                notifier_profile=notifier_profile,
            )
            subscribed = True
    finally:
        conn.close()

    return DelegatedKanbanTask(
        task_id=task_id,
        board_slug=board_slug or kb.DEFAULT_BOARD,
        assignee=resolved_assignee,
        auto_route=auto_route,
        title=title,
        body=body,
        workspace=workspace,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        priority=priority,
        max_runtime_seconds=max_runtime_seconds,
        skills=skill_list,
        subscribed=subscribed,
    )
