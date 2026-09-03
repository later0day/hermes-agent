# Hosted Room ↔ Chat-Group Bridge (C2)

Status: design proposal (not yet implemented)
Author: drafted for later0day
Related: gateway/hosted_room_service.py, gateway/kanban_watchers.py (structural
         precedent), gateway/delivery.py, docs/design/hosted-room-decider.md
         (C2 is the prerequisite for the decider's "single external voice")

## Problem

A hosted room is a self-contained append-only event log. Today it has **no
connection to any chat group**:
- **Inbound** (chat → room): manual only — a human runs `/room create|send` or a
  client calls `groups.send`. There is no auto-route from an IM group message to
  a room (`stream_dispatch.py` has **zero** room references, grep=0).
- **Outbound** (room → chat): **does not exist**. Member turns land back in the
  same log via `_publish_terminal_tasks` → `_append_plan`; a human sees them only
  through the Web read-only inspector or `groups.log`.

C2 bridges both directions so a chat group *is* a room's front door: users talk
in the group, member replies come back into the group.

## Two modes (the choice)

| Mode | Inbound | Outbound | Effort | Risk |
|---|---|---|---|---|
| **Read-only mirror** | none (room still driven by /room, groups.send, or the decider) | push member messages → chat group | low | low — pure additive watcher, no inbound trust surface |
| **Full-duplex** | auto-route group messages → `message.user` | push member messages → chat group | medium | medium — inbound trust, echo-loop, per-user actor mapping |

Recommended: **ship read-only mirror first**, then add inbound as a second slice.
The mirror alone already satisfies the decider's "single external voice" (it
selects which member_ids to emit), and it carries none of the inbound risks.

## Structural precedent (verified): the kanban notifier

`_kanban_notifier_watcher` (kanban_watchers.py:224) is the **exact same shape** C2
outbound needs — reuse its proven pattern rather than invent one:

| Kanban notifier primitive | Verified location | C2 reuse |
|---|---|---|
| durable per-target cursor table | `kanban_notify_subs` (kanban_db.py:1501): PK(task_id,platform,chat_id,thread_id), `last_event_id`, `delivery_mode`, `notifier_profile`, `delivery_metadata` | new `room_notify_subs`: PK(room_id,platform,chat_id,thread_id) + `last_event_seq` + `member_filter` (NULL=all, else decider member_id) |
| claim-then-advance under lock | `claim_unseen_events_for_sub` (kanban_db.py) returns (old_cursor, cursor, events) atomically | mirror `claim_unseen_room_events(room_id, since_seq)` over `hosted_rooms.read_events` (hosted_rooms.py:2455, monotonic delta after since_seq, MAX_LOG_LIMIT=500) |
| supervised background loop | `_spawn_supervised(self._kanban_notifier_watcher, ...)` (run.py:14500), capped-backoff restart, task-level supervision | `_spawn_supervised(self._room_mirror_watcher, ...)` — same helper |
| single-owner dispatch lock | `_owns_kanban_dispatcher_lock` (kanban_watchers.py:214) | reuse the same lease/lock discipline so only one gateway mirrors a room |
| adapter resolution + delivery | `resolve_delivery_transport` (delivery.py:92) → `DeliveryRouter.deliver` (delivery.py:318) → `adapter.send(chat_id, content, metadata)` | identical; room member message → content, sub's (platform,chat_id,thread_id) → target |
| rewind on adapter-disconnect | notifier rewinds the claim if the adapter is gone before send | identical — never advance the cursor past an undelivered message |

**Net: C2 outbound is a near-clone of an already-shipped, battle-tested loop.**

## Fit with hermes-agent

### Outbound (read-only mirror) — fit: HIGH
- **Precise filter point confirmed**: `plan_publication` (discussion.py:1292) emits
  `message.member` with payload `member_id` + actor `profile` (1345/1363). The
  mirror reads these off the event log and emits ONLY rows whose `member_id`
  matches `member_filter` (NULL = all members; decider member_id = single voice).
  This is where the decider's "expose only the decider" promise is *enforced* — in
  C2, not in the discussion policy.
- **Read side is ready**: `read_events(since_seq, limit)` (hosted_rooms.py:2455) is
  already a bounded monotonic delta with the exact cursor semantics the notifier
  pattern needs.
- **No hosted_rooms.py change**: mirror only reads; the new cursor table is
  additive.

### Inbound (full-duplex) — fit: MEDIUM
- **Entry point exists**: `HostedRoomService.send` (hosted_room_service.py:743)
  appends `message.user` (server-owned actor `{kind:user, id:desktop}`), then
  `prepare_room` + `wakeup`. `groups.send` RPC (methods_groups.py:564) already
  wraps it with `user_event_id` idempotency.
- **What's missing (the medium-risk work)**:
  1. **Routing**: `stream_dispatch.py` must recognize "this (platform,chat_id,
     thread_id) is bound to room X" and call `service.send` instead of the normal
     agent path. New binding lookup keyed on the same `room_notify_subs` row.
  2. **Actor identity**: today the actor is hard-coded `id:desktop`. Real group
     users need a stable per-user actor id (the log's actor schema already carries
     `{kind:user, id}` — the id just needs to become the IM user id, verbatim, no
     new field).
  3. **Echo loop**: the mirror must NOT re-mirror a message that originated from
     the same chat group. Guard by actor kind: only mirror `message.member`
     (never `message.user`), which the outbound filter already does — so the loop
     is structurally avoided as long as inbound only writes `message.user`.
  4. **Idempotency**: reuse `user_event_id` (already there) keyed on the IM
     message id so a redelivered group message doesn't double-append.

## Blast radius

### Read-only mirror (slice 1) — additive, low risk
- **new**: `room_notify_subs` table (or reuse a hosted_rooms sidecar); a
  `claim_unseen_room_events` helper; `_room_mirror_watcher` on GatewayRunner
  (via `_spawn_supervised`); a `/room mirror <room_id> <platform:chat_id>` (and
  `--decider-only`) command to create the subscription; i18n.
- **untouched**: `hosted_rooms.py` (read-only), `hosted_room_discussion.py`,
  `hosted_room_service.py` (the worker loop is unaware of mirroring), the driver.
- **shared with kanban**: `delivery.py`, `_spawn_supervised`, dispatch-lock
  discipline — all reused, not forked.

### Full-duplex (slice 2) — touches the hot path
- **modified**: `stream_dispatch.py` gains a room-binding check before normal
  dispatch (the ONE hot-path edit); actor id in `service.send` becomes the IM
  user id.
- **risk controls**: echo-loop avoided by construction (inbound writes only
  `message.user`, outbound mirrors only `message.member`); idempotency via
  existing `user_event_id`; per-user actor mapping is a value change, not a
  schema change.

## Interaction with the decider

C2 is the decider's **enforcement layer**, not just a sibling feature:
- Decider **v1** (internal star scheduling) is testable via the event log alone,
  but its headline promise — "the chat group sees ONE voice" — is literally the
  `member_filter = <decider member_id>` column on the mirror subscription.
- Therefore: **C2 read-only mirror should ship before or with the decider.** A
  decider without C2 schedules correctly but its output stays in the log, exactly
  like any room today.
- Recommended sequencing: **C2 mirror → decider v1 (set member_filter=decider) →
  C2 inbound → decider v2/v3.**

## Verification (same discipline as C1)
Real E2E against a live bound `HostedRoomService` on a COPY of prod `state.db`:
- **Mirror**: create room + subscription → send `message.user` → drive a member
  turn → assert the watcher claims the `message.member`, advances `last_event_seq`
  exactly once, and calls `adapter.send` with the member text; assert
  `member_filter` suppresses non-matching members; assert a rewind on a stubbed
  adapter-disconnect does NOT advance the cursor.
- **Inbound** (slice 2): stub an IM group message on a bound (platform,chat_id)
  → assert it becomes exactly one `message.user` (idempotent on redelivery) with
  actor id = IM user id → assert it is never mirrored back (no echo).
