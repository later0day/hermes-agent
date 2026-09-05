# Hosted Room Decider — Orchestrator-Worker Role for Multi-Agent Rooms

Status: design proposal (not yet implemented)
Author: drafted for later0day
Related: gateway/hosted_room_discussion.py, tui_gateway/hosted_room_service.py,
         gateway/slash_commands.py `/room` (C1 shipped: fa6ecd61b1)

## Problem

Hosted rooms today are a **decentralized group chat**: who speaks next emerges
purely from `@handle` mentions (`plan_next_task`). Round 0 = everyone the user
mentioned (or all); later rounds = anyone a bot `@`-cited and who has not yet
replied (`_unaddressed_member_mentions`). There is no central coordinator.

Two product requirements:
1. A room needs a **decider** role that evaluates the request and dispatches
   the right members to do the work.
2. Only the decider is **exposed to the chat group** (the group sees one
   coherent voice, not N agents talking over each other).

This is a shift from **GroupChat (mesh)** to **Orchestrator-Worker (star)**.

## Industry survey (what the field does)

| Framework | Topology | Context | Worker↔worker | Coordinator |
|---|---|---|---|---|
| **Claude Code — subagents** | star | **isolated** (subagent own context window; returns a summary to caller) | named subagents can `SendMessage` each other | main agent delegates & collects results in one session |
| **Claude Code — agent teams** (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) | **mesh + shared task list** | each teammate fully independent context | ✅ direct `SendMessage` via per-agent JSON mailbox | `team-lead` session assigns/synthesizes; teammates self-claim tasks (file-locked) |
| **Claude Code — dynamic workflows** (`/workflows`) | star (script-driven) | script variables hold intermediate results | via the script | a JS script holds the plan (agent()/parallel()/pipeline()/phase()), not turn-by-turn judgment |
| **LangGraph supervisor** | star | shared state, scoped reads | ❌ (edges only to supervisor) | supervisor node routes `next: worker \| FINISH` |
| **CrewAI hierarchical** | star | shared | ❌ | auto-manager: delegate → review → re-delegate/accept; role/goal/backstory injected |
| **OpenAI Swarm** | mesh | shared | via handoff (control transfer) | none — each agent hands off |
| **AutoGen GroupChat** | mesh | shared | ✅ | `GroupChatManager` next-speaker: round_robin/auto/manual |
| **Hermes rooms (today)** | mesh | shared | ✅ (@mentions) | none |

> **Correction (real-source mining, CC v2.1.226 npm + docs.claude.com).** CC is
> not one topology. It offers a *spectrum*: (1) **subagents** — closest to a star,
> workers return summaries to the caller, but named subagents can `SendMessage`
> each other (not pure fire-and-forget as an earlier draft claimed); (2) **agent
> teams** — an explicit **mesh** with a `team-lead`, a **shared task list with a
> dependency DAG** (blocks/blockedBy, file-locked self-claim), and a **mailbox**
> (`~/.claude/teams/{team}/inboxes/{agent}.json`) for direct agent↔agent messages;
> (3) **dynamic workflows** — the plan moves *out* of the model into a rerunnable
> JS script. Our decider blueprint is still **LangGraph-supervisor star + CrewAI
> role injection** (single external voice = "expose only the decider"), which none
> of CC's three modes gives directly — CC's team-lead does not hide teammates from
> the user (you can `@`-message any teammate). But CC's **Task dependency DAG** is
> the reference model for our C3 (Room↔Kanban), and its **hooks quality gates**
> (`TeammateIdle`/`TaskCreated`/`TaskCompleted`, exit-2 to block+feedback) are the
> reference for enforcing "decider must dispatch or answer, never (pass)" (risk D).

### Claude Code deep mining (what we can actually borrow)

Sources: `@anthropic-ai/claude-code@2.1.226` npm `sdk-tools.d.ts`, and the
`docs.claude.com/en/docs/claude-code/{sub-agents,agent-teams,agents,workflows,
agent-view,cross-session-messaging,worktrees}.md` pages.

- **`Agent` tool input** (`AgentInput`): `description`, `prompt`, `subagent_type`
  (built-ins `Explore`/`Plan`/`general-purpose`/`claude`, or a user/project
  subagent name, or `"fork"` to inherit parent context), `model`
  (sonnet/opus/haiku, or a `fable`), `run_in_background`, `name` (makes the agent
  addressable via `SendMessage({to:name})`), `isolation` (`"worktree"|"remote"`).
  `team_name`/`mode` are DEPRECATED — "the session has a single implicit team".
- **`AgentOutput`** has 3 states: `completed` (with usage/toolStats/worktreePath),
  `async_launched` (agentId/outputFile), `remote_launched` (taskId/sessionUrl).
- **Task tools** (`TaskCreate/Get/Update/List/Stop`) = a **dependency DAG**:
  status pending/in_progress/completed(/deleted), `blocks[]`/`blockedBy[]`,
  auto-unblock on dependency completion, file-locked claim. → **C3 reference.**
- **Workflow DSL** (`WorkflowInput`): script must begin with
  `export const meta = {name, description, phases}` then
  `agent()/parallel()/pipeline()/phase()`; `resumeFromRunId` caches completed
  `agent()` calls; `scriptPath` persists for iteration. → far heavier than what a
  room needs, but validates "plan-as-code" as the ceiling above turn-by-turn.
- **Agent teams mechanics**: `team-lead` + teammates; shared task list at
  `~/.claude/tasks/{team}/` (persists across resume); mailbox JSON at
  `~/.claude/teams/{team}/inboxes/{agent}.json` (validated per-entry, malformed
  entries dropped); config at `~/.claude/teams/{team}/config.json` `members[]`
  (each has name + agent type; lead's type is `team-lead`); subagent definitions
  reusable as teammate roles (tools/model/body/mcpServers applied per display
  mode). **Key contrast with our design: CC does NOT hide teammates** — the user
  can arrow-select and directly message any teammate; our decider is the *single*
  external voice, which is the deliberate delta.
- **Hooks as quality gates**: `TeammateIdle` (exit 2 → keep working),
  `TaskCreated`/`TaskCompleted` (exit 2 → block + feedback). → the enforcement
  pattern for our risk D ("decider must never `(pass)`").
- **What CC does NOT give us**: a coordinator that is the sole voice to an
  *external* chat surface. CC's lead is a coordinator *for the user*, not a proxy
  that hides workers from a downstream group. That gap is exactly our decider.

**Chosen blueprint: LangGraph-supervisor topology + CrewAI role injection**,
implemented on Hermes' existing serial driver. The decider is the star hub;
workers only report back to it; the decider is the room's single external voice.

Rejected: Swarm handoff (decider would lose control, violating "expose only the
decider"); pure AutoGen mesh (that is what we have — the change is to constrain
it).

## Design decisions (locked with user)

1. **Explicit creation**: `/room create <id> <name> --decider=<profile> <worker1> <worker2>…`
2. **Chat parity**: talking to the decider in the group feels like talking to a
   normal agent; workers are invisible behind it.
3. **Decider only schedules, never does business work**: pure coordinator.

## Architecture

```
┌──────────────────────────────────────────────┐
│  Chat group (sees ONLY the decider)           │
└───────────────┬──────────────────────────────┘
                │ inbound:  inject as message.user "@decider …"
                │ outbound: mirror ONLY decider's message.member
        ┌───────▼────────┐
        │  DECIDER        │ r0: understand + @dispatch   r2: summarize + answer
        │ (schedule only) │ ← star hub (LangGraph supervisor)
        └───┬────────┬────┘
       @wk1 │        │ @wk2   (round 0 dispatches IN PARALLEL → stays within 3 rounds)
        ┌───▼──┐  ┌──▼───┐
        │worker│  │worker│  r1: each does the work → @decider hands back
        └──────┘  └──────┘  (star: workers never address each other)
```

## Code-level findings (verified against source)

### Safe / already-works
- **role storage: zero change** — `hosted_rooms._validate_members` keeps member
  dicts verbatim; validation lives in `validate_roster`.
- **role never pollutes event replay** — events store only
  `member.member_id/profile/handle` (discussion.py:964/1013/1014), never the
  whole member object. `role` lives only in `members_json`.
- **role never affects idempotency** — `_member_digest` (→ task_id) hashes
  member_id/profile/handle/target, not role.
- **outbound filter is precise** — `message.member` payload carries `member_id`
  and actor carries `profile` (plan_publication), so the C2 mirror can emit
  ONLY `member_id == decider`.
- **serial execution is guaranteed** — `service.prepare_room` (hosted_room_
  service.py:617) refuses to admit a new task while any queued/running/stopping
  task exists → one member runs at a time, no write races, no worktrees needed.
- **turn_id is opaque to the driver** — driver stores turn_id as TEXT + UNIQUE
  key and never parses it (driver.py:320/347/882). The ONLY structural parse of
  `_TURN_ID_RE` is discussion.py:1214 (`reconstruct_task_plan`, crash recovery).
  The `turn_id` in relay/stream_consumer is a DIFFERENT thing (IM message id).
- **role is orthogonal to peer/local** — peer members carry
  `target:{kind:peer, peer_id, installation_id, profile, capability_digest}`;
  `role` is not in `_REMOTE_MEMBER_FIELDS`, so it applies uniformly to local and
  peer workers without cross-gateway conflict.

### Risks (all located, with mitigations)

| # | Risk | Mitigation |
|---|---|---|
| A | `_exact_fields` rejects unknown fields → `role` must be registered | add `"role"` to `validate_roster` optional set (1 line) |
| B | round cap is hard-coded in `_TURN_ID_RE` (`r(?P<round>[0-4])` since v2) | v1 kept 3 rounds (parallel dispatch); v2 widened the regex + constant to 5 |
| C | round-0 change touches core policy `plan_next_task` | `if has-decider → decider only; else → existing logic` (back-compat), pure fn, fully E2E-able |
| D | `(pass)` semantics: a near-empty decider turn could trigger `silent_round` → room settles before workers run | decider prompt forbids `(pass)`; must always emit an explicit dispatch or final answer |
| E | star topology needs `_unaddressed_member_mentions` to be role-aware (a worker's `@` must NOT pull another worker) | filter mentions: worker `@` only resolves to the decider; decider `@` resolves to any worker |
| F | decider must be owned locally to schedule | require `--decider` to be a LOCAL profile (peer decider cannot be driven here) |

### Context isolation (deep-dive B1)
Today workers see a shared context: `_build_prompt` sends the per-member
`watermark → seen_through_seq` delta of ALL thread messages (incl. other
workers). Claude-style isolation (worker sees only its dispatched sub-task)
would require new delta filtering in `_build_prompt` keyed on role + mention
relationship, WITHOUT disturbing `_derive_member_watermarks` (crash-recovery
depends on watermark advance). **v1 keeps shared context**: the decider must see
everything anyway (to summarize), `MAX_DISCUSSION_MESSAGES=10` caps token
growth, and isolation touches watermark semantics. Isolation is a self-contained
v3 optimization localized to `_build_prompt`.

### Multi-round iteration cost (deep-dive B2)
Raising `MAX_DISCUSSION_ROUNDS` needs exactly TWO hard edits — the constant
(discussion.py:31) AND the regex (discussion.py:46 `[0-2]→[0-4]`); the
`_zero_based_int` bound (discussion.py:649) auto-follows the constant. Because
the driver treats turn_id as opaque, NO schema/migration change is needed. The
ONLY path that must be specifically E2E-tested is **crash recovery**: an
in-flight r3/r4 task whose turn_id is re-parsed by `reconstruct_task_plan` after
restart — assert it rebuilds instead of raising "turn_id is not a Discussion
coordinate". This unlocks CrewAI-style serial dispatch / review-and-redo.

## Round budget accounting
- **Parallel dispatch (v1, 3 rounds)**: r0 decider `@wk1 @wk2`; r1 both workers
  run (different member_index, same round); r2 decider summarizes. Covers
  "understand → dispatch → summarize".
- **Serial iteration (v2, 5 rounds)**: r0 dispatch A; r1 A works; r2 decider
  reviews, dispatches B; r3 B works; r4 decider answers. Enables "see A's result
  before dispatching B" and "reject & redo".

## Roadmap (each version independently shippable and E2E-testable)

**v1 (low risk, main scenario)** — role field + star round-0 + role-aware
mention filter + decider prompt injection (role awareness, no-pass, review
language) + shared context + 3-round parallel dispatch + mirror-only-decider.

**v2 (medium risk) — SHIPPED** — 5-round serial iteration (regex + crash-recovery E2E) =
CrewAI-style review loop.

**v3 (optimization)** — worker context isolation (localized to `_build_prompt`)
= Claude-Code-style token savings.

## Fit with hermes-agent + blast radius (verified against source 2026-09-03)

Every claim below was re-read from source this session, not carried over.

### Data-flow reality check (the load-bearing correction)
There is **no automatic room→chat-group bridge today**. A hosted room is a pure
append-only event log:
- **Inbound** (chat → room): manual only — `groups.send` RPC (methods_groups.py:564,
  server-owned actor, accepts only inert `message.user`) and `/room`
  (slash_commands.py:575). `HostedRoomService.send` (hosted_room_service.py:743)
  appends `message.user` then `prepare_room` + `wakeup`.
- **Worker turn** (the loop): `prepare_room` (599) → serial guard (refuses new task
  while any queued/running/stopping exists, 617-625) → `plan_next_task` (626).
- **Outbound** (room → chat): **does not exist**. `_publish_terminal_tasks` (529)
  only `_append_plan`s the member message back into the *same event log*. The only
  ways a human sees member output are the Web **read-only inspector**
  (`GET /api/rooms/{id}/log`, commit 233f4e497b) and `groups.log`.

**Consequence for the decider:** "expose only the decider to the chat group" has
**no filter target until an outbound bridge (C2) exists** — today nothing is pushed
outward, so there is nothing to hide. So the sequencing is: **C2 outbound bridge
must precede (or ship with) the decider's "single voice" property.** The decider's
*internal* star scheduling (round-0, mention filter) is independently shippable and
testable via the event log, but its headline product promise is coupled to C2.

### Fit: high (the primitives line up)
| Decider need | hermes-agent primitive (verified) | Fit |
|---|---|---|
| role field on member | `_validate_members` keeps dicts verbatim; only `validate_roster` `_exact_fields` gates (discussion.py:392, required={member_id,profile,handle}, optional={display_name,target}) | add `"role"` to optional set = 1 line |
| role must not corrupt replay | events store only member_id/profile/handle (not the member object); `_member_digest` (839) hashes member_id/profile/handle/target — **not role** | zero idempotency impact |
| decider must be local | `validate_roster(local_profiles=…)`; `_REMOTE_MEMBER_FIELDS` (58) rejects cross-gateway fields; `role` not among them | orthogonal to peer/local |
| serial, no write races | `prepare_room` serial guard (617-625) → one member at a time | no worktrees needed |
| turn_id opacity | driver validates via generic `_IDENTIFIER_RE=^[A-Za-z0-9][A-Za-z0-9._:-]*$` (driver.py:53/158), **not** structural; UNIQUE(room_id,thread_id,turn_id) at 347. Only structural parse of `_TURN_ID_RE` is discussion.py:1214 | round-count changes need no schema/migration |
| C3 (Room↔Kanban) | `kanban_watchers.py` has **zero** references to hosted_room/room_id (grep=0) | fully decoupled → C3 is greenfield glue, mirrors CC's Task DAG |

### Blast radius by version
- **decider v1 (internal star)** — touches ONLY `hosted_room_discussion.py`
  (pure functions, fully E2E-able off the live loop): `DiscussionMember.role`;
  `validate_roster` (role∈{decider,worker}, exactly-one-decider, decider local);
  `plan_next_task` round-0 branch (1120: `resolve_mentions` → decider-only when
  present); role-aware `_unaddressed_member_mentions` (517: worker `@` must not
  pull another worker); `_build_prompt` (883) decider system-prompt injection.
  Plus 1 line in `hosted_room_service.update_members`/`create_room` roster path,
  `slash_commands.py` `--decider=` parse, i18n. **hosted_rooms.py: untouched.**
- **decider "single voice" (product promise)** — requires C2 outbound bridge:
  `plan_publication` (1292) already isolates the outbound unit — `message.member`
  payload carries `member_id` + actor.profile (1345/1363), so a C2 bridge can emit
  ONLY `member_id == <decider>`. Precise filter point confirmed.
- **v2 (5-round serial)** — SHIPPED. 2 edits (constant `MAX_DISCUSSION_ROUNDS`
  line 31 + `_TURN_ID_RE` round group line 53 `[0-2]→[0-4]`); the `range(...)`
  loop, the `MAX_DISCUSSION_ROUNDS - 1` bound and `_zero_based_int` maximum all
  auto-follow the constant. Only crash-recovery (`reconstruct_task_plan`, ~1301)
  needed a dedicated E2E — see `test_later_round_task_reconstructs_after_restart`
  and `test_five_round_bound`.
- **v3 (context isolation)** — localized to `_build_prompt`; must not disturb
  `_derive_member_watermarks` (795, crash-recovery depends on watermark advance).

### Net assessment
- **Well-fitted, low-risk core**: role field + internal star scheduling. The
  append-only log + serial guard + opaque turn_id + verbatim member dicts all
  cooperate; no schema/migration, no driver change, one storage-untouched path.
- **Coupled dependency**: the user-facing "only the decider is exposed" promise
  is a C2 concern, not a discussion-policy concern. Recommend **C2 outbound
  bridge first (or jointly)**, since the decider without it schedules correctly
  but its output still only lands in the log/inspector, same as any room today.
- **C3** is independent greenfield (kanban↔room grep=0), and CC's Task
  dependency DAG is the ready-made reference.

## Change set (v1)
- `hosted_room_discussion.py`: `DiscussionMember.role`; `validate_roster` (role
  in optional, role ∈ {decider,worker}, exactly-one-decider, decider must be
  local); `plan_next_task` round-0 (decider only when present); role-aware
  `_unaddressed_member_mentions`; `_build_prompt` decider branch.
- `hosted_rooms.py`: none (verbatim member dicts).
- `slash_commands.py`: `--decider=` parse in create; usage/i18n.
- `locales/en.yaml`, `locales/zh.yaml`: decider strings.

## Deepest CC mechanism findings → decider design (binary-mined, v2.1.226)

Mined from the native binary's embedded JS + real system-prompt text. See
`claude-agent-team-reference.md` Appendix A for the full extract. The five
findings that directly change how we build the decider:

### F1 — "Schedule-only" is a hard TOOL WHITELIST, not a prompt (orchestration-only)
CC's coordinator runs through `applyCoordinatorToolFilter`. The whitelist,
decoded from the binary, is exactly six tools:
`COORDINATOR_MODE_ALLOWED_TOOLS = new Set([Agent, SendMessage, ListAgents,
Workflow, TaskStop, StructuredOutput])`. **Note what is absent: not just no
Edit/Write — there is no `Read`, no `Bash`, no `Grep` either.** The coordinator is
*orchestration-only*: it can delegate (`Agent`), talk to members (`SendMessage`,
`ListAgents`), orchestrate (`Workflow`), stop tasks (`TaskStop`), and emit
structured output — nothing else. Forking is refused (`COORDINATOR_FORK_REFUSAL`).
**Implication for Hermes:** decider decision #3 ("only schedules, never does
work") should not rely on prompt-only. **Shipped (hard enforcement):** the
decider member's local `bot_room` agent is built with its toolset *intersected*
against an orchestration-only allow-set — the Hermes analogue of
`applyCoordinatorToolFilter`. `gateway/hosted_room_execution_policy.py` defines
`ORCHESTRATION_ONLY_TOOLSETS = {bot_room, todo, clarify}` and
`orchestration_only_toolsets(base)` (intersect-then-force-`bot_room`). Generic
`delegation` is deliberately excluded: a Room decider dispatches durable frozen-
roster workers through `@mention`; `delegate_task` would instead create ephemeral
subagents outside the Room event log and can starve the configured workers;
`tui_gateway/server._room_decider_toolset_filter` applies it at
`_make_agent` for exactly the room member whose persisted `role == "decider"`
(a decider is always local and owns a unique local profile, so the room session
`Group:<room_id>` + profile identifies it). A decider bound to a full engineer
profile (file/terminal/code_execution/web/browser) therefore *cannot* Read/Bash/
Edit/Write — those toolsets are stripped before the agent snapshots its tools;
`bot_room` is always retained so it can still `@mention` and speak. Workers are
untouched (fail-open for anything not a positively-identified decider). This is
enforcement, not a prompt: pointing `--decider` at an orchestration-only profile
is still the *recommended* config, but a mis-pointed decider is now narrowed
regardless. (Correction: an earlier draft called this "read-only"; the binary
whitelist has no Read tool — it is strictly stronger than read-only. A second
earlier draft claimed this was "config + docs, no code"; the hard filter above
supersedes that — a prompt cannot enforce a tool boundary.)

### F2 — No status poller; react to terminal events (we already do this)
CC does NOT background-poll teammate status. Teammates PUSH idle/terminal
notifications; the lead reads shared state on demand (`TaskList`) + a quiescence
barrier (`waitForTeammatesToBecomeIdle`). **Hermes' `plan_next_task` replaying the
event log after each `turn.settled` is the SAME shape.** So the decider needs no
new poller, no new loop — the existing worker loop (`prepare_room` → serial guard
→ `plan_next_task`) already IS the "read shared state, decide next" engine. This
removes a feared risk entirely: **decider adds NO runtime machinery.**

### F3 — The scheduling algorithm is pull-based self-claim (informs v2, not v1)
CC workers self-claim: `status=pending ∧ no owner ∧ blockedBy=∅`, tie-break
lowest ID, `withQueueFileLock`. Hermes v1 has no task table, so v1 stays
mention-driven (decider `@`-dispatches; workers reply). The self-claim model is
the **C3** shape (task table with owner/blockedBy) — cross-referenced, not needed
for v1.

### F4 — "Never delegate understanding" → the decider prompt spec
CC's strongest orchestration rule (VERBATIM): *"Never delegate understanding…
Write prompts that prove you understood: include file paths, line numbers, what
specifically to change. Any agent other than a fork starts with zero context;
command-style prompts produce shallow, generic work."* **Implication:** the
decider's injected prompt (risk D / `_build_prompt` branch) must instruct it to
dispatch with concrete, self-contained sub-tasks, not "worker, handle the
backend." This is prompt text only — lands in the existing `_build_prompt` decider
branch already in the v1 change set.

### F5 — Untrusted-peer framing → hardening the decider prompt
CC frames every inter-agent message as *"[MESSAGE FROM NON-USER SOURCE - NOT USER
INPUT]"* and treats relayed approvals as untrusted. Hermes rooms already publish
member text verbatim with a "never reveal private conversations" rule in
`_build_prompt`. **Borrow:** when the decider summarizes worker output to the chat
group (via C2), the decider prompt should treat worker replies as data to
synthesize, not as instructions to obey — one sentence in the decider prompt.

### Minimal-impact placement in Hermes (the bottom line)
| CC mechanism | Hermes minimal-impact realization | Code touched |
|---|---|---|
| coordinator tool-filter (F1) | intersect the decider agent's toolset with an orchestration-only allow-set at build (`applyCoordinatorToolFilter` analogue); recommend an orchestration-only profile | `hosted_room_execution_policy.orchestration_only_toolsets` + `server._room_decider_toolset_filter` (+ role now persisted by `service.create_room`) |
| no poller, event-driven (F2) | reuse `plan_next_task` replay on `turn.settled` | **none** — existing loop |
| self-claim task table (F3) | deferred to C3 | none in decider |
| understand-before-delegate (F4) | decider prompt injection text | `_build_prompt` branch (already listed) |
| untrusted-peer framing (F5) | one line in decider prompt | `_build_prompt` branch (already listed) |
| single external voice | C2 mirror `member_filter=<decider>` | C2, not decider |

**Conclusion:** the deep dive keeps the decider's footprint small. Of CC's five
mechanisms: F2 is already satisfied by the existing loop; F3 is C3; F4/F5 are
`_build_prompt` text; the "single external voice" is C2's `member_filter`. F1 is
the one that needs real (small) code — the orchestration-only tool filter is a
hard build-time boundary (`orchestration_only_toolsets` +
`_room_decider_toolset_filter`), not prompt text, because a prompt cannot
enforce a tool boundary. **No new modules, no new loops, no schema change** —
F1 reuses the existing toolset-authority module and the `_make_agent` seam, and
the decider `role` is now persisted by `service.create_room`/`update_members`
(previously dropped) so both the scheduler and the filter can read it back.

## Verification (same discipline as C1)
Real E2E against a live bound `HostedRoomService` on a COPY of prod `state.db`
with real profile names (never the live DB — the prod worker drives all
local-authority rooms). Assert the full chain: user msg → only decider speaks r0
→ decider `@worker` → worker runs r1 → worker `@decider` → decider summarizes r2.
Back-compat: rooms without a decider keep today's mesh @-mention behavior.
Additionally assert F1 (shipped as
`test_f1_decider_member_is_restricted_to_orchestration_only_tools`): a decider
member bound to a full engineer profile is built with its toolset intersected
down to the orchestration-only allow-set (no file/terminal/code_execution/
browser; `bot_room` retained), while a worker in the same room keeps its full
toolset — confirming schedule-only is a hard tool boundary, not a prompt hack.
