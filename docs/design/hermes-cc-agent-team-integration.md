# Hermes ↔ CC Agent Team Integration — Status, Mapping & E2E Test Plan

Status: living document — current as of 2026-09-05
Sources:
- `docs/design/claude-agent-team-reference.md` (CC v2.1.226 binary-mined)
- `docs/design/hosted-room-decider.md` (Hermes decider design)
- `docs/design/hosted-room-chat-bridge.md` (C2 bridge design)
- `docs/design/hosted-room-task-dag.md` (C3 task DAG design)
- Source-tree verification (grep against live code, this session)

---

## 1. Executive Summary

Claude Code's agent team is a **mesh** with a coordinating `team-lead`, a shared task
DAG, and a file-based mailbox. Hermes' hosted rooms are an **append-only event log**
with authority-epoch fencing, serial execution, and a Web inspector. The two systems
solve the same problem (multi-agent coordination) with fundamentally different
transports — CC uses local files, Hermes uses a durable ordered log.

Hermes' design deliberately deviates from CC in one critical aspect: the **decider is
the single external voice** — CC's lead does not hide teammates from the user, but
Hermes' decider proxies all worker output so the chat group sees one coherent voice.

| Dimension | CC agent teams | Hermes hosted rooms |
|---|---|---|
| Topology | Mesh (teammates message each other) | Star (decider hub, workers report to decider) |
| Transport | JSON mailbox files (`~/.claude/teams/{team}/inboxes/`) | Append-only event log (`state.db`) |
| Coordinator | `team-lead` (assigns, does not hide workers) | `decider` (orchestration-only, single external voice) |
| Task model | Shared DAG (pending/in_progress/completed, blockedBy) | @mention turn-taking (C3 adds DAG) |
| Scheduling | Pull-based self-claim (lowest ID, file-locked) | Serial guard (one member/turn), `plan_next_task` replay |
| Execution | Parallel (multiple teammates run concurrently) | Serial (one member at a time, no write races) |
| Context | Isolated (teammate = own context, no history carry-over) | Shared (bounded delta of all thread messages, v3 adds isolation) |
| Quality gates | Hooks (`TeammateIdle` exit-2, `TaskCreated`/`TaskCompleted`) | Observer (rules_checked, violations) + F1 hard tool whitelist |
| Inspector | In-process agent panel / split-pane | Web dashboard (C9: read-only inspector) |

---

## 2. Architecture Mapping: CC → Hermes

### 2.1 Component-by-component

| CC component | Hermes analogue | Status | Notes |
|---|---|---|---|
| **Team lead** (coordinator mode) | **Decider** (role=`decider`) | ✅ SHIPPED | F1 orchestration-only tool whitelist hard-enforced |
| **Teammates** | Workers (role=`teammate`) | ✅ SHIPPED | Full toolset, serial execution |
| **Task list** (`~/.claude/tasks/{team}/`) | C3 Room↔Kanban DAG | 🔲 TODO | `docs/design/hosted-room-task-dag.md` |
| **Mailbox** (`~/.claude/teams/{team}/inboxes/`) | Event log `message.member` / `message.user` | ✅ SHIPPED | Append-only, idempotent `event_id`, authority-epoch fence |
| **Team config** (`config.json` `members[]`) | Room `members` JSON column | ✅ SHIPPED | Persisted verbatim in `state.db` |
| **SendMessage** | `@handle` mentions in member text | ✅ SHIPPED | `plan_next_task` resolves mentions |
| **ListAgents** | Room topology API | ✅ SHIPPED | `GET /api/rooms/{id}/topology` |
| **TaskCreate/Get/Update/List** | C3 task commands | 🔲 TODO | `/room task add|list|dep` |
| **TaskStop** | `/room disband` | ✅ SHIPPED | |
| **Coordinator tool filter** (`applyCoordinatorToolFilter`) | F1 `ORCHESTRATION_ONLY_TOOLSETS` | ✅ SHIPPED | `hosted_room_execution_policy.py` |
| **TeammateIdle hook** | `turn.settled` terminal event | ✅ SHIPPED | `plan_next_task` replay triggers next |
| **TaskCreated/TaskCompleted hooks** | C3 (future) | 🔲 TODO | |
| **Anti-injection protocol** (3 layers) | `_format_message` / `_build_prompt` | ✅ SHIPPED | `hosted_room_discussion.py` §A.8 |
| **Shared store** (blackboard) | `_build_prompt` bounded thread delta | ✅ SHIPPED | Shared context (v3 adds isolation) |

### 2.2 Key design decisions

| Decision | Rationale | CC equivalent |
|---|---|---|
| **Star, not mesh** | Decider is single external voice; workers never address each other | CC's lead does NOT hide teammates — this is the deliberate delta |
| **Serial, not parallel** | Hermes' serial guard prevents write races; no worktrees needed | CC runs teammates in parallel (separate processes) |
| **Append-only log, not files** | Durable, ordered, transport-neutral source of truth | CC uses local JSON files (mailbox/task/config) |
| **Decider = orchestration-only** | Hard tool whitelist, not prompt — F1 enforces at build time | CC's `applyCoordinatorToolFilter` restricts to 6 tools |
| **Pull-based self-claim → C3** | v1/v2 uses @mention dispatch; C3 adds CC-style task DAG with file-locked claim | CC's self-claim algorithm (A.2) |

---

## 3. Implementation Status by Feature

### 3.1 C1 — Room Create / Control (SHIPPED)

**Commands:** `/room create|list|show|add|remove|disband|task`

| Feature | Status |
|---|---|
| Room creation with member profiles | ✅ |
| Room listing (active/disbanded) | ✅ |
| Add/remove members | ✅ |
| Disband room | ✅ |
| Task commands (`task add|list|dep`) | ✅ |
| `/room send` (manual message injection) | ✅ |

**E2E test:** Create a room with decider + worker, send a message, verify event log.

### 3.2 Decider v1 — Internal Star Scheduling (SHIPPED)

| Feature | Status | Code |
|---|---|---|
| `role` field on `DiscussionMember` | ✅ | `hosted_room_discussion.py:DiscussionMember.role` |
| `validate_roster` (role∈{decider,worker}, exactly-one-decider) | ✅ | `hosted_room_discussion.py:validate_roster` |
| Round-0: decider-only `plan_next_task` | ✅ | `hosted_room_discussion.py:plan_next_task` |
| Role-aware `_unaddressed_member_mentions` (worker `@` must not pull another worker) | ✅ | `hosted_room_discussion.py:_unaddressed_member_mentions` |
| Decider `_build_prompt` injection (role awareness, no-pass, review language) | ✅ | `hosted_room_discussion.py:_build_prompt` |
| 3-round parallel dispatch | ✅ | |

**Risk mitigations verified:**
| Risk | Mitigation | Status |
|---|---|---|
| A: `_exact_fields` rejects `role` | `role` added to `validate_roster` optional set | ✅ |
| B: Round cap hard-coded | v2 widened to 5 rounds | ✅ |
| C: Round-0 touches `plan_next_task` | `if has-decider → decider only; else → existing` (back-compat) | ✅ |
| D: `(pass)` semantics | Decider prompt forbids `(pass)` | ✅ |
| E: Star topology needs role-aware mention filter | Worker `@` only resolves to decider | ✅ |
| F: Decider must be local | `--decider` must be a LOCAL profile | ✅ |

**E2E test:** Full turn cycle: user msg → decider r0 → decider `@worker` → worker r1 →
worker `@decider` → decider summarizes r2. Back-compat: rooms without decider keep mesh
behavior.

### 3.3 Decider v2 — 5-Round Serial Iteration (SHIPPED)

| Feature | Status | Code |
|---|---|---|
| `MAX_DISCUSSION_ROUNDS = 5` | ✅ | `hosted_room_discussion.py:31` |
| `_TURN_ID_RE` widened to `[0-4]` | ✅ | `hosted_room_discussion.py:52` |
| Crash-recovery E2E (r3/r4 task reconstructs after restart) | ✅ | `test_later_round_task_reconstructs_after_restart` |
| `reconstruct_task_plan` handles new round bounds | ✅ | `hosted_room_discussion.py:1228` |

**E2E test:** Serial dispatch: r0 decider → r1 worker A → r2 decider reviews → r3
worker B → r4 decider answers. Crash-recovery: restart mid-turn, verify task
reconstruction.

### 3.4 F1 — Decider Orchestration-Only Tool Whitelist (SHIPPED)

| Feature | Status | Code |
|---|---|---|
| `ORCHESTRATION_ONLY_TOOLSETS = {bot_room, delegation, todo, clarify}` | ✅ | `hosted_room_execution_policy.py:32` |
| `orchestration_only_toolsets(base)` (intersect-then-force-`bot_room`) | ✅ | `hosted_room_execution_policy.py:42` |
| `_room_decider_toolset_filter` applied at `_make_agent` | ✅ | `tui_gateway/server.py` |
| Role persisted by `service.create_room`/`update_members` | ✅ | `tui_gateway/hosted_room_service.py` |

This is **hard enforcement**, not a prompt. A decider bound to a full engineer profile
(file/terminal/code_execution/web/browser) cannot Read/Bash/Edit/Write — those
toolsets are stripped before the agent snapshots its tools. `bot_room` is always
retained so the decider can still `@mention` and speak.

**E2E test:** `test_f1_decider_member_is_restricted_to_orchestration_only_tools` —
decider with full profile → toolset intersected to orchestration-only; worker in same
room keeps full toolset.

### 3.5 CC Anti-Injection Protocol (SHIPPED)

Three-layer protocol from CC v2.1.226, implemented in `hosted_room_discussion.py`:

| Layer | CC mechanism | Hermes implementation | Status |
|---|---|---|---|
| 1. XML wrapper | `SCr` function wraps in `<teammate-message teammate_id="..." summary="...">` | `_format_message` wraps member messages | ✅ |
| 2. Anti-escape | `XTe` function backslash-escapes `<teammate-message` in body | `_escape_teammate_tags` regex `<(?=/?teammate-message\b)` | ✅ |
| 3. Provenance framing | `[MESSAGE FROM NON-USER SOURCE]` + shared-store anti-injection | In `_build_prompt` shared opening + decider/worker rules | ✅ |

User messages keep `User (user): <text>` format (trusted, no wrapping).

**E2E test:** Inject a malicious message containing `</teammate-message>`, verify it's
escaped in the prompt and doesn't break the XML wrapper.

### 3.6 C9 — Hosted Room Web Inspector (SHIPPED this session)

| Feature | Backend endpoint | Frontend component | Status |
|---|---|---|---|
| Roster (member list) | `GET /api/rooms` + `GET /api/rooms/{id}` | Room detail panel | ✅ |
| Event replay | `GET /api/rooms/{id}/log` | Event log table | ✅ |
| Authority/driver | `GET /api/rooms/{id}` (embedded) | Authority section | ✅ |
| Peer grants | `GET /api/rooms/{id}/peer-grants` | `PeerGrantsCard` | ✅ |
| Replication health | `GET /api/rooms/{id}/replication-health` | `ReplicationHealthCard` | ✅ |
| Policy trace | `GET /api/rooms/{id}/policy-trace` | `PolicyTraceCard` (collapsible) | ✅ |
| Room/Kanban linkage | (uses peer-grants data) | `LinkageCard` | ✅ |
| Team topology | `GET /api/rooms/{id}/topology` | `TeamTopologyCard` | ✅ |
| Pending actions | `GET /api/rooms/{id}/pending-actions` | Pending actions panel | ✅ |
| Mailbox | `GET /api/rooms/{id}/members/{mid}/mailbox` | — | ✅ (API) |
| Observer monitor | `GET /api/rooms/{id}/observer` | `LiveObserverCard` | ✅ |
| Observer pause/resume | `POST /api/rooms/{id}/observer/{pause,resume}` | Buttons | ⚠️ Backend is stub |

**E2E test results** (this session, 2026-09-05):
- 307 API tests across 4 rooms / 15 endpoints: **0 failures**
- 18 browser UI tests (Playwright + Chrome headless): **0 failures**
- Known: `observer pause/resume` backend is a stub (returns ok but doesn't change state)

### 3.7 C2 — Chat-Group Bridge (NOT YET IMPLEMENTED)

| Feature | Status | Notes |
|---|---|---|
| Outbound mirror (room → chat) | 🔲 | Design: `docs/design/hosted-room-chat-bridge.md` |
| Inbound routing (chat → room) | 🔲 | Requires outbound first |
| `member_filter` (decider-only emission) | 🔲 | The "single external voice" enforcement point |

**Sequencing:** C2 outbound must precede (or ship with) the decider's "single voice"
product promise. Today nothing is pushed outward — member output only lands in the
event log / Web inspector.

### 3.8 C3 — Room↔Kanban Task DAG (NOT YET IMPLEMENTED)

| Feature | Status | Notes |
|---|---|---|
| Task table (sidecar store) | 🔲 | Design: `docs/design/hosted-room-task-dag.md` |
| Self-claim algorithm (pull-based, file-locked) | 🔲 | Mirrors CC's A.2 scheduling algorithm |
| Dependency DAG (blocks/blockedBy, auto-unblock) | 🔲 | |
| Room task commands (`/room task`) | ✅ | Commands exist; DAG storage not yet |

### 3.9 Decider v3 — Worker Context Isolation (NOT YET IMPLEMENTED)

| Feature | Status | Notes |
|---|---|---|
| Per-worker delta filtering in `_build_prompt` | 🔲 | Localized to `_build_prompt`; must not disturb `_derive_member_watermarks` |

---

## 4. E2E Test Plan

### 4.1 Test Categories

| # | Category | What it covers | How to test |
|---|---|---|---|
| T1 | Room lifecycle | Create, list, detail, disband | API: create room, verify in list, check detail, disband |
| T2 | Event replay | Log correctness, monotonicity, pagination | API: read log, validate seq order, test since_seq edge |
| T3 | Decider star scheduling | Round-0 decider-only, mention filter, worker isolation | Integration: send user msg, verify only decider speaks r0, worker `@` doesn't pull other workers |
| T4 | Decider 5-round serial | r0→r1→r2→r3→r4 cycle, crash recovery | Integration: full 5-turn cycle, restart mid-turn, verify reconstruction |
| T5 | F1 tool whitelist | Decider toolset restricted, worker full toolset | Unit: `test_f1_decider_member_is_restricted_to_orchestration_only_tools` |
| T6 | Anti-injection | XML wrapper, escape, provenance framing | Unit: inject malicious `</teammate-message>`, verify escaped |
| T7 | C9 inspector | All 15 API endpoints, 9 UI components, cross-endpoint consistency | API + browser E2E (this session) |
| T8 | Observer | State machine, rules_checked, violations | API: check state, pause/resume cycle |
| T9 | Peer grants + replication | Route statuses, health arithmetic | API: verify ready+unavail+reauth = total, healthy logic |
| T10 | Policy trace | Checkpoint snapshot, through_seq, watermarks | API: verify through_seq = room latest_seq, event_count = len(events) |
| T11 | Cross-endpoint consistency | member_ids, room_id, latest_seq across endpoints | API: compare detail ↔ topology ↔ log ↔ policy-trace |
| T12 | Error handling | Non-existent room, non-existent member, invalid params | API: 404 for missing rooms, graceful defaults for missing data |
| T13 | Frontend rendering | All C9 components, empty states, collapsible interaction | Browser: Playwright + Chrome headless |
| T14 | i18n | EN + ZH for all new keys | Bundle: verify all 22 new keys in production build |

### 4.2 Test Execution Summary (2026-09-05)

| Test | Scope | Passed | Failed | Warnings |
|---|---|---|---|---|
| T7 (C9 API) | 4 rooms × 15 endpoints | 307 | 0 | 0 |
| T13 (C9 browser UI) | 18 checks (components + empty states + interactions) | 18 | 0 | 0 |
| T12 (Error handling) | 4 error cases | 3 | 0 | 1 (observer stub) |
| T8 (Observer) | 4 rooms | All | 0 | 4 (pause/resume stub) |
| T9 (Peer grants + replication) | 4 rooms | All | 0 | 0 |
| T10 (Policy trace) | 4 rooms | All | 0 | 0 |
| T11 (Cross-endpoint consistency) | 4 rooms × 4 cross-checks | All | 0 | 0 |
| T14 (i18n) | 22 keys × 2 languages | 44 | 0 | 0 |

**Known warnings (not regressions):**
| Warning | Detail |
|---|---|
| `observer pause/resume` is stub | Backend returns `{"ok": true}` but doesn't change state |
| Non-existent room observer returns 200 | Returns default zero-state instead of 404 |

---

## 5. Gap Analysis: What's Left

### 5.1 Critical path (blocks product promise)

| Gap | Priority | Effort | Blocks |
|---|---|---|---|
| C2 outbound bridge (room → chat) | P0 | Medium | Decider "single external voice" product promise |
| C2 inbound routing (chat → room) | P1 | Medium | Full-duplex chat↔room |
| Observer pause/resume implementation | P1 | Small | UI buttons are non-functional |
| Non-existent room observer → 404 | P3 | Trivial | Error handling consistency |

### 5.2 Feature expansion

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| C3 Room↔Kanban task DAG | P2 | Large | CC's self-claim algorithm, dependency DAG, sidecar store |
| Decider v3 context isolation | P2 | Medium | Localized to `_build_prompt` |
| C9 observer non-existent room → 404 | P3 | Trivial | 1-line fix |

### 5.3 CC features not adopted (by design)

| CC feature | Reason not adopted |
|---|---|
| Parallel teammate execution | Hermes' serial guard prevents write races; single-member/turn is simpler |
| Split-pane / tmux display | Hermes is server-driven; UI is the Web dashboard |
| `waitForTeammatesToBecomeIdle` barrier | Hermes' serial execution means only one member is ever active — equivalent to implicit quiescence |
| Worktree isolation (`isolation: "worktree"`) | Serial execution + no concurrent writes = no worktrees needed |
| Dynamic workflows (JS script) | Out of scope — Hermes uses decider prompt + turn-based dispatch |
| Subagent definitions as roles | Hermes uses profiles (reusable agent configs) |

---

## 6. E2E Test Commands

### 6.1 API tests (Python)

```bash
# Full Room E2E test (all 15 endpoints, 4 rooms, 12 test categories)
python3 /tmp/test_room_e2e.py

# Quick smoke test (key endpoints only)
curl -s -b cookies.txt http://localhost:9119/api/rooms | jq '.rooms | length'
curl -s -b cookies.txt http://localhost:9119/api/rooms/{id}/log?since_seq=0 | jq '.events | length'
```

### 6.2 Browser UI tests (Playwright)

```bash
# Login + navigate to rooms + verify all C9 components
node /tmp/test_rooms_final.mjs

# Screenshot output
ls /tmp/rooms_final.png
```

### 6.3 Decider integration tests (pytest)

```bash
# F1: Decider tool whitelist
pytest gateway/tests/ -k "test_f1_decider_member_is_restricted_to_orchestration_only_tools"

# v2: 5-round crash recovery
pytest gateway/tests/ -k "test_later_round_task_reconstructs_after_restart or test_five_round_bound"
```

### 6.4 Frontend build verification

```bash
npm run --workspace web build
# Verify bundle contains all new components/keys
grep -c 'peerGrants\|replicationHealth\|policyTrace\|noLinkage' hermes_cli/web_dist/assets/RoomsPage-*.js
```

---

## 7. File Index

| File | Role |
|---|---|
| `gateway/hosted_rooms.py` | Event log storage (append-only, read_events, room_state) |
| `gateway/hosted_room_discussion.py` | Turn logic, member mentions, prompt building, anti-injection |
| `gateway/hosted_room_execution_policy.py` | F1 orchestration-only tool whitelist |
| `gateway/slash_commands.py` | `/room` CLI commands |
| `tui_gateway/hosted_room_service.py` | Room lifecycle, observer, peer grants, replication, policy trace |
| `tui_gateway/server.py` | `_room_decider_toolset_filter` application |
| `hermes_cli/web_server.py` | All `/api/rooms/*` endpoints (15 total) |
| `web/src/pages/RoomsPage.tsx` | C9 inspector UI (9 cards) |
| `web/src/lib/api.ts` | Frontend API client (types + fetch methods) |
| `web/src/i18n/en.ts`, `zh.ts`, `types.ts` | i18n translations (105+ keys) |

---

## 8. Version History

| Version | Date | Changes |
|---|---|---|
| v1 | 2026-09-05 | Initial document: architecture mapping, implementation status, E2E test plan, gap analysis |
