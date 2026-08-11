"""Agent Room routing — the M1 §6.1 five-step async flow.

Design reference: docs/design/agent-room/design.html §6 (runtime routing)
+ §5.4 (N4 lightweight classifier) + §6.3 (Fence check) + §8 Rule B
(cross-member summary router side). Fifth milestone of the M1→M4
delivery path.

The router is intentionally decoupled from gateway/run.py's
GatewayRunner: it receives everything it needs (agent-runner callable,
aux-LLM classifier callable, notifier callable, store) via constructor
injection. This lets:

  1. Unit tests exercise the full five-step flow without spinning up
     a real gateway (no _profile_runtime_scope, no _run_agent_inner
     real call, no DingTalk network — every dependency is mock-able).
  2. M1.7's gateway/run.py-side integration is a thin adapter that
     assembles a Router with the real callables and hands off to it.
  3. M2/M3/M4 can swap individual dependencies (e.g. M3 replaces the
     ``dispatch_member`` callable with a projection-aware version)
     without rewriting the router state machine.

§6.1 flow ownership (mapping to methods below):
  Step 1 · send acknowledgement          → _send_acknowledgement
  Step 2 · lightweight classify (N4)     → _classify
  Step 3 · route (either reuse or full)  → _decide_target_member
                                            (calls _run_observer_turn
                                             on the miss path)
  Step 4 · edit ack message              → _update_ack_message
  Step 4.5 · cross-member summary (§8 B) → _extract_cross_member_context
  Step 5 · dispatch member turn          → _dispatch_member

Fence check (§6.3): applied at two gates — before observer emit AND
before member dispatch. A room that was unbound/deleted/re-membered
mid-flight sees its in-flight turn's output silently dropped.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from gateway.agent_room_store import AgentRoom, AgentRoomStore

logger = logging.getLogger(__name__)


# Regex for §8 Rule B: SOUL.md instructs the observer to prefix its
# reason with "上一位处理人 <name> 的回复摘要:" when switching members
# mid-topic. Captures the previous member's name and the summary text.
_CROSS_MEMBER_SUMMARY_RE = re.compile(
    r"上一位处理人\s*(\S+?)\s*的回复摘要\s*[:：]\s*(.+)",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifierResult:
    """N4 aux-LLM classifier output. See §5.4."""
    is_new_topic: bool
    confidence: float

    def should_reuse_last_routed(self) -> bool:
        """Reuse condition per §5.4: not a new topic AND high confidence."""
        return (not self.is_new_topic) and self.confidence > 0.7


@dataclass(frozen=True)
class RoutingDecision:
    """The result of Step 3 — who the message goes to and why.

    M3: target_member can be either a str (single-member dispatch,
    M1 legacy) or a list[str] (concurrent multi-member dispatch).
    Router callers should check ``.is_multi`` before iterating.

    M4: when the observer decides the request is a multi-step task it
    calls ``decompose_and_route`` instead of ``route_to_member``. That
    yields a decision with ``action == "decompose_and_route"`` and a
    populated ``decompose_tasks`` DAG. ``target_member`` is unused on
    that path (the orchestrator resolves per-subtask assignees).
    """
    target_member: Any  # str or list[str]
    reason: str
    is_new_topic: bool
    # True if we reused last_routed_member instead of running a full
    # observer turn (§5.4 N4 fast path). Useful for A/B metric
    # "N4 沿用率 ≥40%".
    reused_last_route: bool
    # M4: "route" (default, single/multi member) or "decompose_and_route"
    # (the observer emitted a subtask DAG to be orchestrated).
    action: str = "route"
    # M4: the raw subtask list from decompose_and_route, only set when
    # action == "decompose_and_route". Each entry:
    #   {"title", "body", "assignee", "parents"}.
    decompose_tasks: Optional[list[dict]] = None

    @property
    def is_decompose(self) -> bool:
        """True when the observer requested task decomposition (M4)."""
        return self.action == "decompose_and_route"

    @property
    def is_multi(self) -> bool:
        """True when the observer chose multiple members for concurrent dispatch."""
        return isinstance(self.target_member, list)

    @property
    def target_members(self) -> list[str]:
        """Always returns a list — single-member decisions become [name]."""
        if isinstance(self.target_member, list):
            return list(self.target_member)
        if isinstance(self.target_member, str) and self.target_member:
            return [self.target_member]
        return []


@dataclass(frozen=True)
class CrossMemberContext:
    """§8 Rule B parsed context, when the observer includes a summary
    of the previous member's last reply. All-None means no summary
    was found (or wasn't well-formed); router should send the raw
    original message unchanged."""
    previous_member: Optional[str]
    summary: Optional[str]

    def has_summary(self) -> bool:
        return bool(self.previous_member and self.summary)


# ---------------------------------------------------------------------------
# Injected-callable types
# ---------------------------------------------------------------------------


# Sends an immediate ack message to the source and returns an
# implementation-specific handle usable for later edit_message.
# Real impl: DingTalk session_webhook. Test impl: MagicMock.
AckSender = Callable[[Any, str], Awaitable[Any]]

# Given an ack handle + new text, edit the previously-sent message.
AckEditor = Callable[[Any, str], Awaitable[None]]

# N4 classifier: last 5 messages + last_routed_member → ClassifierResult.
Classifier = Callable[[list[dict], Optional[str]], Awaitable[ClassifierResult]]

# Runs the observer profile's turn (with SOUL/tools locked to
# room_observer) and returns the routing decision the observer emitted.
# Under M1.7 this is wired to gateway/run.py's _run_agent_inner wrapped
# in _profile_runtime_scope; under tests it's a MagicMock.
ObserverRunner = Callable[
    [str, str, Any, list[dict], str],  # observer_profile, session_id, source, history, message
    Awaitable[RoutingDecision],
]

# Dispatches to a member profile's turn with the pre-processed message
# (may include an §8 Rule B context prefix) and returns the member's
# final reply text. Reply delivery to the source is the caller's
# responsibility — this callable ONLY runs the turn and returns the
# text. Kept narrow so tests can assert on the exact string handed to
# the member without also mocking a webhook.
MemberDispatcher = Callable[
    [str, str, Any, list[dict], str],  # member_profile, session_id, source, history, message
    Awaitable[str],
]


# M4.4: runs the observer's SYNTHESIS turn — a second observer turn,
# reusing the same observer session, fed the rendered subtask results.
# Returns the final user-facing reply text the observer composed. Kept
# optional (constructor defaults to None) so M1-M3 routers that never
# see a decompose decision don't need to provide it.
SynthesisRunner = Callable[
    [str, str, Any, str],  # observer_profile, session_id, source, rendered_results
    Awaitable[str],
]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class AgentRoomRouter:
    """Coordinates the §6.1 five-step routing flow for a single message.

    Instances are safe to share across concurrent messages: all mutable
    state is either owned by the injected store (Fence set) or scoped
    per-room via the ``_last_routed_member`` dict guarded by an asyncio
    Lock. There is intentionally NO per-message state on ``self`` — a
    process_message call passes its own state via arguments only.
    """

    def __init__(
        self,
        *,
        store: AgentRoomStore,
        ack_sender: AckSender,
        ack_editor: AckEditor,
        classifier: Classifier,
        observer_runner: ObserverRunner,
        member_dispatcher: MemberDispatcher,
        synthesis_runner: Optional["SynthesisRunner"] = None,
    ) -> None:
        self._store = store
        self._ack_sender = ack_sender
        self._ack_editor = ack_editor
        self._classifier = classifier
        self._observer_runner = observer_runner
        self._member_dispatcher = member_dispatcher
        # M4.4: only needed on the decompose_and_route path. When absent,
        # a decompose decision falls back to concatenating raw subtask
        # replies (still correct, just not LLM-synthesized).
        self._synthesis_runner = synthesis_runner

        # §5.4: last_routed_member cache. Keyed by room_id; process-local,
        # not persisted. A restart forces the first message to run a full
        # observer turn (M1-B11 accepts this as expected behavior).
        self._last_routed_member: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session ID conventions (§6.2)
    # ------------------------------------------------------------------

    @staticmethod
    def observer_session_id(room_id: str) -> str:
        return f"room_observer:{room_id}"

    @staticmethod
    def member_session_id(room_id: str, member_profile: str) -> str:
        return f"room_member:{room_id}:{member_profile}"

    # ------------------------------------------------------------------
    # last_routed_member cache
    # ------------------------------------------------------------------

    async def _get_last_routed(self, room_id: str) -> Optional[str]:
        async with self._cache_lock:
            return self._last_routed_member.get(room_id)

    async def _set_last_routed(self, room_id: str, member: str) -> None:
        async with self._cache_lock:
            self._last_routed_member[room_id] = member

    async def clear_last_routed(self, room_id: str) -> None:
        """Called when the room is structurally changed (member roster
        update, delete, unbind) so the next message triggers a full
        observer turn instead of reusing a now-invalid target.

        Not called from within process_message; hook for M1.6's slash
        command handlers to invoke alongside store.fence_room().
        """
        async with self._cache_lock:
            self._last_routed_member.pop(room_id, None)

    # ------------------------------------------------------------------
    # §8 Rule B: parse cross-member summary from the observer's reason
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_cross_member_context(reason: str) -> CrossMemberContext:
        """Parse the "上一位处理人 <X> 的回复摘要: <text>" prefix pattern
        the SOUL.md template (M1.2 §8 Rule A) instructs the observer to
        emit. Missing / malformed prefix → returns an empty context and
        the router sends the raw original message unchanged."""
        if not reason:
            return CrossMemberContext(previous_member=None, summary=None)
        match = _CROSS_MEMBER_SUMMARY_RE.search(reason)
        if not match:
            return CrossMemberContext(previous_member=None, summary=None)
        prev_member = (match.group(1) or "").strip()
        summary = (match.group(2) or "").strip()
        if not prev_member or not summary:
            return CrossMemberContext(previous_member=None, summary=None)
        return CrossMemberContext(previous_member=prev_member, summary=summary)

    @staticmethod
    def _apply_cross_member_prefix(
        original_message: str, context: CrossMemberContext
    ) -> str:
        """§8 Rule B format: 3-line prefix, blockquote-styled so a member
        can visually distinguish it from the user's original words even
        if the member's model doesn't understand the intent."""
        if not context.has_summary():
            return original_message
        return (
            f"> 上一位处理人 {context.previous_member} 的回复摘要：{context.summary}\n"
            f"> ---\n"
            f"> 用户消息：{original_message}"
        )

    # ------------------------------------------------------------------
    # M4.4: decompose_and_route orchestration + synthesis
    # ------------------------------------------------------------------

    async def _run_decompose(
        self,
        *,
        source: Any,
        room: AgentRoom,
        observer_sid: str,
        decision: "RoutingDecision",
    ) -> dict[str, Any]:
        """M4.4 · run the subtask DAG the observer emitted, then a
        synthesis turn that composes the final user-facing reply.

        Flow:
          1. build_subtasks(raw, roster) — validate/normalize/reject cycles
          2. orchestrate(...) — level-by-level, concurrent within level,
             per-subtask member dispatch via self._member_dispatcher
          3. render_subtask_results_for_synthesis(result)
          4. synthesis observer turn (reuses observer_sid per M4.1 spike)
             → final reply. If no synthesis_runner is wired, fall back to
             a plaintext concatenation of successful subtask replies.

        Returns the same diagnostic bundle shape as process_message,
        with ``decompose == True`` and an ``orchestration`` sub-dict.
        """
        from gateway.agent_room_task_orchestrator import (
            OrchestratorError,
            build_subtasks,
            orchestrate,
            render_subtask_results_for_synthesis,
        )

        room_id = room.room_id
        raw_tasks = decision.decompose_tasks or []

        # Step 1: validate the DAG against the current roster.
        try:
            subtasks = build_subtasks(
                raw_tasks,
                room.members,
                default_member=room.resolve_default_member(),
            )
        except OrchestratorError as exc:
            # Cyclic / structurally-broken DAG. Fall back to a single
            # default-member dispatch of the observer's stated reason so
            # the user still gets *an* answer rather than silence.
            logger.warning(
                "room %s: decompose DAG rejected (%s); falling back to default member",
                room_id, exc,
            )
            fallback_member = room.resolve_default_member()
            member_sid = self.member_session_id(room_id, fallback_member)
            reply = await self._member_dispatcher(
                fallback_member, member_sid, source, [], decision.reason or "",
            )
            return {
                "fenced_at": None,
                "decompose": True,
                "decompose_error": str(exc),
                "target_member": fallback_member,
                "reason": decision.reason,
                "reused_last_route": False,
                "reply": reply,
            }

        if not subtasks:
            logger.info(
                "room %s: decompose produced no valid subtasks; nothing to run",
                room_id,
            )
            return {
                "fenced_at": None,
                "decompose": True,
                "target_member": None,
                "reason": decision.reason,
                "reused_last_route": False,
                "reply": None,
                "orchestration": {"total_subtasks": 0, "results": []},
            }

        # Step 2: orchestrate. The dispatcher adapts the orchestrator's
        # 4-arg signature (member, session_id, message, projected_hist)
        # to self._member_dispatcher's 5-arg one (adds the source).
        async def _subtask_dispatcher(
            member: str, session_id: str, message_text: str, projected_hist: list[dict],
        ) -> str:
            return await self._member_dispatcher(
                member, session_id, source, projected_hist, message_text,
            )

        def _fence_gate(member_session_id: str) -> bool:
            return self._store.is_fenced(room_id, member_session_id)

        orch_result = await orchestrate(
            subtasks,
            room_id=room_id,
            dispatcher=_subtask_dispatcher,
            fence_gate=_fence_gate,
        )
        logger.info(
            "room %s: orchestration done — %d/%d ok, %d failed, %d skipped%s",
            room_id, orch_result.completed, orch_result.total_subtasks,
            orch_result.failed, orch_result.skipped,
            " (fenced mid-flight)" if orch_result.fenced_mid_flight else "",
        )

        # §6.3 Fence check: if the room was structurally changed during
        # orchestration, drop the whole thing before synthesizing.
        if self._store.is_fenced(room_id, observer_sid):
            logger.info(
                "room %s: observer fenced post-orchestration; discarding synthesis",
                room_id,
            )
            return {
                "fenced_at": "observer_postorchestration",
                "decompose": True,
                "target_member": None,
                "reason": decision.reason,
                "reused_last_route": False,
                "reply": None,
                "orchestration": orch_result.to_dict(),
            }

        # Step 3 + 4: synthesis turn (reuse observer session per M4.1).
        rendered = render_subtask_results_for_synthesis(orch_result)
        final_reply: str
        if self._synthesis_runner is not None:
            try:
                final_reply = await self._synthesis_runner(
                    room.observer_profile, observer_sid, source, rendered,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "room %s: synthesis turn failed (%s); using raw concatenation",
                    room_id, exc,
                )
                final_reply = self._concat_subtask_replies(orch_result)
        else:
            final_reply = self._concat_subtask_replies(orch_result)

        return {
            "fenced_at": None,
            "decompose": True,
            "target_member": None,
            "reason": decision.reason,
            "reused_last_route": False,
            "reply": final_reply,
            "orchestration": orch_result.to_dict(),
        }

    @staticmethod
    def _concat_subtask_replies(orch_result: Any) -> str:
        """Fallback synthesis: plain concatenation of successful subtask
        replies, each labeled by assignee. Used when no synthesis_runner
        is wired or the synthesis turn errored."""
        parts: list[str] = []
        for r in orch_result.results:
            if r.status == "success" and r.reply:
                parts.append(f"【{r.assignee}】{r.title}\n{r.reply.strip()}")
            elif r.status == "failed":
                parts.append(f"【{r.assignee}】{r.title}\n(处理失败：{r.error})")
        return "\n\n".join(parts) if parts else "(所有子任务均未产出结果)"

    # ------------------------------------------------------------------
    # §6.1 five-step flow
    # ------------------------------------------------------------------

    async def process_message(
        self,
        source: Any,
        message: str,
        history: list[dict],
        room: AgentRoom,
    ) -> dict[str, Any]:
        """Run the full §6.1 five-step flow.

        Parameters
        ----------
        source : Any
            Opaque source descriptor (typically ``SessionSource``); the
            router does not introspect it, only forwards to the injected
            callables. Testing uses ``object()`` as a stand-in.
        message : str
            The user's incoming message text.
        history : list[dict]
            Recent conversation history for the room (used by both the
            classifier and the observer). Format: OpenAI-style
            ``[{"role": ..., "content": ...}, ...]``. Empty list on
            first message is fine.
        room : AgentRoom
            The room record from AgentRoomStore. The router does NOT
            re-fetch this — the caller has already resolved it in
            gateway/run.py's ``_resolve_room_for_source``.

        Returns
        -------
        dict
            Diagnostic bundle documenting what happened (target member,
            reason, reused-cache flag, whether §8 summary was applied,
            member's reply text, Fence dropped-at-which-step). Useful
            for A/B metric collection and for M1's live tests.
        """
        room_id = room.room_id
        observer_sid = self.observer_session_id(room_id)

        # ── Step 1: immediate acknowledgement ─────────────────────────
        ack_handle = await self._ack_sender(
            source, "已收到，正在为你选择处理人..."
        )

        # ── Step 2: N4 lightweight classifier ─────────────────────────
        last_routed = await self._get_last_routed(room_id)
        # Only the tail 5 messages fed to the classifier (§5.4 spec).
        classifier_input = history[-5:] if history else []
        classification = await self._classifier(classifier_input, last_routed)

        # ── Step 3: route ─────────────────────────────────────────────
        if last_routed and classification.should_reuse_last_routed():
            # Fast path: reuse the last-routed member, skip the
            # observer turn entirely. This is the §5.4 cost saver
            # that the A/B "N4 沿用率 ≥40%" metric measures.
            decision = RoutingDecision(
                target_member=last_routed,
                reason=f"N4 reuse (confidence={classification.confidence:.2f})",
                is_new_topic=False,
                reused_last_route=True,
            )
        else:
            # Slow path: full observer turn.
            observer_decision = await self._observer_runner(
                room.observer_profile,
                observer_sid,
                source,
                history,
                message,
            )
            # §6.3 Fence check A: if the room was structurally changed
            # while the observer turn was in flight, its decision is
            # discarded here — no member is dispatched, no group
            # message is sent.
            if self._store.is_fenced(room_id, observer_sid):
                logger.info(
                    "room %s: observer session %s fenced mid-turn; discarding decision",
                    room_id, observer_sid,
                )
                return {
                    "fenced_at": "observer",
                    "target_member": None,
                    "reason": None,
                    "reused_last_route": False,
                    "reply": None,
                }

            # ── M4.4: decompose_and_route branch ──────────────────────
            # If the observer decided this is a multi-step task, it
            # emitted a subtask DAG instead of a single/multi member
            # route. Run the orchestrator, then a synthesis turn.
            if getattr(observer_decision, "is_decompose", False):
                return await self._run_decompose(
                    source=source,
                    room=room,
                    observer_sid=observer_sid,
                    decision=observer_decision,
                )

            # Validate the observer's target against the current roster.
            # M1-B4: empty / whitespace / unknown members all fall back
            # to the room's default_member (or members[0] via
            # AgentRoom.resolve_default_member).
            #
            # M3.5: target_member can be a list[str] for concurrent
            # multi-member dispatch. Validate each element independently;
            # any invalid names are dropped. If all are invalid, fall
            # back to a single-member decision on default_member.
            raw_target = observer_decision.target_member
            if isinstance(raw_target, list):
                cleaned = [str(m).strip() for m in raw_target if str(m).strip()]
                valid = [m for m in cleaned if m in room.members]
                if not valid:
                    fallback = room.resolve_default_member()
                    logger.warning(
                        "room %s: observer proposed %r (none in roster %s); "
                        "falling back to single %r",
                        room_id, raw_target, room.members, fallback,
                    )
                    target = fallback
                elif len(valid) == 1:
                    # Single-valid degenerates to single-member dispatch
                    target = valid[0]
                else:
                    # De-dupe preserving order for concurrent list
                    seen: set = set()
                    deduped: list[str] = []
                    for m in valid:
                        if m not in seen:
                            seen.add(m)
                            deduped.append(m)
                    target = deduped  # keep as list → is_multi=True
                    if len(cleaned) != len(valid):
                        logger.info(
                            "room %s: dropped invalid members from concurrent "
                            "route (kept %s, dropped %s)",
                            room_id, valid, set(cleaned) - set(valid),
                        )
            else:
                target = (raw_target or "").strip()
                if target not in room.members:
                    fallback = room.resolve_default_member()
                    logger.warning(
                        "room %s: observer chose %r (not in roster %s); "
                        "falling back to %r",
                        room_id, target, room.members, fallback,
                    )
                    target = fallback

            decision = RoutingDecision(
                target_member=target,
                reason=observer_decision.reason,
                is_new_topic=observer_decision.is_new_topic,
                reused_last_route=False,
            )

        # Update the cache regardless of which path we took — successful
        # reuse also refreshes the "last route" so a subsequent classifier
        # call has a valid anchor. For multi-member decisions we cache
        # the first member (arbitrary but deterministic) so N4 continuation
        # detection still works.
        _cache_target = (
            decision.target_member[0] if decision.is_multi
            else decision.target_member
        )
        await self._set_last_routed(room_id, _cache_target)

        # ── Step 4: edit the ack message (skipped for cleaner UX) ─────
        # The ack message ('已收到，正在为你选择处理人...') was originally
        # edited to '已转交给 X 处理...' as a UX hint that routing had
        # progressed. Live testing showed this becomes noise: (a) the
        # member's actual reply arrives seconds later and IS the useful
        # signal, (b) for concurrent multi-member routing the member
        # labels are already carried by each reply bubble, and (c) the
        # ack card and the reply are separate IM messages, so the edit
        # doesn't visibly replace anything for the user.
        #
        # We keep the initial ack (sent in Step 1) so the user knows
        # the message was received during LLM latency, but skip the
        # 'transferred to X' edit.
        _ = ack_handle  # kept for future re-enable if needed

        # ── Step 4.5: §8 Rule B cross-member summary injection ────────
        # (M3: the projection layer replaces this in-band summary
        # mechanism, but the parser still runs to preserve M1 behavior
        # when projection isn't wired.)
        cross_context = self._extract_cross_member_context(decision.reason)
        member_input = self._apply_cross_member_prefix(message, cross_context)

        # ── Step 5: dispatch member turn(s) ───────────────────────────
        if decision.is_multi:
            # M3.5 concurrent multi-member dispatch: fan out to all
            # members in parallel via asyncio.gather. Fence check is
            # per-member; a room-wide fence discards all replies.
            member_ids = decision.target_members
            member_sids = {
                m: self.member_session_id(room_id, m) for m in member_ids
            }

            # Pre-dispatch fence gate (§6.3 checkpoint B for each member).
            # If ANY member session is fenced pre-dispatch, drop the
            # entire concurrent turn — a mid-flight structural change
            # invalidates all of it.
            for m, sid in member_sids.items():
                if self._store.is_fenced(room_id, sid):
                    logger.info(
                        "room %s: member %s fenced pre-dispatch; dropping concurrent turn",
                        room_id, m,
                    )
                    return {
                        "fenced_at": "member_predispatch",
                        "target_member": decision.target_member,
                        "reason": decision.reason,
                        "reused_last_route": decision.reused_last_route,
                        "reply": None,
                    }

            # Fan out concurrently. Any single member exception is
            # captured as a string reply for that member — one failure
            # does not sink the others (M3-B4).
            import asyncio as _asyncio

            async def _run_one(m: str) -> tuple[str, str]:
                sid = member_sids[m]
                try:
                    r = await self._member_dispatcher(
                        m, sid, source, history, member_input,
                    )
                    return (m, r or "")
                except Exception as exc:  # noqa: BLE001 — per-member isolation
                    logger.warning(
                        "room %s: member %s turn failed: %s", room_id, m, exc,
                    )
                    return (m, f"[error: {type(exc).__name__}: {exc}]")

            results = await _asyncio.gather(*[_run_one(m) for m in member_ids])
            replies: dict[str, str] = dict(results)

            # Post-dispatch fence (§6.3 checkpoint C): drop replies
            # if the room was fenced during any of the concurrent turns.
            for m, sid in member_sids.items():
                if self._store.is_fenced(room_id, sid):
                    logger.info(
                        "room %s: member %s fenced during concurrent dispatch; dropping",
                        room_id, m,
                    )
                    return {
                        "fenced_at": "member_postdispatch",
                        "target_member": decision.target_member,
                        "reason": decision.reason,
                        "reused_last_route": decision.reused_last_route,
                        "reply": None,
                    }

            return {
                "fenced_at": None,
                "target_member": decision.target_member,
                "reason": decision.reason,
                "reused_last_route": decision.reused_last_route,
                "cross_member_summary_applied": cross_context.has_summary(),
                "cross_member_previous": cross_context.previous_member,
                "reply": None,          # per-member replies live in .replies
                "replies": replies,     # M3 addition: per-member reply map
                "concurrent": True,
            }

        # ── Single-member dispatch (M1 legacy path, unchanged) ────────
        member_sid = self.member_session_id(room_id, decision.target_member)
        # §6.3 Fence check B: check the member's session BEFORE running
        # its turn — a structural change between observer completion
        # and here also stops us.
        if self._store.is_fenced(room_id, member_sid):
            logger.info(
                "room %s: member session %s fenced pre-dispatch; skipping",
                room_id, member_sid,
            )
            return {
                "fenced_at": "member_predispatch",
                "target_member": decision.target_member,
                "reason": decision.reason,
                "reused_last_route": decision.reused_last_route,
                "reply": None,
            }

        reply_text = await self._member_dispatcher(
            decision.target_member,
            member_sid,
            source,
            history,
            member_input,
        )

        # §6.3 Fence check C: even after the member turn finishes, if
        # the room was structurally changed in the meantime, drop the
        # reply. This is the "已发出的 step4 edit_message 作废" case
        # from M3-B8 that also applies to M1.
        if self._store.is_fenced(room_id, member_sid):
            logger.info(
                "room %s: member session %s fenced during dispatch; discarding reply",
                room_id, member_sid,
            )
            return {
                "fenced_at": "member_postdispatch",
                "target_member": decision.target_member,
                "reason": decision.reason,
                "reused_last_route": decision.reused_last_route,
                "reply": None,
            }

        return {
            "fenced_at": None,
            "target_member": decision.target_member,
            "reason": decision.reason,
            "reused_last_route": decision.reused_last_route,
            "cross_member_summary_applied": cross_context.has_summary(),
            "cross_member_previous": cross_context.previous_member,
            "reply": reply_text,
        }
