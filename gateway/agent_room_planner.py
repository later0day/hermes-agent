"""Room planner — M2 natural-language room composition.

Design ref: docs/design/agent-room/design.html §2.3 (M2 明确包含).

Reads the user's requirement + existing profile roster → calls aux LLM
→ parses JSON → returns a RoomPlan (proposed members + room description).

Key constraint (§11m2 DoD): the planner NEVER creates profiles or rooms.
It only returns a plan. The caller (/room plan command or REST API)
shows the plan to the user for Y/N confirmation, and only on Y does
the caller invoke create_profile + M1's /room create.

Mirrors hermes_cli/kanban_decompose.py's aux LLM pattern: call_llm with
task="room_planner", _extract_json_blob for lenient JSON parsing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from gateway.agent_room_planner_prompts import SYSTEM_PROMPT, USER_TEMPLATE

logger = logging.getLogger(__name__)

MAX_ROOM_MEMBERS = 5


@dataclass(frozen=True)
class PlannedMember:
    """One proposed member in the room plan."""
    profile: Optional[str]    # existing profile name, or None if new
    is_new: bool
    name: str
    description: str
    reason: str = ""


@dataclass(frozen=True)
class RoomPlan:
    """The complete plan returned by the LLM.

    members is empty when the requirement is too vague to plan
    (rationale will say "requirement too vague").
    """
    rationale: str
    members: list[PlannedMember] = field(default_factory=list)
    room_description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rationale": self.rationale,
            "room_description": self.room_description,
            "members": [
                {
                    "profile": m.profile,
                    "is_new": m.is_new,
                    "name": m.name,
                    "description": m.description,
                    "reason": m.reason,
                }
                for m in self.members
            ],
        }

    @property
    def is_actionable(self) -> bool:
        """True if the plan has members and can be confirmed."""
        return len(self.members) > 0

    @property
    def new_profiles(self) -> list[PlannedMember]:
        """Members that require creating a new profile."""
        return [m for m in self.members if m.is_new]

    @property
    def existing_profiles(self) -> list[PlannedMember]:
        """Members that reuse an existing profile."""
        return [m for m in self.members if not m.is_new]


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """Lenient JSON extraction — tolerates fenced code blocks and
    leading/trailing whitespace. Copied from kanban_decompose.py."""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def _format_roster(profiles: list[tuple[str, str]]) -> str:
    """Format (name, description) pairs for the LLM prompt."""
    if not profiles:
        return "(no existing profiles)"
    lines = []
    for name, desc in profiles:
        desc = desc.strip() or "(no description)"
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _validate_member(
    raw_member: dict,
    existing_names: set[str],
) -> Optional[PlannedMember]:
    """Parse + validate one member from the LLM output.

    Hallucination guard (§11m2 B3 / M2-B6): if the LLM says is_new=false
    but the profile name isn't in the roster, treat it as new (don't
    hallucinate an existing profile).
    """
    if not isinstance(raw_member, dict):
        return None

    profile = str(raw_member.get("profile") or "").strip()
    is_new = bool(raw_member.get("is_new", False))
    name = str(raw_member.get("name") or "").strip()
    description = str(raw_member.get("description") or "").strip()
    reason = str(raw_member.get("reason") or "").strip()

    if not name:
        return None

    # Hallucination guard: LLM says "existing" but name not in roster
    if not is_new and profile and profile not in existing_names:
        logger.warning(
            "room planner: LLM claimed '%s' is existing but it's not in "
            "the roster; treating as new profile",
            profile,
        )
        is_new = True
        # keep the name as the profile-to-create

    if is_new:
        profile = None  # no existing profile to reference

    return PlannedMember(
        profile=profile or None,
        is_new=is_new,
        name=name,
        description=description,
        reason=reason,
    )


def plan_room(
    requirement: str,
    profiles: list[tuple[str, str]],
    *,
    max_members: int = MAX_ROOM_MEMBERS,
    timeout: int = 60,
) -> RoomPlan:
    """Call the aux LLM to propose a room composition.

    Parameters
    ----------
    requirement : str
        Natural language description of what the user wants the room to do.
    profiles : list[tuple[str, str]]
        Existing (profile_name, description) pairs from the roster.
    max_members : int
        Hard cap on members (default 5, matches N3).
    timeout : int
        Seconds to wait for the LLM response.

    Returns
    -------
    RoomPlan
        The proposed composition. members is empty if the requirement
        is too vague or the LLM call fails.
    """
    requirement = (requirement or "").strip()
    if not requirement:
        return RoomPlan(rationale="requirement is empty")

    # Truncate to prevent prompt explosion
    if len(requirement) > 2000:
        requirement = requirement[:1997] + "..."

    existing_names = {name for name, _ in profiles}

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:
        logger.debug("room planner: auxiliary client import failed: %s", exc)
        return RoomPlan(rationale="auxiliary client unavailable")

    user_msg = USER_TEMPLATE.format(
        requirement=requirement,
        roster=_format_roster(profiles),
        max_members=max_members,
    )

    try:
        resp = call_llm(
            task="room_planner",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=2000,
            timeout=timeout,
        )
    except Exception as exc:
        logger.info("room planner: LLM call failed: %s", exc)
        return RoomPlan(rationale=f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if not parsed:
        return RoomPlan(rationale="LLM output was not valid JSON")

    rationale = str(parsed.get("rationale") or "").strip()
    room_description = str(parsed.get("room_description") or "").strip()

    raw_members = parsed.get("members") or []
    if not isinstance(raw_members, list):
        return RoomPlan(rationale="LLM output 'members' is not a list")

    # Parse + validate each member
    members: list[PlannedMember] = []
    for raw_member in raw_members:
        member = _validate_member(raw_member, existing_names)
        if member is not None:
            members.append(member)

    # Enforce max_members cap
    if len(members) > max_members:
        logger.warning(
            "room planner: LLM proposed %d members, capping to %d",
            len(members), max_members,
        )
        members = members[:max_members]

    return RoomPlan(
        rationale=rationale,
        members=members,
        room_description=room_description,
    )
