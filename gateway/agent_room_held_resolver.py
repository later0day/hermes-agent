"""Agent Room · held-draft resolver (Raft AX 改造 1 · 后续纵切).

Design ref: docs/design/agent-room/ax-alignment.md §3 改造 1.

``AgentRoomHeldStore`` (agent_room_held_store.py) turns the router's
historical silent-drop into a durable, recoverable ``HeldReply``. This
module is the other half: it takes a held reply back OUT of the "held"
state along one of Raft's four recovery paths, and — crucially — actually
delivers it, closing the black hole for real.

The four Raft paths, and how we map them:

  * **Send-as-is** — the reply is fine, only its *transport* was lost
    (defect #2: the DingTalk session_webhook expired). We re-deliver the
    stored payload verbatim through a transport-independent path
    (``adapter.send`` after the stashed proactive/AI-Card fallback is
    re-applied). Safe iff the room did NOT move since the reply was
    reasoned (``held.room_version == current``).
  * **Revise** — the room *moved* under the reply (its ``room_version`` is
    behind the current one), so sending it as-is could be stale/wrong. We
    re-run the member with fresh projection and deliver the new reply. If
    no rerun callable is wired, we degrade to Send-as-is (delivering a
    slightly-stale reply still beats the black hole — that is the whole
    anti-drop principle).
  * **Stay-silent** — a conscious discard (e.g. an operator judges the
    topic already covered). No delivery; the row is resolved + logged so
    it's an accountable decision, not a silent loss.
  * **Send-anyway** — explicit bypass: deliver as-is regardless of version
    drift (operator override / repeated-hold escalation).

Auto policy (when ``resolve_one`` is called with no explicit path):
  room unchanged → Send-as-is ; room moved → Revise (→ Send-as-is if no
  rerun). Both terminal states DELIVER, which is the point.

Like the router, every dependency is injected so this is unit-testable
with no gateway / no network: ``deliver`` and ``rerun_member`` are plain
awaitables a test can stub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from gateway.agent_room_held_store import (
    AgentRoomHeldStore,
    HELD_REASON_ROOM_MOVED,
    HeldReply,
    RESOLUTION_REVISE,
    RESOLUTION_SEND_ANYWAY,
    RESOLUTION_SEND_AS_IS,
    RESOLUTION_STAY_SILENT,
)

logger = logging.getLogger(__name__)


# Deliver a payload to an IM chat via a transport-INDEPENDENT path. Returns
# True on success. Real impl: DingTalkAdapter.send (which, once the stashed
# defect-#2 fallback is re-applied, no longer needs a live session_webhook).
Deliver = Callable[[str, str], Awaitable[bool]]

# Re-run a member's turn against the CURRENT room and return its fresh
# reply text. Used only on the Revise path. Optional — absent → Revise
# degrades (room_moved → stay-silent, transport-lost → send-as-is).
#
# Receives the full HeldReply so the implementation has everything it
# needs to reconstruct the run: room_id, member, session_id, chat_id, and
# any transport metadata stashed in ``extra`` (platform, chat_type, user).
# Must NOT deliver — the resolver delivers the returned text to
# ``held.chat_id`` via ``deliver``. Returning "" is a legitimate
# "member now has nothing to add" → the resolver treats it as stay-silent.
RerunMember = Callable[["HeldReply"], Awaitable[str]]


@dataclass(frozen=True)
class ResolveOutcome:
    """What happened to one held reply."""
    held_id: int
    path: str            # which of the four resolutions was taken
    delivered: bool      # was a payload actually delivered to the chat
    resolved: bool       # was the store row transitioned to 'resolved'
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "held_id": self.held_id,
            "path": self.path,
            "delivered": self.delivered,
            "resolved": self.resolved,
            "error": self.error,
        }


class AgentRoomHeldResolver:
    """Drives held replies to a terminal Raft recovery path (+ delivery)."""

    def __init__(
        self,
        *,
        held_store: AgentRoomHeldStore,
        deliver: Deliver,
        room_version_provider: Optional[Callable[[str], int]] = None,
        rerun_member: Optional[RerunMember] = None,
    ) -> None:
        self._held_store = held_store
        self._deliver = deliver
        self._room_version_provider = room_version_provider
        self._rerun_member = rerun_member

    # ------------------------------------------------------------------
    # Version helpers
    # ------------------------------------------------------------------

    def _current_room_version(self, room_id: str) -> int:
        if self._room_version_provider is None:
            return 0
        try:
            return int(self._room_version_provider(room_id) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("held resolver: room_version_provider failed: %s", exc)
            return 0

    def _auto_path(self, held: HeldReply) -> str:
        """Pick a path when the caller didn't force one.

        room unchanged (or version unknown) → Send-as-is; room moved →
        Revise. A held.room_version of 0 means "unknown snapshot" (no
        provider was wired when it was held) — we treat that as
        Send-as-is because we can't prove the room moved and delivering
        beats dropping."""
        if not held.room_version:
            return RESOLUTION_SEND_AS_IS
        current = self._current_room_version(held.room_id)
        if current > held.room_version:
            return RESOLUTION_REVISE
        return RESOLUTION_SEND_AS_IS

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve_one(
        self,
        held: HeldReply,
        *,
        path: Optional[str] = None,
    ) -> ResolveOutcome:
        """Resolve a single held reply, delivering as the path requires.

        ``path`` forces one of the four resolutions; ``None`` auto-selects.
        Delivery failures leave the row HELD (returns delivered=False,
        resolved=False) so a later drain can retry — a transport that is
        still down must never consume the held artifact."""
        if held.id is None:
            return ResolveOutcome(
                held_id=-1, path="(none)", delivered=False, resolved=False,
                error="held reply has no id",
            )
        chosen = path or self._auto_path(held)

        # Revise with no rerun callable → degrade. WHICH way to degrade
        # depends on WHY the reply was held:
        #   * transport-lost holds (fenced / no_webhook / send_failed): the
        #     reply itself is still valid, only its transport died, so a
        #     slightly-stale Send-as-is still beats the black hole.
        #   * room_moved holds: a NEW user turn provably superseded this
        #     reply — sending it as-is would re-land the very non-sequitur
        #     the hold exists to prevent (the counting-game case). Without
        #     rerun we cannot produce a fresh reply, so the honest, logged,
        #     accountable resolution is Stay-silent, NOT Send-as-is.
        if chosen == RESOLUTION_REVISE and self._rerun_member is None:
            if held.held_reason == HELD_REASON_ROOM_MOVED:
                logger.info(
                    "held %d: room moved + no rerun_member wired; the reply is "
                    "provably stale → stay-silent (conscious discard, not "
                    "a stale send)", held.id,
                )
                chosen = RESOLUTION_STAY_SILENT
            else:
                logger.info(
                    "held %d: Revise requested but no rerun_member wired; "
                    "degrading to Send-as-is", held.id,
                )
                chosen = RESOLUTION_SEND_AS_IS

        if chosen == RESOLUTION_STAY_SILENT:
            resolved = self._held_store.resolve(held.id, RESOLUTION_STAY_SILENT)
            logger.info("held %d: stay-silent (conscious discard)", held.id)
            return ResolveOutcome(
                held_id=held.id, path=RESOLUTION_STAY_SILENT,
                delivered=False, resolved=resolved,
            )

        if chosen == RESOLUTION_REVISE:
            return await self._do_revise(held)

        # Send-as-is / Send-anyway both deliver the stored payload verbatim.
        return await self._do_send(held, chosen)

    async def _do_send(self, held: HeldReply, path: str) -> ResolveOutcome:
        chat_id = held.chat_id or ""
        if not chat_id:
            logger.warning(
                "held %d: cannot %s — no chat_id on record", held.id, path,
            )
            return ResolveOutcome(
                held_id=held.id, path=path, delivered=False, resolved=False,
                error="no chat_id",
            )
        try:
            ok = bool(await self._deliver(chat_id, held.payload))
        except Exception as exc:  # noqa: BLE001 — transport error != crash
            logger.warning("held %d: delivery raised: %s", held.id, exc)
            return ResolveOutcome(
                held_id=held.id, path=path, delivered=False, resolved=False,
                error=str(exc),
            )
        if not ok:
            logger.info(
                "held %d: transport still down; leaving held for retry", held.id,
            )
            return ResolveOutcome(
                held_id=held.id, path=path, delivered=False, resolved=False,
                error="delivery failed",
            )
        resolved = self._held_store.resolve(held.id, path)
        logger.info("held %d: delivered via %s", held.id, path)
        return ResolveOutcome(
            held_id=held.id, path=path, delivered=True, resolved=resolved,
        )

    async def _do_revise(self, held: HeldReply) -> ResolveOutcome:
        try:
            fresh = await self._rerun_member(held)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "held %d: revise rerun raised (%s); falling back to Send-as-is",
                held.id, exc,
            )
            return await self._do_send(held, RESOLUTION_SEND_AS_IS)

        fresh = (fresh or "").strip()
        if not fresh:
            # The re-run chose to say nothing → the room moved on and the
            # member now has nothing to add. That is a legitimate stay-silent.
            resolved = self._held_store.resolve(held.id, RESOLUTION_STAY_SILENT)
            logger.info(
                "held %d: revise produced empty reply → stay-silent", held.id,
            )
            return ResolveOutcome(
                held_id=held.id, path=RESOLUTION_STAY_SILENT,
                delivered=False, resolved=resolved,
            )

        chat_id = held.chat_id or ""
        if not chat_id:
            return ResolveOutcome(
                held_id=held.id, path=RESOLUTION_REVISE,
                delivered=False, resolved=False, error="no chat_id",
            )
        try:
            ok = bool(await self._deliver(chat_id, fresh))
        except Exception as exc:  # noqa: BLE001
            logger.warning("held %d: revise delivery raised: %s", held.id, exc)
            return ResolveOutcome(
                held_id=held.id, path=RESOLUTION_REVISE,
                delivered=False, resolved=False, error=str(exc),
            )
        if not ok:
            return ResolveOutcome(
                held_id=held.id, path=RESOLUTION_REVISE,
                delivered=False, resolved=False, error="delivery failed",
            )
        resolved = self._held_store.resolve(held.id, RESOLUTION_REVISE)
        logger.info("held %d: revised + delivered", held.id)
        return ResolveOutcome(
            held_id=held.id, path=RESOLUTION_REVISE,
            delivered=True, resolved=resolved,
        )

    async def resume_all(
        self,
        room_id: Optional[str] = None,
        *,
        path: Optional[str] = None,
    ) -> list[ResolveOutcome]:
        """Drain the durable held backlog (oldest first).

        This is the "gateway restart recovers held replies" promise: on
        startup we walk every still-held row and try to deliver it. Rows
        whose transport is still down stay held (not consumed) for the next
        drain. One row's failure never stops the others."""
        outcomes: list[ResolveOutcome] = []
        for held in self._held_store.list_held(room_id):
            try:
                outcomes.append(await self.resolve_one(held, path=path))
            except Exception as exc:  # noqa: BLE001 — isolate one row
                logger.warning(
                    "held %s: resolve_one crashed: %s", held.id, exc,
                )
                outcomes.append(ResolveOutcome(
                    held_id=held.id or -1, path="(error)",
                    delivered=False, resolved=False, error=str(exc),
                ))
        if outcomes:
            delivered = sum(1 for o in outcomes if o.delivered)
            logger.info(
                "held resolver: drained %d row(s), %d delivered, %d still held",
                len(outcomes), delivered,
                sum(1 for o in outcomes if not o.resolved),
            )
        return outcomes
