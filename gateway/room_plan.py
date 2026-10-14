"""``/room plan`` — propose a hosted-room team for a request in plain language; ``/room confirm`` creates it.

The fork's agent-room had a planner (``/room plan`` + confirm). Hosted Rooms only had ``/room create`` with a
hand-picked roster. This asks the auxiliary model (the ``kanban_decomposer`` slot: the same "split work over
described profiles" job) to pick a decider and teammates from the local profiles' role descriptions, validates
every pick against the real profile set, and holds the proposal per chat until it is confirmed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PROPOSAL_TTL_SECONDS = 30 * 60
MAX_TEAM_SIZE = 6
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "You assemble a small team of AI agent profiles for a request. Pick ONE decider (the orchestrator that "
    "splits the work, dispatches teammates and writes the final answer) and 1-5 teammates whose role "
    "descriptions fit the work. Use only profile names from the roster, never invent one, and prefer fewer "
    "members when the work is small. Reply with JSON only: "
    '{"name": "<short team name in the language of the request>", "decider": "<profile>", '
    '"members": ["<profile>", ...], "reason": "<one sentence, in the language of the request>"}. '
    "`members` excludes the decider."
)


@dataclass(frozen=True)
class RoomPlan:
    """A validated team proposal: ``decider`` plus ``members`` (all distinct local profiles)."""
    name: str
    decider: str
    members: tuple[str, ...]
    reason: str
    request: str

    @property
    def room_id(self) -> str:
        digest = hashlib.sha256(f"{self.name}\0{self.request}".encode()).hexdigest()[:8]
        return f"team-{digest}"


def members_from_profiles(profiles: list[str], decider_profile: str = "") -> list[dict[str, Any]]:
    """Hosted-room roster for bare profile names: handle and member_id derived from the profile (so the same
    roster is idempotent for the same room id), duplicates suffixed, the first ``decider_profile`` promoted."""
    seen: dict[str, int] = {}
    members: list[dict[str, Any]] = []
    for prof in profiles:
        seen[prof] = seen.get(prof, 0) + 1
        handle = prof if seen[prof] == 1 else f"{prof}{seen[prof]}"
        member: dict[str, Any] = {"member_id": handle, "profile": prof, "handle": handle,
                                  "target": {"kind": "local", "profile": prof}}
        if decider_profile and prof == decider_profile and not any(m.get("role") == "decider" for m in members):
            member["role"] = "decider"
        members.append(member)
    return members


def _roster_lines(roster: list[dict[str, str]]) -> str:
    return "\n".join(f"- {entry['name']}: {entry.get('description') or '(no description)'}" for entry in roster)


def _parse_plan(raw: str, request: str, valid: set[str]) -> RoomPlan | str:
    """A validated plan, or an error string: unknown/duplicate profiles are dropped, a missing decider fails."""
    try:
        data = json.loads(_FENCE_RE.sub("", raw.strip()))
    except (json.JSONDecodeError, TypeError):
        return "the planner returned malformed JSON"
    if not isinstance(data, dict):
        return "the planner returned malformed JSON"
    decider = str(data.get("decider") or "").strip()
    if decider not in valid:
        return f"the planner picked an unknown decider {decider!r}"
    members: list[str] = []
    for raw_member in data.get("members") or []:
        name = str(raw_member).strip()
        if name in valid and name != decider and name not in members:
            members.append(name)
    if not members:
        return "the planner picked no valid teammates"
    name = " ".join(str(data.get("name") or "").split())[:60] or "Team"
    reason = " ".join(str(data.get("reason") or "").split())[:300]
    return RoomPlan(name=name, decider=decider, members=tuple(members[: MAX_TEAM_SIZE - 1]), reason=reason,
                    request=request)


def plan_team(request: str, roster: list[dict[str, str]], *,
              call: Optional[Callable[..., tuple[Optional[str], str]]] = None) -> RoomPlan | str:
    """Ask the auxiliary model for a team; ``RoomPlan`` or a human-readable error string."""
    if len(roster) < 2:
        return "at least two local profiles are needed to form a team"
    if call is None:
        from hermes_cli.kanban_specify import _call_aux as call
    raw, reason = call(
        "room-plan", "room-plan", aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
        user=f"Request:\n{request}\n\nRoster (profile: role description):\n{_roster_lines(roster)}",
        max_tokens=2000, timeout=120, log=logger)
    if raw is None:
        return reason or "the planner is unavailable"
    return _parse_plan(raw, request, {entry["name"] for entry in roster})


class ProposalStore:
    """Pending proposals per chat (in memory: a restart just means planning again), expiring after the TTL."""

    def __init__(self, ttl: float = PROPOSAL_TTL_SECONDS, clock: Callable[[], float] = time.time):
        self._items: dict[str, tuple[RoomPlan, float]] = {}
        self._ttl, self._clock = ttl, clock

    def put(self, key: str, plan: RoomPlan) -> None:
        self._items[key] = (plan, self._clock() + self._ttl)

    def pop(self, key: str) -> Optional[RoomPlan]:
        plan, expires = self._items.pop(key, (None, 0.0))
        return plan if plan is not None and expires > self._clock() else None
