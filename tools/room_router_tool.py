"""route_to_member — the observer profile's ONLY tool.

Design reference: docs/design/agent-room/design.html §5.3 (tool + loop
termination) + §9.2 (M1's唯一保留的必做补丁A). Third milestone of the
M1→M4 delivery path.

The observer profile has ``toolsets: [room_observer]`` which grants it
exactly one tool: ``route_to_member``. When the observer's LLM emits a
``route_to_member`` tool call, three things happen in order:

  1. Observer's agent-loop calls this function with the parsed arguments.
  2. This function immediately triggers ``request_hard_interrupt`` on the
     current agent instance (retrieved via ``get_active_subagent_parent``,
     which the agent-loop entry binds at every turn — Spike 4 verified
     this ContextVar is bound for every AIAgent turn, not just for
     delegate_task subagents).
  3. Returns a routing-decision dict. The observer's turn winds down on
     the next loop iteration when the interrupt flag is checked, without
     wasting a second model call to "reply to the tool result".

The observer NEVER produces a user-facing reply — its whole job is to
emit this one tool call. Skipping the interrupt would let the model
happily generate a wrapping "OK, I've decided" text turn after the tool
result, doubling the observer's per-message cost. That's why §9.2 lists
loop termination as one of only TWO hard patches M1 must ship.

Spike 4 (§9.1) confirmed the required pieces already exist:
  * ``agent/subagent_lifecycle.py::get_active_subagent_parent`` is called
    from ``run_agent.py::AIAgent`` via ``bind_subagent_parent(self)`` at
    every ``run_conversation`` entry (turn_lifecycle:7889) — every agent
    turn, not just delegate children.
  * ``agent/interrupt_compat.py::request_hard_interrupt`` is the existing
    normalized entry point that handles both new (``hard_interrupt``) and
    legacy (``interrupt``) ABIs.

No agent-runtime changes required.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Sentinel action name in the return dict so M1.5's router can distinguish
# a route_to_member decision from any other tool return shape.
ROUTE_ACTION = "route_to_member"


def route_to_member(
    member,  # str or list[str] — see M3 multi-member routing
    reason: str,
    is_new_topic: bool = False,
) -> str:
    """The observer profile's only tool: emit a routing decision.

    Parameters
    ----------
    member : str | list[str]
        Either a single profile name (M1 single-routing) or a list of
        profile names (M3 concurrent multi-member dispatch). The router
        validates each against the actual roster; here we only normalize
        + pass through. An empty value → empty string → router falls
        back to default_member (M1-B4).
    reason : str
        1-sentence rationale. When the observer is switching to a
        different member than last time on a continuing topic, the
        SOUL.md template (M1.2 §8 Rule A) instructs it to include a
        summary of the previous member's last reply prefixed with
        "上一位处理人 <name> 的回复摘要:". M1.5's router (STEP 4.5)
        parses that prefix out and forwards the summary to the new
        member's message as a context prefix. (Under M3, the projection
        layer replaces the summary mechanism.)
    is_new_topic : bool
        Observer's assessment of whether this message starts a new
        topic. Used to update ``last_routed_member`` cache invalidation
        in the router.

    Returns
    -------
    str
        JSON-encoded routing decision. String (not dict) because the
        agent tool-call machinery expects tool return values to be
        JSON-serializable text — dicts get str()-formatted with single
        quotes which breaks any downstream JSON parse.

    Side effect
    -----------
    Fires ``request_hard_interrupt`` on the calling agent so the
    observer's turn ends immediately after this call returns. If the
    interrupt cannot be delivered (agent instance unreachable, ABI
    mismatch, etc.) the routing decision is still returned successfully
    — the router will still get a valid decision even if the observer
    wastes one extra iteration on wrap-up text.
    """
    resolved_member = str(member or "").strip()
    resolved_reason = str(reason or "").strip()
    resolved_is_new_topic = bool(is_new_topic)

    # M3: normalize member into either a single str (M1 legacy) OR a
    # list[str] (M3 concurrent multi-member). The router looks at the
    # returned "member" field's TYPE to choose single vs concurrent
    # dispatch — so we preserve the LLM's original shape here.
    if isinstance(member, list):
        resolved_members = [str(m).strip() for m in member if str(m).strip()]
        # De-dupe preserving order
        seen: set = set()
        deduped: list[str] = []
        for m in resolved_members:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        resolved_member = deduped  # keep as list for router
    else:
        resolved_member = str(member or "").strip()

    # Attempt to terminate the observer's agent loop immediately. Wrapped
    # in a broad try/except because a failure to interrupt must NEVER
    # break the routing decision itself — degraded behavior (extra
    # wrap-up iteration) is acceptable, silent decision loss is not.
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        from agent.interrupt_compat import request_hard_interrupt

        active_agent = get_active_subagent_parent()
        if active_agent is not None:
            interrupted = request_hard_interrupt(
                active_agent,
                f"route_to_member decided: {resolved_member!r}",
            )
            if not interrupted:
                logger.debug(
                    "route_to_member: hard-interrupt returned False; "
                    "observer loop may run one more wrap-up iteration"
                )
        else:
            # This should never happen in a real agent turn (Spike 4
            # confirmed the ContextVar is bound at turn entry), so log
            # loudly if it does — likely means the tool was invoked
            # from a test harness or bypass path.
            logger.warning(
                "route_to_member: no active parent agent bound in ContextVar; "
                "observer loop will not be terminated. This should not happen "
                "in a live agent turn — check that bind_subagent_parent is in "
                "the call stack."
            )
    except Exception as exc:  # noqa: BLE001 — deliberately broad
        logger.warning(
            "route_to_member: hard-interrupt raised %s (%s); "
            "returning routing decision anyway",
            type(exc).__name__,
            exc,
        )

    return json.dumps(
        {
            "action": ROUTE_ACTION,
            "member": resolved_member,
            "reason": resolved_reason,
            "is_new_topic": resolved_is_new_topic,
        },
        ensure_ascii=False,
    )


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

ROUTE_TO_MEMBER_SCHEMA = {
    "name": "route_to_member",
    "description": (
        "Route the current room message to one of the room's members. "
        "This is the ONLY tool available to a room observer agent — "
        "every observer decision must be emitted via exactly one call "
        "to this tool. You have no other tools, no memory writes, no "
        "user-facing reply. Do not produce any assistant text; just "
        "call this tool with your routing decision.\n\n"
        "How to pick the member:\n"
        "1. Look at the incoming message.\n"
        "2. If it's a continuation of the ongoing topic, keep routing "
        "to the same member as the last routed decision.\n"
        "3. If it's a NEW topic, pick the member whose description "
        "best matches the required expertise.\n"
        "4. When switching to a DIFFERENT member than last time on a "
        "continuing topic, put a 1-sentence summary of the previous "
        "member's last reply in the `reason` field prefixed with "
        "'上一位处理人 <name> 的回复摘要:' — the router will forward "
        "that summary to the new member.\n"
        "5. If the message is truly ambiguous, use the room's default "
        "member with reason 'fallback'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "member": {
                "oneOf": [
                    {
                        "type": "string",
                        "description": (
                            "A single profile name from the room's member roster. "
                            "Must match a name shown in the '## Members' section of "
                            "this observer's SOUL.md exactly (case-sensitive). "
                            "Use this form for single-member routing (M1 legacy)."
                        ),
                    },
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                        "description": (
                            "An array of profile names for CONCURRENT multi-member "
                            "dispatch (M3+). Use this when the message spans multiple "
                            "expertise domains (e.g. '我要退款并咨询发票问题' → "
                            "['client_svc', 'finance']). All listed members will run "
                            "their turns in parallel with the shared room context, "
                            "and their replies will all be delivered to the group."
                        ),
                    },
                ],
                "description": (
                    "Either a single profile name (str) for single-member routing, "
                    "or an array of profile names for concurrent multi-member dispatch. "
                    "If none fit, use the room's default_member."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "1-sentence rationale for the routing choice. On "
                    "member-switch continuations, prefix with "
                    "'上一位处理人 <previous_member> 的回复摘要: <summary>' "
                    "so the router can forward context to the new member. "
                    "For multi-member routing, briefly explain why each member is needed."
                ),
            },
            "is_new_topic": {
                "type": "boolean",
                "description": (
                    "True if this message starts a new topic (not a "
                    "continuation of the ongoing conversation). Used "
                    "to invalidate the router's last-routed-member cache."
                ),
                "default": False,
            },
        },
        "required": ["member", "reason"],
    },
}

# Register route_to_member into the tool registry so
# registry.get_definitions({'route_to_member'}) returns its schema.
# Without this, _compute_tool_definitions finds the tool name via
# resolve_toolset('room_observer') but get_definitions returns 0
# schemas because the registry has no entry for it.
from tools.registry import registry as _registry

_registry.register(
    name="route_to_member",
    toolset="room_observer",
    schema=ROUTE_TO_MEMBER_SCHEMA,
    handler=route_to_member,
    check_fn=None,
    is_async=False,
    description="Route a room message to a member profile (observer-only).",
    emoji="🔀",
)
