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
    """The result of Step 3 — who the message goes to and why."""
    target_member: str
    reason: str
    is_new_topic: bool
    # True if we reused last_routed_member instead of running a full
    # observer turn (§5.4 N4 fast path). Useful for A/B metric
    # "N4 沿用率 ≥40%".
    reused_last_route: bool


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
    ) -> None:
        self._store = store
        self._ack_sender = ack_sender
        self._ack_editor = ack_editor
        self._classifier = classifier
        self._observer_runner = observer_runner
        self._member_dispatcher = member_dispatcher

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

            # Validate the observer's target against the current roster.
            # M1-B4: empty / whitespace / unknown members all fall back
            # to the room's default_member (or members[0] via
            # AgentRoom.resolve_default_member).
            target = (observer_decision.target_member or "").strip()
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
        # call has a valid anchor.
        await self._set_last_routed(room_id, decision.target_member)

        # ── Step 4: edit the ack message ──────────────────────────────
        try:
            await self._ack_editor(
                ack_handle,
                f"已转交给 {decision.target_member} 处理...",
            )
        except Exception as exc:  # noqa: BLE001
            # Ack edit failures shouldn't derail the routing. Log and
            # continue — the member's actual reply will still land.
            logger.warning(
                "room %s: failed to edit ack message: %s",
                room_id, exc,
            )

        # ── Step 4.5: §8 Rule B cross-member summary injection ────────
        cross_context = self._extract_cross_member_context(decision.reason)
        member_input = self._apply_cross_member_prefix(message, cross_context)

        # ── Step 5: dispatch member turn ──────────────────────────────
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
