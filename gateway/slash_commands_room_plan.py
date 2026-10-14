"""``/room create|plan|confirm`` helpers for GatewaySlashCommandsMixin (team creation from profiles)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.i18n import t
from gateway.room_plan import ProposalStore, RoomPlan, members_from_profiles, plan_team

logger = logging.getLogger("gateway.run")

_PROPOSALS = ProposalStore()


def _proposal_key(source: Any) -> str:
    """Proposals belong to one person in one chat: someone else in the group cannot confirm them."""
    platform = getattr(getattr(source, "platform", None), "value", "") or ""
    return f"{platform}:{getattr(source, 'chat_id', '') or ''}:{getattr(source, 'user_id', '') or ''}"


def _team_roster() -> list[dict[str, str]]:
    """Local profiles a team may use, with their role descriptions (the decomposer's roster); legacy
    agent-room observer profiles are not team members."""
    from hermes_cli.kanban_decompose import _build_roster

    roster, _valid = _build_roster()
    return [{"name": entry["name"], "description": entry["description"] if entry["has_description"] else ""}
            for entry in roster
            if not (entry["name"].startswith("room_") and entry["name"].endswith("_observer"))]


def _render_plan(plan: RoomPlan) -> str:
    return t("gateway.room.plan_proposal", name=plan.name, decider=plan.decider,
             members=", ".join(f"@{m}" for m in plan.members), reason=plan.reason or "-")


class GatewayRoomPlanCommandsMixin:
    """Room creation from bare profiles, by hand (``/room create``) or planned (``/room plan`` + confirm)."""

    def _room_create_from_profiles(self, room_id: str, name: str, profiles: list[str], decider_profile: str) -> str:
        from tui_gateway.methods_groups import get_hosted_room_service

        service = get_hosted_room_service()
        if service is None:
            return t("gateway.room.worker_unavailable")
        try:
            room = service.create_room(room_id=room_id, name=name,
                                       members=members_from_profiles(profiles, decider_profile))
        except Exception as exc:  # roster validation/authority errors go back to the chat as text
            logger.warning("/room create %s failed", room_id, exc_info=True)
            return t("gateway.room.error_prefix", error=exc)
        if room.get("idempotent"):
            return t("gateway.room.created_idempotent", room_id=room.get("room_id"))
        return t("gateway.room.created", room_id=room.get("room_id"), name=room.get("name"),
                 members=len(room.get("members") or []))

    async def _handle_room_plan(self, action: str, args: list[str], source: Any) -> str:
        key = _proposal_key(source)
        if action == "confirm":
            plan = _PROPOSALS.pop(key)
            if plan is None:
                return t("gateway.room.confirm_none")
            return self._room_create_from_profiles(
                plan.room_id, plan.name, [plan.decider, *plan.members], plan.decider)
        request = " ".join(args).strip()
        if not request:
            return t("gateway.room.plan_usage")
        result = await asyncio.to_thread(lambda: plan_team(request, _team_roster()))
        if isinstance(result, str):
            return t("gateway.room.plan_failed", error=result)
        _PROPOSALS.put(key, result)
        return _render_plan(result)
