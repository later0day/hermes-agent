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
- **Entry point exists**: `HostedRoomService.send`
  (`tui_gateway/hosted_room_service.py:743`) appends `message.user` (server-owned
  actor `{kind:user, id:desktop}`), then `prepare_room` + `wakeup`. `groups.send`
  RPC (`tui_gateway/methods_groups.py:564`) already wraps it with `user_event_id`
  idempotency (`hosted_rooms.user_event_id`, hosted_rooms.py:316).
- **What's missing (the medium-risk work)**:
  1. **Routing** (corrected): the inbound entry is NOT `stream_dispatch.py` — that
     module is the *outbound* stream-event renderer (agent→chat) and has zero room
     references (grep=0). The real hot path is `GatewayRunner._handle_message`
     (`gateway/run.py:18266`), which today always routes an inbound message to the
     agent. The full-duplex slice inserts ONE early guarded check there: if the
     source `(platform, chat_id, thread_id)` matches an *inbound-enabled* binding,
     call `service.send` and return instead of running the local agent. Binding
     lookup is keyed on the same `room_notify_subs` row, gated behind an explicit
     `inbound` flag (mirroring output must never silently start ingesting chatter).
  2. **Actor identity**: today `service.send` hard-codes `actor={"kind":"user",
     "id":"desktop"}` (`hosted_room_service.py:757`). Real group users need a stable
     per-user actor id; the log's actor schema already carries `{kind:user, id}`,
     so `send` gains an optional `actor_id` param (default `"desktop"`, so existing
     callers are unchanged) — a value change, not a schema change.
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
- **modified**: `GatewayRunner._handle_message` (gateway/run.py:18266) gains a
  room-binding check before normal agent dispatch (the ONE hot-path edit —
  corrected from an earlier draft that mislabeled it `stream_dispatch.py`, which
  is the outbound renderer); `service.send` (hosted_room_service.py:743) gains an
  optional per-user `actor_id`.
- **risk controls**: echo-loop avoided by construction (inbound writes only
  `message.user`, outbound mirrors only `message.member`); idempotency via
  existing `user_event_id`; per-user actor mapping is a value change, not a
  schema change; inbound is opt-in per subscription (`inbound` flag) so a
  read-only mirror never starts ingesting.

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

## Build status (this doc ↔ commits)
This design is the construction plan; each slice below maps to a real commit so
the codebase and this doc stay in lockstep.

- [x] **Decider v1** (internal star scheduling) — `a6466a91a7`
      `feat(rooms)`. Role-aware `plan_next_task` / `_build_prompt` /
      `_unaddressed_member_mentions`; roster gains a `role` field; at most one
      decider; `hosted_rooms.py` untouched.
- [x] **C2 slice 1 — read-only outbound mirror** — `27c80c0891`
      `feat(rooms): C2 read-only room→chat mirror (outbound)`. New
      `gateway/room_mirror_db.py` (`room_notify_subs` cursor table, additive to
      `state.db`, invisible to `hosted_rooms._schema_is_current`) +
      `gateway/room_mirror_watcher.py` (`_spawn_supervised` sibling of the kanban
      notifier: claim/advance/rewind under `BEGIN IMMEDIATE`, `member_filter`
      single-voice, member_id→handle resolution, 12-failure budget). Slash:
      `/room mirror … [--decider-only]` / `/room unmirror`. Never mirrors
      `message.user` (echo-loop avoided by construction).
- [x] **C2 slice 2 — full-duplex inbound** — auto-routes plain-text group
      messages on an `inbound`-enabled binding → `message.user` on the bound
      room. ONE hot-path edit in `GatewayRunner._handle_message`
      (`_maybe_route_inbound_to_room`, gated on `not command` so slash/skills
      still dispatch locally); `room_notify_subs` gains an `inbound` column
      (in-place ALTER upgrades a slice-1 db, no migration harness);
      `service.send` gains an optional per-user `actor_id` (default `"desktop"`,
      existing callers unchanged); idempotency reuses the room's
      `user_event_id` namespace keyed on the IM message id. Slash:
      `/room mirror … --inbound`. Echo-loop stays impossible: inbound writes
      only `message.user`, outbound mirrors only `message.member`.
- [x] **Decider v2** — 5-round discussions. Two hard edits in
      `hosted_room_discussion.py`: `MAX_DISCUSSION_ROUNDS` 3→5 (line 31) and the
      `_TURN_ID_RE` round group `[0-2]`→`[0-4]` (line 53). The `range(...)`
      loop, the `MAX_DISCUSSION_ROUNDS - 1` bound check and the
      `_zero_based_int` maximum all auto-follow the constant; turn_id is opaque
      to the driver so there is NO schema/migration change. Crash-recovery is
      the load-bearing path (an in-flight r3/r4 turn_id must reconstruct, not
      raise), covered by `test_later_round_task_reconstructs_after_restart`;
      `test_five_round_bound` pins the new cap. Enables CrewAI-style
      review-and-redo serial iteration.
- [x] **C3 — Room↔task DAG** — the explicit shared task ledger with
      `owner`/`blockedBy` (CC's F3 pull-based self-claim). New
      `gateway/room_task_dag.py`: additive `room_task_dag` + `room_task_deps`
      tables in the same `state.db` (invisible to
      `hosted_rooms._schema_is_current`'s subset checks — same proven pattern as
      `room_notify_subs`, no migration). `claim_next` picks the lowest-`seq`
      task that is `pending ∧ unowned ∧ every blockedBy completed` under
      `BEGIN IMMEDIATE`, so a 10-way claim race never double-claims (verified);
      `assign_task` is the decider's push branch; completion auto-unblocks
      dependents implicitly (no stored blocked flag → no unblock-loop bug);
      cycle-rejection via in-txn DFS keeps the graph a DAG. Slash:
      `/room task <list|add|dep|claim|assign|done|release>`. Full design in
      `docs/design/hosted-room-task-dag.md`. `hosted_rooms.py`,
      `hosted_room_discussion.py`, the driver and `kanban_db.py` all untouched.

Sequencing realized: decider v1 → C2 mirror → C2 inbound → decider v2 → C3.
All slices in this doc are now shipped.

- [x] **C4 — wire the task DAG into the live scheduler** — an audit found the
      C3 store was an orphan (referenced only by `slash_commands.py`; the decider
      never wrote to it and the scheduler never read it). C4 closes the loop the
      honest way: `project_task_dag` (pure function in `hosted_room_discussion.py`)
      derives the live task DAG from the same committed event log the scheduler
      replays — the decider's `@mention` is the dispatch, a worker's reply is the
      completion — so it can never drift from real dispatch. `/room task list`
      now shows this live projection by default (`--manual` shows the
      hand-authored ledger). The shipped push-based scheduler is left untouched
      (forcing CC's literal pull-`claim_next` into `prepare_room` would fight the
      battle-tested v1/v2 machinery; true auto-claim into member turns is named as
      a separate future slice, not hidden here). 5 projection tests (suite 60
      passed) + E2E through the real handler; C3 store suite still 20 passed. See
      `docs/design/hosted-room-task-dag.md` §C4.
- [x] **C5 — pull-based auto-dispatch of manual DAG tasks into member turns** —
      C4 wired the DAG as a faithful *projection* but a *manual* `room_task_dag`
      task still needed a human `@mention` to run. C5 closes that loop at the
      one safe seam — `prepare_room`'s `idle` branch (no queued/running task, no
      pending user turn) — without touching the tested `plan_next_task`
      scheduler. A sweep (`_sweep_completed_dag_dispatches`) completes any task
      whose worker turn settled on its `dagtask:<id>` anchor thread (unblocking
      dependents); then `_maybe_autodispatch_dag_task` picks the lowest-seq
      claimable task, resolves exactly one `@handle` target (skips
      ambiguous/unaddressed), atomically `claim_task_for_dispatch` (CAS under
      `BEGIN IMMEDIATE`, stamping the anchor thread), and appends a
      `message.user` anchor the *unchanged* scheduler executes and publishes —
      reusing 100% of the turn-identity/reconstruction machinery. The DAG↔turn
      mapping lives in a new additive `dispatch_thread_id` column (guarded ALTER,
      in-place C3/C4 upgrade, still invisible to
      `hosted_rooms._schema_is_current`). +10 store tests (30 passed) + 2
      service-level E2E tests proving the closed loop runs purely from the
      runtime loop (t1 auto-dispatched → settled → completed → unblocked t2 →
      auto-dispatched → completed) plus a negative ambiguous-subject test; the
      canonical scheduler E2E is untouched and still passes. See
      `docs/design/hosted-room-task-dag.md` §C5.
