"""Room→chat mirror watcher (C2 outbound, read-only mirror).

A background loop that pushes hosted-room member messages into subscribed IM
chat groups. It is a deliberately-smaller sibling of the kanban notifier
(``gateway/kanban_watchers.py``): claim a bounded delta of new
``message.member`` events per subscription, deliver each to
``(platform, chat_id, thread_id)`` via the same adapter/authorization path,
then advance the cursor — rewinding on send failure so nothing is lost.

Read-only: it never writes the room log and never touches ``hosted_rooms.py``.
The only mutable state is the ``room_notify_subs`` cursor table owned by
``gateway.room_mirror_db``. This is the enforcement layer for the decider's
"single external voice": a subscription's ``member_filter`` decides which
members' turns reach the group.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from agent.i18n import t

if TYPE_CHECKING:
    from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.run")

# Consecutive failures are diagnostic only. They must never authorize cursor
# advancement: without a durable delivery/dead-letter record, advancing would
# permanently discard an event after a transient outage or process restart.
_MAX_SEND_FAILURES = 12

# Sentinel distinguishing "inbound message was NOT routed to a hosted room" (fall through to normal dispatch)
# from a routing result of ``None`` (routed, no textual reply to the chat). A plain ``None`` return would
# otherwise be ambiguous with the many handlers that legitimately return ``None``.
INBOUND_ROOM_NOT_ROUTED = object()


class GatewayRoomMirrorMixin:
    """Room<->chat bridge for GatewayRunner: outbound mirror loop + opt-in inbound routing (C2)."""

    async def _maybe_route_inbound_to_room(self, event: MessageEvent, source: Any):
        """Route an inbound chat message into a bound hosted room, if any.

        Returns :data:`INBOUND_ROOM_NOT_ROUTED` when the source is not bound to
        an inbound-enabled room (the caller then falls through to normal agent
        dispatch). When the source IS bound, appends the text to the room as a
        ``message.user`` event (server-owned actor id = the IM user id) and
        returns a short confirmation string (or ``None`` for a silent ack).

        Design invariants (see docs/design/hosted-room-chat-bridge.md slice 2):
          * Opt-in: only rows with ``inbound=1`` match, so a read-only mirror
            never starts ingesting.
          * Idempotent: the event id is derived from the IM ``message_id`` via
            the room's ``user_event_id`` namespace, so a redelivered message
            never double-appends.
          * Echo-safe: this only ever writes ``message.user``; the outbound
            mirror only forwards ``message.member`` — the loop is structurally
            impossible.
          * Safe fallback: lookup errors before a binding is proven fall through
            to normal handling. Once a binding is proven, dispatch errors fail
            closed with a retryable user response so another agent cannot consume
            a message intended for the room.
        """

        try:
            from gateway import room_mirror_db as _mir
            from gateway.hosted_rooms import default_db_path, user_event_id
        except Exception:
            return INBOUND_ROOM_NOT_ROUTED

        platform = source.platform.value if getattr(source, "platform", None) else ""
        chat_id = str(getattr(source, "chat_id", "") or "")
        thread_id = str(getattr(source, "thread_id", "") or "")
        if not platform or not chat_id:
            return INBOUND_ROOM_NOT_ROUTED

        db_path = default_db_path()
        try:
            binding = await asyncio.to_thread(
                _mir.inbound_binding_for_source,
                db_path,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        except Exception:
            logger.debug("room inbound: binding lookup failed", exc_info=True)
            return INBOUND_ROOM_NOT_ROUTED
        if binding is None:
            return INBOUND_ROOM_NOT_ROUTED

        room_id = str(binding.get("room_id") or "")
        text = (event.text or "").strip()
        if not room_id or not text:
            return INBOUND_ROOM_NOT_ROUTED

        # Per-user actor identity: the IM user id, verbatim, into the log's
        # existing {kind:user, id} actor schema. Falls back to "desktop" (the
        # historical default) when the platform gives us no stable user id.
        actor_id = str(getattr(source, "user_id", "") or "").strip() or "desktop"

        # Idempotency key: bind the room's user-event namespace to the IM
        # message id so PTB/webhook redelivery of the same message is a no-op.
        # Fall back to a per-(source,text) key when the platform gives no id.
        import hashlib as _hashlib

        source_scope = f"{platform}:{chat_id}:{thread_id}"
        raw_msg_id = str(getattr(event, "message_id", "") or "").strip()
        if raw_msg_id:
            client_key = f"{source_scope}:{raw_msg_id}"
        else:
            digest = _hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            client_key = f"{source_scope}:t:{digest}"
        # Room discussion payloads require a stable identifier-safe thread id.
        # Platform thread/chat ids are not guaranteed to satisfy that schema, so
        # derive one from the bound source. Messages redelivered into the same
        # chat/thread converge on this exact discussion thread.
        room_thread_id = (
            "inbound:"
            + _hashlib.sha256(source_scope.encode("utf-8")).hexdigest()[:32]
        )

        def _send() -> dict:
            from tui_gateway.methods_groups import get_hosted_room_service
            service = get_hosted_room_service()
            if service is None:
                raise RuntimeError("hosted room worker unavailable")
            return service.send(
                room_id=room_id,
                event_id=user_event_id(client_key),
                payload={"text": text, "thread_id": room_thread_id},
                actor_id=actor_id,
            )

        try:
            await asyncio.to_thread(_send)
        except Exception as exc:
            logger.warning(
                "room inbound: failed to route message to room %s: %s",
                room_id, exc,
            )
            # Fail-open would silently drop the message into the agent path,
            # which is wrong for a bound room; instead tell the user it didn't
            # land so they can retry, without leaking internals.
            return t("gateway.room.inbound_failed", room_id=room_id)

        logger.info(
            "room inbound: routed message from %s:%s → room %s (actor=%s)",
            platform, chat_id, room_id, actor_id,
        )
        # Silent ack: the member reply comes back through the outbound mirror,
        # so a confirmation echo here would be noise. Return None (routed).
        return None

    async def _room_mirror_watcher(self, interval: float = 5.0) -> None:
        """Poll ``room_notify_subs`` and mirror member messages to chat groups.

        Runs in the gateway event loop; all SQLite work is pushed to a thread
        via ``asyncio.to_thread`` so the loop never blocks on the WAL lock.
        Failures in one tick don't stop later ticks, and a failed adapter send
        rewinds the claim so the cursor never skips an undelivered message.
        """

        from gateway.config import Platform as _Platform
        try:
            from gateway import hosted_rooms as _rooms
            from gateway import room_mirror_db as _mir
        except Exception:
            logger.warning("room mirror: modules not importable; mirror disabled")
            return

        db_path = _rooms.default_db_path()
        fail_counts: dict[tuple, int] = getattr(self, "_room_mirror_fail_counts", {})
        self._room_mirror_fail_counts = fail_counts

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect() -> list[dict]:
                    claimed: list[dict] = []
                    try:
                        subs = _mir.list_subs(db_path)
                    except Exception as exc:
                        logger.debug("room mirror: cannot list subs: %s", exc)
                        return claimed
                    for sub in subs:
                        try:
                            old_cursor, cursor, events = _mir.claim_unseen_room_events(
                                db_path,
                                room_id=sub["room_id"],
                                platform=sub["platform"],
                                chat_id=sub["chat_id"],
                                thread_id=sub.get("thread_id") or "",
                            )
                            if not events:
                                continue
                            # Resolve member_id → handle for display. The
                            # message.member payload only carries member_id;
                            # the handle lives on the roster. Read-only.
                            handles: dict[str, str] = {}
                            try:
                                state = _rooms.room_state(
                                    db_path, room_id=sub["room_id"]
                                )
                                for m in state.get("members") or []:
                                    if isinstance(m, dict):
                                        handles[str(m.get("member_id") or "")] = str(
                                            m.get("handle") or ""
                                        )
                            except Exception:
                                handles = {}
                            claimed.append({
                                "sub": sub,
                                "old_cursor": old_cursor,
                                "cursor": cursor,
                                "events": events,
                                "handles": handles,
                            })
                        except Exception as sub_exc:
                            # Isolate per-subscription failures so one bad row
                            # cannot block delivery for every other mirror.
                            logger.warning(
                                "room mirror: claim failed for room %s: %s",
                                sub.get("room_id"), sub_exc,
                            )
                    return claimed

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform: advance so we don't replay forever.
                        await asyncio.to_thread(
                            _mir.advance_cursor,
                            db_path,
                            room_id=sub["room_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or "",
                            new_cursor=d["cursor"],
                        )
                        continue

                    sub_profile = sub.get("notifier_profile") or None
                    adapter = self._authorization_adapter(plat, sub_profile)
                    if adapter is None:
                        # Adapter gone before delivery: rewind so the next tick
                        # (or another gateway) retries these exact messages.
                        logger.debug(
                            "room mirror: adapter %s disconnected for room %s; rewinding",
                            platform_str, sub["room_id"],
                        )
                        await asyncio.to_thread(
                            _mir.rewind_cursor,
                            db_path,
                            room_id=sub["room_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or "",
                            claimed_cursor=d["cursor"],
                            old_cursor=d.get("old_cursor", 0),
                        )
                        continue

                    sub_key = (
                        sub["room_id"], sub["platform"],
                        sub["chat_id"], sub.get("thread_id") or "",
                    )
                    # The process-local counter is observability only. Even after
                    # many failures, retry the claimed events: skipping them would
                    # violate the durable mirror contract because no dead-letter
                    # receipt exists from which an operator could recover them.

                    handles = d.get("handles") or {}
                    metadata: dict = {}
                    raw_meta = sub.get("delivery_metadata")
                    if isinstance(raw_meta, str) and raw_meta:
                        try:
                            import json as _json
                            parsed = _json.loads(raw_meta)
                            if isinstance(parsed, dict):
                                metadata = dict(parsed)
                        except Exception:
                            metadata = {}
                    if sub.get("thread_id") and not metadata.get("thread_id"):
                        metadata["thread_id"] = sub["thread_id"]

                    delivered_ok = True
                    for ev in d["events"]:
                        payload = ev.get("payload") or {}
                        text = str(payload.get("text") or "").strip()
                        if not text:
                            continue
                        member_id = str(payload.get("member_id") or "")
                        handle = handles.get(member_id, member_id)
                        content = f"@{handle}: {text}" if handle else text
                        try:
                            send_res = await adapter.send(
                                sub["chat_id"], content, metadata=metadata,
                            )
                            if getattr(send_res, "success", True) is False:
                                raise RuntimeError(
                                    "adapter send() reported failure: "
                                    f"{getattr(send_res, 'error', None) or 'unknown'}"
                                )
                            fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = fail_counts.get(sub_key, 0) + 1
                            fail_counts[sub_key] = fails
                            logger.warning(
                                "room mirror: send failed for room %s on %s "
                                "(consecutive attempt %d%s): %s",
                                sub["room_id"],
                                platform_str,
                                fails,
                                " — operator attention required"
                                if fails >= _MAX_SEND_FAILURES
                                else "",
                                exc,
                            )
                            delivered_ok = False
                            break

                    if not delivered_ok:
                        # Rewind the whole claim; the failed and any following
                        # messages in this batch are re-delivered next tick.
                        await asyncio.to_thread(
                            _mir.rewind_cursor,
                            db_path,
                            room_id=sub["room_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or "",
                            claimed_cursor=d["cursor"],
                            old_cursor=d.get("old_cursor", 0),
                        )
            except Exception as exc:
                logger.warning("room mirror: tick failed: %s", exc, exc_info=True)

            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(0.5, interval - slept))
                slept += 0.5
