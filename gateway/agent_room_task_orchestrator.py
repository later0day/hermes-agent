"""M4.3 · Agent Room task orchestrator.

Design ref: docs/design/agent-room/design.html §2.5 + §10.5.

Runs the DAG returned by ``decompose_and_route`` (M4.2):
  * Topological sort → level-by-level execution
  * Independent tasks in the same level run CONCURRENTLY
    (asyncio.gather, like M3.5's multi-member dispatch)
  * Dependent tasks wait for all their parents to complete
  * Failed parent → dependent skipped, failure surfaced to synthesis
  * Room-scoped: assignees MUST be in room.members (M4-B3 boundary)
  * Cycle-safe: rejects cyclic DAGs at plan time (M4-B2)
  * Fence-aware: mid-flight structural change to the room drops
    remaining subtasks (M4-B5)

Design tradeoffs:
  * Pure async in-memory execution — no persistence between levels.
    A gateway restart mid-DAG loses in-flight tasks (M4-B5 accepts
    this: complex multi-step tasks are not durability-critical yet).
  * The dispatcher callable is INJECTED, mirroring
    AgentRoomRouter's dependency-injection pattern. Real dispatcher
    runs the member's LLM turn under _profile_runtime_scope; test
    dispatchers can be mock AsyncMocks.
  * Each task's result is appended to the shared messages store so
    the synthesis turn (M4.4) sees them in projection order.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlannedSubtask:
    """One node in the DAG the observer emitted via decompose_and_route."""
    index: int          # 0-based position in the observer's tasks list
    title: str
    body: str
    assignee: str       # profile name (validated against roster)
    parents: tuple[int, ...] = ()

    @property
    def has_parents(self) -> bool:
        return len(self.parents) > 0


@dataclass
class SubtaskResult:
    """Runtime result carrying either the assignee's reply or a failure."""
    index: int
    title: str
    assignee: str
    status: str         # "success" | "failed" | "skipped_parent_failed" | "fenced"
    reply: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "assignee": self.assignee,
            "status": self.status,
            "reply": self.reply,
            "error": self.error,
        }


class OrchestratorError(Exception):
    """Raised for structural DAG problems (cycles, unknown assignees).
    Runtime failures during a subtask are captured as SubtaskResult,
    not raised."""
    pass


# ──────────────────────────────────────────────────────────────────────────
# DAG validation + topological levels
# ──────────────────────────────────────────────────────────────────────────


def build_subtasks(
    raw_tasks: list[dict],
    room_members: list[str] | tuple[str, ...],
    *,
    default_member: str = "",
) -> list[PlannedSubtask]:
    """Convert the observer's raw task list into a validated
    PlannedSubtask list. Applies assignee validation (M4-B3):
      * assignee ∉ room_members → rewrite to default_member if that's
        in the roster, else drop.
      * empty title → drop.
    Also strips duplicate parent entries.
    """
    roster = list(room_members)
    roster_set = set(roster)
    fallback = default_member if default_member in roster_set else (
        roster[0] if roster else ""
    )

    subtasks: list[PlannedSubtask] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        body = str(entry.get("body") or "").strip()
        assignee = str(entry.get("assignee") or "").strip()
        if assignee not in roster_set:
            if not fallback:
                # No usable assignee at all — drop this subtask.
                logger.warning(
                    "orchestrator: subtask idx=%d has unknown assignee %r "
                    "and no fallback available; dropping",
                    idx, assignee,
                )
                continue
            logger.info(
                "orchestrator: subtask idx=%d assignee %r not in roster %s "
                "→ rewriting to %r",
                idx, assignee, roster, fallback,
            )
            assignee = fallback
        raw_parents = entry.get("parents") or []
        parents: list[int] = []
        if isinstance(raw_parents, list):
            seen: set[int] = set()
            for p in raw_parents:
                if isinstance(p, bool):
                    continue
                if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx and p not in seen:
                    parents.append(p)
                    seen.add(p)
        subtasks.append(PlannedSubtask(
            index=idx, title=title[:200], body=body,
            assignee=assignee, parents=tuple(parents),
        ))

    _reject_cycles(subtasks)
    return subtasks


def _reject_cycles(subtasks: list[PlannedSubtask]) -> None:
    """Raise OrchestratorError if the DAG contains a cycle. Kahn's
    algorithm — count in-degree from parents, peel off zero-in nodes,
    and if we can't reach every subtask the remainder is a cycle."""
    indexed = {t.index: t for t in subtasks}
    in_degree: dict[int, int] = {}
    for t in subtasks:
        in_degree[t.index] = 0
    for t in subtasks:
        for p in t.parents:
            if p in indexed:
                in_degree[t.index] += 1

    ready: deque[int] = deque(i for i, d in in_degree.items() if d == 0)
    visited = 0
    dependents_of: dict[int, list[int]] = defaultdict(list)
    for t in subtasks:
        for p in t.parents:
            dependents_of[p].append(t.index)

    while ready:
        n = ready.popleft()
        visited += 1
        for dep in dependents_of.get(n, ()):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready.append(dep)

    if visited != len(subtasks):
        raise OrchestratorError(
            f"DAG contains cycle(s): visited {visited} of {len(subtasks)} subtasks"
        )


def topological_levels(subtasks: list[PlannedSubtask]) -> list[list[PlannedSubtask]]:
    """Group subtasks by dependency depth. Level 0 = no parents. Level
    N = all parents are in levels < N. Same-level tasks run concurrently."""
    indexed = {t.index: t for t in subtasks}
    depth: dict[int, int] = {}

    def _depth(idx: int, chain: set[int]) -> int:
        if idx in depth:
            return depth[idx]
        if idx in chain:
            # This should be unreachable if _reject_cycles has already run.
            raise OrchestratorError(f"cycle involving subtask idx={idx}")
        t = indexed[idx]
        if not t.parents:
            depth[idx] = 0
            return 0
        chain2 = chain | {idx}
        d = 1 + max(
            (_depth(p, chain2) for p in t.parents if p in indexed),
            default=-1,
        )
        depth[idx] = d
        return d

    for t in subtasks:
        _depth(t.index, set())

    max_d = max(depth.values(), default=-1)
    levels: list[list[PlannedSubtask]] = [[] for _ in range(max_d + 1)]
    for t in sorted(subtasks, key=lambda x: x.index):
        levels[depth[t.index]].append(t)
    return levels


# ──────────────────────────────────────────────────────────────────────────
# Execution
# ──────────────────────────────────────────────────────────────────────────


# The dispatcher signature — same shape M3's router uses so real code
# can pass the same underlying callable for both direct member routing
# and orchestrated subtask dispatch.
SubtaskDispatcher = Callable[
    [str, str, str, list[dict]],   # member, session_id, message_text, projected_history
    Awaitable[str],                 # returns the member's reply text
]


# Optional per-subtask fence check — returns True when the room's
# structural state has moved on and remaining subtasks should be
# abandoned (matches AgentRoomRouter's Fence gate pattern).
FenceGate = Callable[[str], bool]  # (member_session_id) → is_fenced


@dataclass
class OrchestrationResult:
    """The complete outcome of a DAG execution."""
    results: list[SubtaskResult] = field(default_factory=list)
    fenced_mid_flight: bool = False
    total_subtasks: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_subtasks": self.total_subtasks,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "fenced_mid_flight": self.fenced_mid_flight,
            "results": [r.to_dict() for r in self.results],
        }


async def orchestrate(
    subtasks: list[PlannedSubtask],
    *,
    room_id: str,
    dispatcher: SubtaskDispatcher,
    fence_gate: Optional[FenceGate] = None,
    projection_provider: Optional[Callable[[str], list[dict]]] = None,
    session_id_for_member: Optional[Callable[[str, int], str]] = None,
) -> OrchestrationResult:
    """Execute the DAG level-by-level.

    Parameters
    ----------
    subtasks : list[PlannedSubtask]
        Output of ``build_subtasks``. Assumed cycle-free.
    room_id : str
        The room's canonical ID. Passed to session-id builder.
    dispatcher : SubtaskDispatcher
        Callable that runs one member's LLM turn and returns its reply.
    fence_gate : Callable[[session_id], bool], optional
        If provided, called before EACH subtask dispatch. If it returns
        True, all remaining subtasks are marked "fenced" and the
        orchestration returns early. Same semantic as
        AgentRoomStore.is_fenced.
    projection_provider : Callable[[member_name], list[dict]], optional
        Per-subtask, returns the projected history the member should see.
        If None, empty history is used (fine for tests / simple flows).
    session_id_for_member : Callable[[member_name, subtask_idx], str], optional
        Session-id builder. Default: 'room_member_task:{room_id}:{member}:{idx}'.

    Returns
    -------
    OrchestrationResult
        Ordered by subtask index; each subtask has status +
        reply/error. The synthesis turn (M4.4) reads this to build
        the final user-facing message.
    """
    if not subtasks:
        return OrchestrationResult(total_subtasks=0)

    if session_id_for_member is None:
        def session_id_for_member(member: str, idx: int) -> str:  # type: ignore[misc]
            return f"room_member_task:{room_id}:{member}:{idx}"

    if projection_provider is None:
        def projection_provider(member: str) -> list[dict]:  # type: ignore[misc]
            return []

    levels = topological_levels(subtasks)
    logger.info(
        "orchestrator: room=%s starting DAG with %d subtasks across %d levels",
        room_id, len(subtasks), len(levels),
    )

    # Index → SubtaskResult so downstream levels can check parents.
    results_by_idx: dict[int, SubtaskResult] = {}
    out = OrchestrationResult(total_subtasks=len(subtasks))

    for level_i, level in enumerate(levels):
        # Any-parent-failed check: if a task's parent failed OR was
        # skipped, mark it skipped. Don't dispatch to LLM.
        active_this_level: list[PlannedSubtask] = []
        for t in level:
            failed_parent = False
            for p_idx in t.parents:
                p_result = results_by_idx.get(p_idx)
                if p_result is None or p_result.status not in ("success",):
                    failed_parent = True
                    break
            if failed_parent:
                skipped_result = SubtaskResult(
                    index=t.index,
                    title=t.title,
                    assignee=t.assignee,
                    status="skipped_parent_failed",
                )
                results_by_idx[t.index] = skipped_result
                out.results.append(skipped_result)
                out.skipped += 1
                logger.info(
                    "orchestrator: room=%s subtask idx=%d skipped "
                    "(parent failed/skipped)", room_id, t.index,
                )
            else:
                active_this_level.append(t)

        if not active_this_level:
            continue

        # Fence gate: check BEFORE dispatching this level. If any active
        # subtask's session is fenced, mark all remaining subtasks (this
        # level + later levels) as fenced.
        if fence_gate is not None:
            for t in active_this_level:
                sid = session_id_for_member(t.assignee, t.index)
                if fence_gate(sid):
                    logger.info(
                        "orchestrator: room=%s fenced before level %d dispatch; "
                        "aborting remainder", room_id, level_i,
                    )
                    out.fenced_mid_flight = True
                    for u in active_this_level:
                        u_result = SubtaskResult(
                            index=u.index,
                            title=u.title,
                            assignee=u.assignee,
                            status="fenced",
                        )
                        results_by_idx[u.index] = u_result
                        out.results.append(u_result)
                    # Any tasks in later levels also fenced.
                    for later in levels[level_i + 1:]:
                        for u in later:
                            if u.index in results_by_idx:
                                continue
                            u_result = SubtaskResult(
                                index=u.index,
                                title=u.title,
                                assignee=u.assignee,
                                status="fenced",
                            )
                            results_by_idx[u.index] = u_result
                            out.results.append(u_result)
                    out.results.sort(key=lambda r: r.index)
                    return out

        # Dispatch this level's active tasks concurrently.
        async def _run_one(task: PlannedSubtask) -> SubtaskResult:
            sid = session_id_for_member(task.assignee, task.index)
            proj = projection_provider(task.assignee)
            # Build the member's message: title + body (body may be empty).
            body = task.body.strip()
            member_msg = task.title if not body else f"{task.title}\n\n{body}"
            try:
                reply = await dispatcher(task.assignee, sid, member_msg, proj)
                return SubtaskResult(
                    index=task.index,
                    title=task.title,
                    assignee=task.assignee,
                    status="success",
                    reply=reply or "",
                )
            except Exception as exc:  # noqa: BLE001 — per-subtask isolation
                logger.warning(
                    "orchestrator: room=%s subtask idx=%d (%s) failed: %s",
                    room_id, task.index, task.assignee, exc,
                )
                return SubtaskResult(
                    index=task.index,
                    title=task.title,
                    assignee=task.assignee,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        gathered = await asyncio.gather(*[_run_one(t) for t in active_this_level])
        for r in gathered:
            results_by_idx[r.index] = r
            out.results.append(r)
            if r.status == "success":
                out.completed += 1
            elif r.status == "failed":
                out.failed += 1

    out.results.sort(key=lambda r: r.index)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Formatting helpers for synthesis turn (M4.4)
# ──────────────────────────────────────────────────────────────────────────


def render_subtask_results_for_synthesis(result: OrchestrationResult) -> str:
    """Format the orchestration outcome for injection into the
    observer's synthesis turn. Returns a plaintext block the observer
    can read verbatim to compose the final user-facing reply.

    Format:

        Subtask results:
        [0] "Draft contract" · client_svc · success
            <reply text>
        [1] "Compute cost" · finance · failed
            error: RuntimeError: LLM timeout
        [2] "Send to client" · client_svc · skipped_parent_failed
    """
    if not result.results:
        return "(no subtasks)"
    lines = [
        f"Subtask results ({result.completed}/{result.total_subtasks} "
        f"completed, {result.failed} failed, {result.skipped} skipped"
        + (", fenced mid-flight" if result.fenced_mid_flight else "")
        + "):",
        "",
    ]
    for r in result.results:
        lines.append(f"[{r.index}] {r.title!r} · {r.assignee} · {r.status}")
        if r.status == "success" and r.reply:
            body = r.reply.strip()
            if len(body) > 800:
                body = body[:800] + "…"
            for ln in body.split("\n"):
                lines.append(f"    {ln}")
        elif r.status == "failed" and r.error:
            lines.append(f"    error: {r.error}")
        elif r.status == "skipped_parent_failed":
            missing = ", ".join(str(p) for p in ())
            lines.append(f"    (skipped because parent subtask did not succeed)")
        elif r.status == "fenced":
            lines.append(f"    (skipped because room state changed mid-flight)")
        lines.append("")
    return "\n".join(lines).rstrip()
