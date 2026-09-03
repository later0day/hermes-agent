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

logger = logging.getLogger("gateway.run")

# Consecutive send-failure budget before a subscription is skipped for the rest
# of the process lifetime. Matches the kanban notifier's ~60s-at-5s-cadence
# tolerance so a transient adapter outage never silently drops a live mirror.
_MAX_SEND_FAILURES = 12


class GatewayRoomMirrorMixin:
    """Room→chat outbound mirror loop for GatewayRunner."""

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
                    # Consecutive-failure budget: after _MAX_SEND_FAILURES ticks
                    # against a dead chat, advance past this claim rather than
                    # spinning forever. A genuinely transient outage recovers
                    # well within the budget; a deleted chat / kicked bot stops
                    # replaying. The counter resets on any successful send.
                    if fail_counts.get(sub_key, 0) >= _MAX_SEND_FAILURES:
                        logger.warning(
                            "room mirror: room %s → %s exceeded %d send failures; "
                            "advancing cursor to stop replay",
                            sub["room_id"], platform_str, _MAX_SEND_FAILURES,
                        )
                        await asyncio.to_thread(
                            _mir.advance_cursor,
                            db_path,
                            room_id=sub["room_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or "",
                            new_cursor=d["cursor"],
                        )
                        fail_counts.pop(sub_key, None)
                        continue

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
                                "(attempt %d/%d): %s",
                                sub["room_id"], platform_str, fails,
                                _MAX_SEND_FAILURES, exc,
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
