# Hosted Room ↔ Task DAG (C3)

Status: SHIPPED (this slice). See "Build status" in
        docs/design/hosted-room-chat-bridge.md.
Author: drafted for later0day
Related: gateway/hosted_rooms.py (event log, untouched), gateway/room_mirror_db.py
         (structural precedent — additive sidecar store), hermes_cli/kanban_db.py
         (mature DAG reference), docs/design/claude-agent-team-reference.md §4 + A.2
         (CC's shared task DAG + the VERBATIM self-claim algorithm = F3)

## Problem

A hosted-room discussion is turn-based `@`-mention ping-pong. There is no
*shared work ledger* the members can coordinate around: no "these are the open
sub-tasks, who owns what, what's blocked on what". CC's agent teams solve this
with a **shared task DAG** (the C3 reference model, binary-verified):

- tasks: `{id, subject, description, status: pending|in_progress|completed,
  owner?, blockedBy[]}` (docs.claude.com/agent-teams + `sdk-tools.d.ts`)
- **pull-based self-claim** (VERBATIM teammate prompt, native binary v2.1.226):
  > 2. Look for tasks with status 'pending', no owner, and empty blockedBy
  > 3. Prefer tasks in ID order (lowest ID first) …
  > 4. Claim an available task using TaskUpdate (set `owner` to your name), or
  >    wait for leader assignment
- claim race guarded by `withQueueFileLock`; assignment is **dual-mode** — the
  lead can push (`TaskUpdate owner=<name>`, `isTaskAssignment`) OR a teammate
  pulls. Completing a task **auto-unblocks** its dependents.

C3 gives hosted rooms that same ledger: the decider decomposes a request into a
DAG of sub-tasks with dependencies; workers self-claim the next available one
(or the decider push-assigns); completion unblocks dependents. This is the F3
mechanism from the CC deep-dive, deferred out of decider v1/v2 to here.

## Non-goals (explicit scope fence)

- **NOT new room event kinds.** CC keeps the task list *separate* from the
  mailbox ("the task list is never uploaded"; the lead reads it on demand via
  TaskList). Hosted-room events are a fixed, validated kind vocabulary in
  `hosted_rooms.py`; adding kinds would touch that file. The DAG is a **sidecar
  store queried on demand**, exactly like CC's TaskList = "read shared state to
  decide next". `hosted_rooms.py` and `hosted_room_discussion.py` are UNTOUCHED.
- **NOT automatic loop integration.** Making `plan_next_task` auto-emit tasks or
  auto-route claims into member turns is a larger follow-on (call it C4) with its
  own blast radius on the discussion hot path. C3 delivers the *complete,
  usable* ledger + claim mechanics + the command surface to drive it. The
  decider (human via `/room task`, or the decider bot guided by its F4 prompt)
  drives it explicitly.
- **NOT the kanban.db DAG.** `hermes_cli/kanban_db.py` is the *reference* for the
  data model, but it is a heavyweight worker-dispatch kernel (leases, runs,
  heartbeats, unblock-loop breakers). A room task DAG is a lightweight
  coordination ledger; reusing kanban.db would drag in machinery a room does not
  need and couple two independent subsystems (kanban↔room grep=0 today — keep it
  that way). C3 is a *new* minimal store that borrows kanban's proven
  concurrency discipline (WAL + BEGIN IMMEDIATE + CAS), not its schema.

## Structural precedent (verified): room_mirror_db.py

C3 is a near-sibling of the C2 mirror store — the same additive-sidecar shape:

| room_mirror_db.py primitive | C3 reuse |
|---|---|
| own `_connect` (apply_wal_with_fallback + retry) + `_write_txn` (BEGIN IMMEDIATE) | identical |
| `CREATE TABLE IF NOT EXISTS` in the same state.db, invisible to `hosted_rooms._schema_is_current` (subset checks ignore extra tables — verified hosted_rooms.py:606) | identical — adds `room_task_dag` + `room_task_deps`, no migration |
| CAS-guarded state transition under the writer lock | claim/complete are CAS updates; concurrent workers serialize on SQLite's writer lock, so exactly one claims a given task |

**Net: C3 reuses an already-shipped, battle-tested concurrency pattern.** No
hosted_rooms.py change, no migration harness, no new event kinds.

## Data model

```
room_task_dag (
    room_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,      -- caller-supplied or auto (t1, t2, …)
    subject     TEXT NOT NULL,      -- imperative title (CC: "subject")
    description TEXT NOT NULL DEFAULT '',  -- enough for ANOTHER agent (F4)
    status      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','in_progress','completed')),
    owner       TEXT,               -- member handle; NULL = unclaimed
    seq         INTEGER NOT NULL,   -- monotonic creation order → lowest-id tie-break
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    dispatch_thread_id TEXT,        -- C5: the `dagtask:<id>` anchor thread a
                                    -- claimed task's auto-dispatched member turn
                                    -- runs on; NULL until claim_task_for_dispatch
                                    -- stamps it (added by a guarded ALTER, see §C5)
    PRIMARY KEY (room_id, task_id)
)
room_task_deps (
    room_id       TEXT NOT NULL,
    task_id       TEXT NOT NULL,    -- the dependent (blocked) task
    blocked_by    TEXT NOT NULL,    -- must complete before task_id is claimable
    PRIMARY KEY (room_id, task_id, blocked_by)
)
```

`blockedBy` is the edge set in `room_task_deps`; `blocks[]` is its inverse
(derived by querying the other direction), matching CC's bidirectional view.

## Operations (the API)

- `create_task(room_id, subject, description='', task_id=None, blocked_by=())`
  — starts `pending`, no owner, `seq` = next monotonic. Auto-allocates `t<N>`
  when `task_id` is None. Idempotent on `(room_id, task_id)`. Rejects a
  `blocked_by` that would create a **cycle** (fail-closed, before insert).
- `add_dependency(room_id, task_id, blocked_by)` — set a `blockedBy` edge after
  creation (CC sets deps post-create). Cycle-checked. Idempotent.
- `claim_next(room_id, owner)` — **F3 pull self-claim.** Under BEGIN IMMEDIATE:
  pick the lowest-`seq` task that is `pending` ∧ `owner IS NULL` ∧ every
  `blocked_by` is `completed`; set `owner=<owner>`, `status='in_progress'`.
  Returns the claimed task or None. Concurrent workers serialize on the writer
  lock → exactly one claims a given task.
- `claim_task(room_id, task_id, owner)` — claim a *specific* available task
  (same availability predicate); rejects if unavailable.
- `assign_task(room_id, task_id, owner)` — **push-assign** (CC `isTaskAssignment`
  branch): decider sets `owner` directly (task → `in_progress`) regardless of
  self-claim ordering, but still refuses a task blocked by an incomplete dep.
- `complete_task(room_id, task_id)` — `status='completed'`. Dependents are
  **auto-unblocked implicitly**: `claim_next` re-evaluates the predicate every
  call, so a dependent becomes claimable the instant its last blocker completes
  (no stored "blocked" flag to flip → no unblock-loop bug class).
- `release_task(room_id, task_id)` — return an in_progress task to `pending`,
  clear owner (worker gives up / crash recovery).
- `list_tasks(room_id)` / `get_task(room_id, task_id)` — survey
  `{task_id, subject, description, status, owner, seq, blockedBy[], blocks[]}`.
  This is CC's TaskList = read shared state on demand.

### Availability predicate (the load-bearing invariant)
A task is **claimable** iff:
`status = 'pending'` ∧ `owner IS NULL` ∧ `∄ dep ∈ blockedBy : dep.status ≠ 'completed'`.
Tie-break: lowest `seq` (monotonic creation order = CC's "lowest ID first").
A task blocked by a *missing* task id is treated as blocked (fail-closed), so a
typo can't accidentally make work claimable.

### Cycle prevention
`create_task` / `add_dependency` reject an edge whose `blocked_by` can already
(transitively) reach `task_id` — a DFS over `room_task_deps` before insert,
inside the same write txn so it is race-free. Guarantees the graph stays a DAG,
so the availability predicate always terminates.

## Command surface

```
/room task list <room_id>
/room task add  <room_id> <subject...> [--desc="..."] [--after=<task_id>[,<task_id>…]]
/room task dep  <room_id> <task_id> --after=<task_id>[,<task_id>…]
/room task claim <room_id> <handle> [<task_id>]      # no id = pull next available
/room task assign <room_id> <task_id> <handle>
/room task done <room_id> <task_id>
/room task release <room_id> <task_id>
```

Thin parse+render adapter over `room_task_dag`, mirroring the existing `/room`
subcommand style. `--after` is the `blockedBy` edge (human-readable: "do this
after t1").

## Blast radius

- **new**: `gateway/room_task_dag.py` (self-contained store + claim logic);
  `/room task …` subcommands in `gateway/slash_commands.py`; i18n; tests.
- **untouched**: `hosted_rooms.py` (no schema/kind change), `hosted_room_discussion.py`,
  `hosted_room_driver.py`, `hermes_cli/kanban_db.py` (kept decoupled),
  `gateway/run.py` (no watcher needed — the DAG is pulled on demand, not pushed).
- **shared**: the additive-sidecar concurrency pattern from room_mirror_db.py.

## Verification

Exhaustive unit tests against real sqlite (venv has no pytest → standalone
harness, same as C2/decider):
- create defaults (pending, no owner, monotonic seq) + idempotency
- availability predicate: unblocked claimable, blocked NOT claimable, blocked-by
  missing id NOT claimable
- `claim_next` tie-break = lowest seq; second claim skips the owned one
- complete a blocker → dependent becomes claimable (auto-unblock)
- concurrent self-claim serializes (two claim_next calls never claim the same id)
- push-assign overrides ordering but still refuses a blocked task
- release returns to pending/unowned
- cycle rejection (direct A→A, 2-cycle A→B→A, transitive A→B→C→A)
- list/get surface blockedBy[] + blocks[] both directions

---

## C4 — Wiring the DAG into the live scheduler (the integration)

C3 (above) delivered the *store* + claim mechanics + command surface. But an
audit found the store was an **orphan**: `grep` proved `room_task_dag` was
referenced only by `slash_commands.py` — the decider never wrote dispatched
sub-tasks into it, and the scheduler never read from it. A parallel manual
kanban is not "the CC shared task DAG"; the load-bearing value is the closed
loop *decider decomposes → workers pick up → completion advances the plan*.

### The architectural truth that shaped C4

The shipped scheduler (`plan_next_task` + `hosted_room_service.prepare_room` +
`hosted_room_driver`) is **push-based**, not pull-based:
- the decider's `@mention` **is** the dispatch (assignment),
- a worker's next non-`(pass)` reply **is** the completion,
- watermarks (`_derive_member_watermarks`) are the "who has seen what" polling,
- `driver` task statuses (queued/running/settled/…) are the real execution state.

Forcing CC's *literal* pull-based `claim_next` into `prepare_room` would fight
this battle-tested v1/v2 machinery and risk breaking a shipped scheduler. So C4
takes the honest path: **project** the task DAG from the same committed event log
the scheduler replays, instead of maintaining a second, drift-prone ledger.

### `project_task_dag` (gateway/hosted_room_discussion.py)

A pure function (no I/O, same contract as the rest of the module) that returns
the live `ProjectedTask` DAG:
- one task per decider `@mention` of a worker (`owner` = that worker),
- `status = dispatched` until the worker replies, then `completed`,
- `blocked_by` = still-open tasks from *earlier* decider messages in the thread
  (later-round dispatches implicitly depend on prior rounds); workers named in
  the **same** decider message run in parallel and never block each other,
- deterministic, ordered by dispatch seq — so it always matches what the
  scheduler actually did. **Zero drift is possible by construction.**

Mesh rosters (no decider) have no orchestration ledger, so the projection is
empty for them (they coordinate purely by mention/watermark).

### Command surface change

`/room task list <room_id>` now shows the **live projection** (the decider's
real decomposition + each worker's real status) by default. `--manual` shows the
hand-authored `room_task_dag` ledger (still available for explicit planning).
This makes the DAG reflect reality out of the box rather than being a parallel
universe.

### Verification

- 5 new projection tests in `tests/gateway/test_hosted_room_discussion.py`
  (dispatch→completion, parallel dispatch not blocked, later-round blocking,
  determinism, empty-without-decider) — full suite **60 passed**.
- End-to-end through the real `_handle_room_command`: drove a decider discussion
  and watched `/room task list` transition `dispatched → completed` as workers
  replied; `--manual` ledger verified independently. C3 store suite still
  **20 passed** (no regression).

### What remains genuinely out of scope (named honestly)

True *pull-based auto-claim into member turns* (a worker autonomously grabbing
the next projected-available task and the scheduler executing it without a
decider `@mention`) would require `prepare_room` to admit tasks from the
projection, not just from `plan_next_task`. That is a scheduler-behavior change
with real blast radius and belongs in its own slice with its own E2E proof — it
is deliberately **not** hidden inside C4. C4's claim is precise: the DAG is now a
faithful, wired projection of the real dispatch, surfaced to users and
drift-free — not a decorative orphan.

---

## C5 — Scheduler-mediated auto-dispatch of manual DAG tasks into member turns

> **Naming note (honest framing).** CC's F3 is a *worker* **pull self-claim**:
> an idle teammate autonomously grabs the next available task. C5 is **not**
> that. C5 keeps the shipped **push-based, scheduler-mediated** model: the
> service — at the `idle` seam — claims the next available manual task on the
> worker's behalf and dispatches it by appending an ordinary `@mention` anchor
> the *existing* scheduler executes. The DAG claim (`claim_task_for_dispatch`)
> is CC-shaped (pending ∧ unowned ∧ unblocked, lowest-seq, CAS under
> `BEGIN IMMEDIATE`), but the *actor* is the scheduler, not a self-directed
> worker turn. True worker-initiated pull-into-turns remains the named future
> slice below (§"genuinely out of scope").

C4 named the honest gap: a *manual* `room_task_dag` task never became a real
member turn on its own — a human still had to `@mention` a worker. C5 closes
exactly that loop **without touching the tested `plan_next_task` scheduler** and
without violating any of the crown-jewel invariants (turn identity, frozen
driver payload, reconstruction-after-restart).

### The safe seam: `idle`

`prepare_room` already returns `status="idle"` when the mention-driven scheduler
has nothing to do (no queued/running/stopping task, no pending user event). That
is the *only* place C5 acts — never competing with a real turn:

1. **Sweep first** (`_sweep_completed_dag_dispatches`, called right after
   `prune_published_terminal_tasks`, before the queued/running guard): scan the
   log for a settled `message.member` on a `dagtask:<id>` thread and mark the
   matching in-progress DAG task `completed` (auto-unblocking dependents). This
   runs *before* the dispatch decision so a just-finished task's dependents are
   claimable in the same tick.
2. **Auto-dispatch on idle** (`_maybe_autodispatch_dag_task`, in the
   `decision.status == "idle"` branch): pick the lowest-seq claimable task
   (`next_claimable`), resolve **exactly one** `@handle` target from its subject
   with the same `resolve_mentions` the scheduler routes with (ambiguous or
   unaddressed subjects are skipped), atomically `claim_task_for_dispatch`
   (stamping a per-task anchor thread `dagtask:<id>`), then append a
   `message.user` anchor `@handle <subject>` on that thread. The existing
   scheduler then routes, executes, and publishes that turn — reusing **100%** of
   the turn-coordinate / reconstruction machinery. On append failure the claim is
   released so it retries next tick. Both helpers are best-effort (swallow
   exceptions) and can never break the tested path.

A `runtime.wakeup()` after a successful dispatch and after each completed sweep
keeps the loop responsive (immediate follow-up pass instead of a full poll
interval) — the same courtesy `send` already extends.

### Why this respects the invariants

- **No new dispatch path.** The anchor is an ordinary `message.user` event; the
  turn it spawns is admitted by the *unchanged* `plan_next_task` →
  `admit_task` path, so its `turn_id`/`task_id` are derived and reconstructed
  exactly like any human-typed `@mention`. Nothing new to round-trip on restart.
- **Frozen driver payload untouched.** The DAG↔turn mapping lives entirely in the
  additive store (`dispatch_thread_id` column), never in the driver payload the
  reconstruction rejects extra keys from.
- **Echo/double-dispatch impossible.** `claim_task_for_dispatch` is a CAS under
  `BEGIN IMMEDIATE` (pending ∧ unowned ∧ unblocked → in_progress); a second
  scheduler tick or process finds the task owned and dispatches nothing.
  Completion keys on the `dagtask:` thread, so re-sweeps are idempotent.

### Store additions (`gateway/room_task_dag.py`, additive)

- `dispatch_thread_id TEXT` column on `room_task_dag`, added via a **guarded
  ALTER** in `_connect` (duplicate-column-name = success) so a C3/C4 `state.db`
  upgrades in place with no migration harness — same additive-safety posture as
  the whole store (still invisible to `hosted_rooms._schema_is_current`).
- `next_claimable(room_id)` — read-only peek at the next available task.
- `claim_task_for_dispatch(room_id, task_id, owner, dispatch_thread_id)` —
  atomic specific-claim + stamp anchor thread; returns `None` (never raises) if
  the task is no longer available.
- `dispatched_task_for_thread(room_id, dispatch_thread_id)` — read-only lookup
  of the in-progress task on an anchor thread. `_sweep_completed_dag_dispatches`
  uses it as a **cheap pre-check** on every `prepare_room` cycle: a re-sweep of
  an already-completed `dagtask:` thread returns `None` here and skips the
  `BEGIN IMMEDIATE` write in `complete_dispatched` entirely, so only a still-
  in-progress dispatch ever pays for the write.
- `complete_dispatched(room_id, dispatch_thread_id)` — mark that task
  `completed` (idempotent).

### Verification

- **Store-level** (`tests/gateway/test_room_task_dag.py`, +10 tests → **30
  passed**): peek-doesn't-claim, skip-blocked/owned, dispatch-claim stamps
  thread, returns-None-when-unavailable, refuses-blocked, requires-owner+thread,
  thread lookup, complete-and-unblock, complete idempotent, and a guarded-ALTER
  in-place upgrade of a pre-C5 db.
- **Service-level E2E** (`tests/tui_gateway/test_hosted_room_service.py`, +2
  tests): the closed loop driven **purely by the runtime loop** (no manual
  ticks) — two manual tasks (`t2` blocked by `t1`), each `@handle`-targeted,
  end `completed` with the right owner, and the log carries the `message.user`
  anchor + the worker's `message.member` reply on each `dagtask:` thread; plus a
  negative test that an ambiguous / unaddressed subject is never dispatched and
  the room stays quiet. Canonical E2E
  (`test_create_send_drive_publish_and_replay_without_client_transport`) and the
  restart-republish test still pass — the tested scheduler is untouched.

C5's claim is precise: a manual DAG task with a single `@handle` subject now
becomes a real member turn on its own, its completion unblocks and dispatches
its dependents, and the whole loop runs through the shipped push-based scheduler
with zero changes to the identity/reconstruction crown jewels.
