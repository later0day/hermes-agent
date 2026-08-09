"""M4 · Task decomposition tool for room observers.

Second tool in the room_observer toolset (alongside route_to_member).
Called when the observer decides the incoming message is a COMPLEX task
that benefits from being broken into subtasks with dependencies.

Design ref: docs/design/agent-room/design.html §2.5 (M4 明确包含).

Interaction model:
  1. Observer sees a complex user message (e.g. "帮我起草合同 + 找财务算
     成本 + 发给客户").
  2. Instead of route_to_member, observer calls decompose_and_route
     with a task graph:
       [
         {"title": "起草合同", "assignee": "legal", "parents": []},
         {"title": "算成本",   "assignee": "finance", "parents": []},
         {"title": "发给客户", "assignee": "client_svc", "parents": [0, 1]},
       ]
  3. Tool returns JSON string with action=decompose_and_route and the
     task graph, then fires request_hard_interrupt to end observer's
     turn (same mechanism as route_to_member).
  4. Router picks up the decomposed action and hands it to
     agent_room_task_orchestrator (M4.3) which executes the DAG.

Constraints:
  * All assignees MUST be current room members (validated by the
    router before dispatch; here we only trim + preserve).
  * Cycles are rejected downstream (topological sort will fail).
  * Empty tasks list is treated as "not actually complex, don't
    dispatch" (returns empty action; router falls back to
    default_member).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


DECOMPOSE_ACTION = "decompose_and_route"


def decompose_and_route(
    tasks: list,
    reason: str = "",
    is_new_topic: bool = True,
) -> str:
    """The observer's second tool: emit a task DAG for orchestrated dispatch.

    Parameters
    ----------
    tasks : list[dict]
        Each entry must have:
          - "title": str — 1-line task label
          - "body": str — full task description (may be empty)
          - "assignee": str — MUST match a room member profile
          - "parents": list[int] — 0-based indices into this same list;
            defines dependency edges (must run after all parents)
        The router validates assignees against the room roster; empty
        or unknown assignees are dropped or reassigned to default_member.
    reason : str
        1-sentence rationale for choosing decompose over route.
    is_new_topic : bool
        Observer's assessment of whether this starts a new topic.

    Returns
    -------
    str
        JSON with the decomposition decision. Same shape as route_to_member's
        return but action=decompose_and_route.

    Side effect
    -----------
    Same as route_to_member: fires request_hard_interrupt on the calling
    agent so the observer's turn ends immediately. Failure to interrupt
    doesn't invalidate the decision.
    """
    # Normalize the tasks payload — drop malformed entries but keep
    # the list ordering so parents indices remain valid.
    resolved_tasks: list[dict[str, Any]] = []
    if isinstance(tasks, list):
        for idx, entry in enumerate(tasks):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            body = str(entry.get("body") or "").strip()
            assignee = str(entry.get("assignee") or "").strip()
            raw_parents = entry.get("parents") or []
            parents: list[int] = []
            if isinstance(raw_parents, list):
                for p in raw_parents:
                    if isinstance(p, bool):
                        continue  # bool is subclass of int in Python
                    if isinstance(p, int) and 0 <= p < len(tasks) and p != idx:
                        parents.append(p)
            resolved_tasks.append({
                "title": title[:200],
                "body": body,
                "assignee": assignee,
                "parents": parents,
            })

    resolved_reason = str(reason or "").strip()
    resolved_is_new_topic = bool(is_new_topic)

    # Fire hard_interrupt on the observer's agent loop (Spike 4 pattern).
    # Broad try/except — failure to interrupt must never lose the
    # decomposition decision.
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        from agent.interrupt_compat import request_hard_interrupt

        active_agent = get_active_subagent_parent()
        if active_agent is not None:
            interrupted = request_hard_interrupt(
                active_agent,
                f"decompose_and_route decided: {len(resolved_tasks)} subtasks",
            )
            if not interrupted:
                logger.debug(
                    "decompose_and_route: hard-interrupt returned False; "
                    "observer loop may run one more iteration"
                )
        else:
            logger.warning(
                "decompose_and_route: no active parent agent bound in "
                "ContextVar; observer loop will not be terminated."
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "decompose_and_route: hard-interrupt raised %s (%s); "
            "returning decomposition anyway",
            type(exc).__name__, exc,
        )

    return json.dumps(
        {
            "action": DECOMPOSE_ACTION,
            "tasks": resolved_tasks,
            "reason": resolved_reason,
            "is_new_topic": resolved_is_new_topic,
        },
        ensure_ascii=False,
    )


DECOMPOSE_AND_ROUTE_SCHEMA = {
    "name": "decompose_and_route",
    "description": (
        "Decompose a COMPLEX user request into a DAG of subtasks and "
        "route each to a specific room member. Use this ONLY when the "
        "user's message clearly requires MULTIPLE INDEPENDENT STEPS "
        "with dependencies (e.g. 'draft the contract, then have finance "
        "compute the cost, then send it to the client'). For simple "
        "single-question or single-domain requests, use route_to_member "
        "instead — this tool is heavier and should not be used for chat.\n\n"
        "How to build the task list:\n"
        "1. Break the request into individual actionable steps.\n"
        "2. Each step's `assignee` MUST be a profile name from the room's "
        "'## Members' section (exact match, case-sensitive).\n"
        "3. If step B depends on step A's output, set B's `parents` to "
        "[A's index]. Independent tasks can run in parallel.\n"
        "4. Keep it to 2-10 subtasks. If it fits in 1 subtask, use "
        "route_to_member instead.\n"
        "5. No self-parent (parents cannot include own index). "
        "Cycles will be rejected downstream.\n\n"
        "After emitting the tool call, the observer's turn ends. The "
        "task orchestrator runs the DAG, then triggers a synthesis "
        "turn (observer sees all subtask results and writes the final "
        "user-visible reply)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "1-line label for this subtask.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Full task description for the assignee.",
                        },
                        "assignee": {
                            "type": "string",
                            "description": (
                                "Profile name from the room's roster. "
                                "Must exact-match a member name in SOUL.md's "
                                "'## Members' section."
                            ),
                        },
                        "parents": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "default": [],
                            "description": (
                                "0-based indices of tasks this one depends on. "
                                "Task will start only after all listed parents "
                                "complete. Empty list = no dependencies (parallel-ready)."
                            ),
                        },
                    },
                    "required": ["title", "assignee"],
                },
                "description": "The subtask DAG. Indices in 'parents' reference this same array.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "1-sentence rationale for decomposing rather than "
                    "single-routing (e.g. 'user asks for 3 dependent "
                    "steps: draft, cost, send')."
                ),
            },
            "is_new_topic": {
                "type": "boolean",
                "default": True,
                "description": (
                    "True if this decomposition starts a new topic. "
                    "Complex tasks are almost always new topics."
                ),
            },
        },
        "required": ["tasks", "reason"],
    },
}


# Register into the tool registry so _compute_tool_definitions can find it.
# Same pattern as route_to_member.
from tools.registry import registry as _registry

_registry.register(
    name="decompose_and_route",
    toolset="room_observer",
    schema=DECOMPOSE_AND_ROUTE_SCHEMA,
    handler=decompose_and_route,
    check_fn=None,
    is_async=False,
    description="Decompose a complex user request into a DAG of subtasks (observer-only).",
    emoji="🧩",
)
