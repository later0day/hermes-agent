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
| **Claude Code** (`Task`, `--agents`, `--teammate-mode`) | star | **isolated** (subagent own context; returns a summary) | ❌ | main agent spawns fire-and-forget subagents |
| **LangGraph supervisor** | star | shared state, scoped reads | ❌ (edges only to supervisor) | supervisor node routes `next: worker \| FINISH` |
| **CrewAI hierarchical** | star | shared | ❌ | auto-manager: delegate → review → re-delegate/accept; role/goal/backstory injected |
| **OpenAI Swarm** | mesh | shared | via handoff (control transfer) | none — each agent hands off |
| **AutoGen GroupChat** | mesh | shared | ✅ | `GroupChatManager` next-speaker: round_robin/auto/manual |
| **Hermes rooms (today)** | mesh | shared | ✅ (@mentions) | none |

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
| B | round cap is hard-coded in `_TURN_ID_RE` (`r(?P<round>[0-2])`) | v1 keeps 3 rounds (parallel dispatch) → does not touch regex |
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

**v2 (medium risk)** — 5-round serial iteration (regex + crash-recovery E2E) =
CrewAI-style review loop.

**v3 (optimization)** — worker context isolation (localized to `_build_prompt`)
= Claude-Code-style token savings.

## Change set (v1)
- `hosted_room_discussion.py`: `DiscussionMember.role`; `validate_roster` (role
  in optional, role ∈ {decider,worker}, exactly-one-decider, decider must be
  local); `plan_next_task` round-0 (decider only when present); role-aware
  `_unaddressed_member_mentions`; `_build_prompt` decider branch.
- `hosted_rooms.py`: none (verbatim member dicts).
- `slash_commands.py`: `--decider=` parse in create; usage/i18n.
- `locales/en.yaml`, `locales/zh.yaml`: decider strings.

## Verification (same discipline as C1)
Real E2E against a live bound `HostedRoomService` on a COPY of prod `state.db`
with real profile names (never the live DB — the prod worker drives all
local-authority rooms). Assert the full chain: user msg → only decider speaks r0
→ decider `@worker` → worker runs r1 → worker `@decider` → decider summarizes r2.
Back-compat: rooms without a decider keep today's mesh @-mention behavior.
