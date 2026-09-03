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
