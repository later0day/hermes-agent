"""Member-to-member handoff depth policy for Agent Rooms.

Ported from hermes-studio's group-chat/handoff-depth.ts. Bounds the
chained ``@member`` handoffs that ``agent_room_mentions.resolve_mention_
targets`` can trigger, so one user message cannot loop members forever.

The observer picks the FIRST responder (depth 0). If that member's reply
@mentions another member, we re-dispatch at depth 1, and so on, until a
reply has no @mention (natural end) or ``depth`` reaches ``max_depth``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_HANDOFF_DEPTH = 4


@dataclass(frozen=True)
class HandoffPolicy:
    enabled: bool
    max_depth: int | None  # None == unlimited when unlimited=True
    unlimited: bool


def _finite_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def recommended_handoff_depth(active_member_count: int) -> int:
    count = max(0, _finite_int(active_member_count) or 0)
    return max(DEFAULT_HANDOFF_DEPTH, count + 1)


def resolve_handoff_policy(
    *,
    enabled: Any = True,
    max_depth: Any = None,
    unlimited: Any = False,
    server_default: Any = None,
) -> HandoffPolicy:
    is_enabled = True if enabled is None else bool(enabled)
    if not is_enabled:
        return HandoffPolicy(enabled=False, max_depth=None, unlimited=False)
    if unlimited is True:
        return HandoffPolicy(enabled=True, max_depth=None, unlimited=True)
    room_depth = _finite_int(max_depth)
    default_depth = _finite_int(server_default)
    resolved = room_depth if room_depth is not None else (
        default_depth if default_depth is not None else DEFAULT_HANDOFF_DEPTH
    )
    return HandoffPolicy(
        enabled=True,
        max_depth=max(1, resolved),
        unlimited=False,
    )


def should_route_handoff(depth: Any, policy: HandoffPolicy) -> bool:
    if not policy.enabled:
        return False
    if policy.unlimited:
        return True
    normalized = max(0, _finite_int(depth) or 0)
    return normalized < (policy.max_depth or DEFAULT_HANDOFF_DEPTH)


def next_mention_depth(depth: Any) -> int:
    return max(0, _finite_int(depth) or 0) + 1
