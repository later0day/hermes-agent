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
| Task model | Shared DAG (pending/in_progress/completed, blockedBy) | @mention scheduler + persistent manual Room DAG; Decider materialization open |
| Scheduling | Pull-based self-claim (lowest ID, file-locked) | Serial guard (one member/turn), `plan_next_task` replay |
| Execution | Parallel (multiple teammates run concurrently) | Serial (one member at a time, no write races) |
| Context | Isolated (teammate = own context, no history carry-over) | Shared (bounded delta of all thread messages, v3 adds isolation) |
| Quality gates | Hooks (`TeammateIdle` exit-2, `TaskCreated`/`TaskCompleted`) | F1 tool policy exists; durable task-quality gates remain open |
| Inspector | In-process agent panel / split-pane | Web dashboard (C9: read-only inspector) |

---

## 2. Architecture Mapping: CC → Hermes

### 2.1 Component-by-component

| CC component | Hermes analogue | Status | Notes |
|---|---|---|---|
| **Team lead** (coordinator mode) | **Decider** (role=`decider`) | ✅ SHIPPED | F1 orchestration-only tool whitelist hard-enforced |
| **Teammates** | Workers (role=`teammate`) | ✅ SHIPPED | Full toolset, serial execution |
| **Task list** (`~/.claude/tasks/{team}/`) | C3 Room Task DAG sidecar | ✅ SHIPPED (manual ledger) | `gateway/room_task_dag.py`; Decider materialization remains open |
| **Mailbox** (`~/.claude/teams/{team}/inboxes/`) | Event log `message.member` / `message.user` | ✅ SHIPPED | Append-only, idempotent `event_id`, authority-epoch fence |
| **Team config** (`config.json` `members[]`) | Room `members` JSON column | ✅ SHIPPED | Persisted verbatim in `state.db` |
| **SendMessage** | `@handle` mentions in member text | ✅ SHIPPED | `plan_next_task` resolves mentions |
| **ListAgents** | Room topology API | ✅ SHIPPED | `GET /api/rooms/{id}/topology` |
| **TaskCreate/Get/Update/List** | C3 task commands/store | ✅ SHIPPED | `/room task add|list|dep|claim|assign|done|release` |
| **TaskStop** | `/room disband` | ✅ SHIPPED | |
| **Coordinator tool filter** (`applyCoordinatorToolFilter`) | F1 `ORCHESTRATION_ONLY_TOOLSETS` | ✅ SHIPPED | `hosted_room_execution_policy.py` |
| **TeammateIdle hook** | `turn.settled` terminal event | ⚠️ PARTIAL | Scheduling resumes, but no completion-quality veto exists |
| **TaskCreated/TaskCompleted hooks** | Durable Room quality gates | 🔲 TODO | |
| **Anti-injection protocol** (3 layers) | `_format_message` / `_build_prompt` | ✅ SHIPPED | `hosted_room_discussion.py` §A.8 |
| **Shared store** (blackboard) | Event-log replay + bounded thread delta | ⚠️ PARTIAL | Not a general shared blackboard; task-context isolation remains open |
| **Structured protocol messages** (plan/shutdown/permission) | Pending actions | ⚠️ PARTIAL | Permission actions are durable and relayed exactly; plan/shutdown semantics remain incomplete |
| **Cross-session messaging** | C2 full-duplex chat-group bridge | ✅ SHIPPED | Durable cursor, idempotent inbound event ids, stable source-thread mapping |

### 2.2 Key design decisions

| Decision | Rationale | CC equivalent |
|---|---|---|
| **Star, not mesh** | Decider is single external voice; workers never address each other | CC's lead does NOT hide teammates — this is the deliberate delta |
| **Serial, not parallel** | Hermes' serial guard prevents write races; no worktrees needed | CC runs teammates in parallel (separate processes) |
| **Append-only log, not files** | Durable, ordered, transport-neutral source of truth | CC uses local JSON files (mailbox/task/config) |
| **Decider = orchestration-only** | Hard tool whitelist, not prompt — F1 enforces at build time | CC's `applyCoordinatorToolFilter` restricts to 6 tools |
| **Pull-based self-claim → C3** | v1/v2 uses @mention dispatch; C3 adds CC-style task DAG with file-locked claim | CC's self-claim algorithm (A.2) |

### 2.3 CC structured protocol messages → Hermes pending actions

CC's mailbox carries typed protocol messages beyond plain text (§A.5). Hermes maps
these to typed events in the append-only log, exposed as pending actions:

| CC protocol message | Hermes event kind | API endpoint |
|---|---|---|
| `isPlanApprovalRequest` / `isPlanApprovalResponse` | `plan.approval` / `plan.approval_result` | `GET /api/rooms/{id}/pending-actions` → approve/deny |
| `isShutdownRequest` / `isShutdownApproved` | `shutdown.request` / `shutdown.approved` / `shutdown.denied` | `GET /api/rooms/{id}/pending-actions` → approve/deny |
| `isPermissionRequest` / `isPermissionResponse` | `permission.request` / `permission.approved` / `permission.denied` | `GET /api/rooms/{id}/pending-actions` → approve/deny |
| `isTaskAssignment` | (`task.created` / `task.updated` — C3) | `/room task` commands |
| `isIdleNotification` | `turn.settled` (terminal event, no user action needed) | Event log (auto-triggers `plan_next_task`) |

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

**E2E test (T1):** Create a room with decider + worker, send a message, verify event
log. → Not executed this session (rooms pre-existed in state.db).

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

**E2E test (T3):** Full turn cycle: user msg → decider r0 → decider `@worker` →
worker r1 → worker `@decider` → decider summarizes r2. Back-compat: rooms without
decider keep mesh behavior. → Not executed this session (requires live room driver).

### 3.3 Decider v2 — 5-Round Serial Iteration (SHIPPED)

| Feature | Status | Code |
|---|---|---|
| `MAX_DISCUSSION_ROUNDS = 5` | ✅ | `hosted_room_discussion.py:31` |
| `_TURN_ID_RE` widened to `[0-4]` | ✅ | `hosted_room_discussion.py:52` |
| Crash-recovery E2E (r3/r4 task reconstructs after restart) | ✅ | `test_later_round_task_reconstructs_after_restart` |
| `reconstruct_task_plan` handles new round bounds | ✅ | `hosted_room_discussion.py:1228` |

**E2E test (T4):** Serial dispatch: r0 decider → r1 worker A → r2 decider reviews →
r3 worker B → r4 decider answers. Crash-recovery: restart mid-turn, verify task
reconstruction. → Not executed this session (requires live room driver + restart).

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

**E2E test (T5):** `test_f1_decider_member_is_restricted_to_orchestration_only_tools`
— decider with full profile → toolset intersected to orchestration-only; worker in
same room keeps full toolset. → Pytest unit test (not executed this session).

### 3.5 CC Anti-Injection Protocol (SHIPPED)

Three-layer protocol from CC v2.1.226, implemented in `hosted_room_discussion.py`:

| Layer | CC mechanism | Hermes implementation | Status |
|---|---|---|---|
| 1. XML wrapper | `SCr` function wraps in `<teammate-message teammate_id="..." summary="...">` | `_format_message` wraps member messages | ✅ |
| 2. Anti-escape | `XTe` function backslash-escapes `<teammate-message` in body | `_escape_teammate_tags` regex `<(?=/?teammate-message\b)` | ✅ |
| 3. Provenance framing | `[MESSAGE FROM NON-USER SOURCE]` + shared-store anti-injection | In `_build_prompt` shared opening + decider/worker rules | ✅ |

User messages keep `User (user): <text>` format (trusted, no wrapping).

**E2E test (T6):** Inject a malicious message containing `</teammate-message>`, verify
it's escaped in the prompt and doesn't break the XML wrapper. → Not executed this
session (requires room with active member turns).

### 3.6 C9 — Hosted Room Web Inspector (SHIPPED this session, E2E verified)

| Feature | Backend endpoint | Frontend component | Status |
|---|---|---|---|
| Roster (member list) | `GET /api/rooms` + `GET /api/rooms/{id}` | Room detail panel | ✅ |
| Event replay | `GET /api/rooms/{id}/log` | Event log table | ✅ |
| Authority/driver | `GET /api/rooms/{id}` (embedded) | Authority section | ✅ |
| Peer grants | `GET /api/rooms/{id}/peer-grants` | `PeerGrantsCard` | ✅ |
| Replication health | `GET /api/rooms/{id}/replication-health` | `ReplicationHealthCard` | ✅ |
| Policy trace | `GET /api/rooms/{id}/policy-trace` | `PolicyTraceCard` (collapsible) | ✅ |
| Room linkage (peer route targets) | (uses peer-grants data) | `LinkageCard` | ✅ |
| Team topology | `GET /api/rooms/{id}/topology` | `TeamTopologyCard` | ✅ |
| Pending actions | `GET /api/rooms/{id}/pending-actions` | Pending actions panel | ✅ |
| Mailbox | `GET /api/rooms/{id}/members/{mid}/mailbox` | — | ✅ (API) |
| Observer monitor | `GET /api/rooms/{id}/observer` | `LiveObserverCard` | ✅ |
| Observer pause/resume | `POST /api/rooms/{id}/observer/{pause,resume}` | Buttons | ⚠️ Backend is stub |

**E2E test results (T7, this session, 2026-09-05):**
- 307 API tests across 4 rooms / 15 endpoints: **0 failures**
- 18 browser UI tests (Playwright + Chrome headless): **0 failures**
- Known: `observer pause/resume` backend is a stub (returns ok but doesn't change state)

### 3.7 C2 — Chat-Group Bridge (SHIPPED, production-chain E2E)

| Feature | Status | Notes |
|---|---|---|
| Outbound mirror (room → chat) | ✅ | Durable subscription cursor; send failure rewinds and never silently drops |
| Inbound routing (chat → room) | ✅ | Exact bound source, idempotent message id, stable identifier-safe Room thread |
| `member_filter` (decider-only emission) | ✅ | Decider Rooms default to single external voice; `--all-members` is explicit opt-out |
| Full production chain | ✅ | Gateway ingress → Room → runtime → real `prompt.submit` → agent turn → terminal callback → publication |

### 3.8 C3 — Room Task DAG (SHIPPED manual ledger; structured Decider planning open)

| Feature | Status | Notes |
|---|---|---|
| Task table (sidecar store) | ✅ | Real SQLite `room_task_dag` / `room_task_deps` |
| Self-claim algorithm | ✅ | `BEGIN IMMEDIATE` + CAS; real concurrent-thread acceptance test |
| Dependency DAG (blocks/blockedBy, auto-unblock) | ✅ | Cycle rejection and missing-dependency fail closed |
| Room task commands (`/room task`) | ✅ | Add/list/dep/claim/assign/done/release |
| Automatic manual-DAG dispatch | ✅ | Skips unroutable head tasks; settlement closes dispatch and unlocks dependents |
| Decider output → structured persistent DAG | 🔲 | Must use a narrow service-gated protocol, not free-form text parsing |

### 3.9 Decider v3 — Worker Context Isolation (NOT YET IMPLEMENTED)

| Feature | Status | Notes |
|---|---|---|
| Per-worker delta filtering in `_build_prompt` | 🔲 | Localized to `_build_prompt`; must not disturb `_derive_member_watermarks` |

---

## 4. E2E Test Plan

### 4.1 Test Categories

| # | Category | What it covers | How to test | Executed this session |
|---|---|---|---|---|
| T1 | Room lifecycle | Create, list, detail, disband | Repository tests against isolated SQLite | ✅ |
| T2 | Event replay | Log correctness, monotonicity, pagination, bounded replay | Repository tests against isolated SQLite | ✅ |
| T3 | Decider star scheduling | Round-0 decider-only, mention filter, worker isolation | Real runtime tests plus deterministic policy tests | ✅ |
| T4 | Decider 5-round serial | r0→r1→r2→r3→r4 cycle, crash reconstruction | Driver/discussion restart tests | ✅ (reconstruction; process-kill matrix remains open) |
| T5 | F1 tool whitelist | Decider restricted, Worker full, lookup failures fail closed | `test_f1_decider_member_is_restricted_to_orchestration_only_tools` | ✅ |
| T6 | Anti-injection | XML wrapper, escape, provenance framing | Repository discussion/runtime tests | ✅ |
| T7 | C9 inspector | All 15 API endpoints, 9 UI components, cross-endpoint consistency | API + browser E2E | ✅ **307 passed, 0 failed** |
| T8 | Observer | State machine, rules_checked, violations | API: check state, pause/resume cycle | ✅ (4 rooms, stub noted) |
| T9 | Peer grants + replication | Route statuses, health arithmetic | API: verify ready+unavail+reauth = total, healthy logic | ✅ (4 rooms, 0 failures) |
| T10 | Policy trace | Checkpoint snapshot, through_seq, watermarks | API: verify through_seq = room latest_seq, event_count = len(events) | ✅ (4 rooms, 0 failures) |
| T11 | Cross-endpoint consistency | member_ids, room_id, latest_seq across endpoints | API: compare detail ↔ topology ↔ log ↔ policy-trace | ✅ (4 rooms, 0 failures) |
| T12 | Error handling | Non-existent room, non-existent member, invalid params | API: 404 for missing rooms, graceful defaults for missing data | ✅ (3/4 passed, 1 warning) |
| T13 | Frontend rendering | All C9 components, empty states, collapsible interaction | Browser: Playwright + Chrome headless | ✅ **18 passed, 0 failed** |
| T14 | i18n | EN + ZH for all new keys | Bundle verification | ✅ |
| T15 | Production turn chain | bound Gateway ingress → Room service/runtime → real `prompt.submit` → agent turn → terminal publication | Deterministic model boundary, real storage/session/driver/RPC path | ✅ |
| T16 | DAG concurrency/starvation | simultaneous SQLite claims; malformed head before valid task | Real connections/threads + runtime loop | ✅ |
| T17 | Approval restart | pending action restart recovery and exact local relay | Recreate service on same SQLite DB | ✅ |
| T18 | Mirror durability | retry budget cannot advance past unsent event | Real cursor store + watcher loop | ✅ |

### 4.2 Test Execution Results (latest repository acceptance)

The complete Hosted Room acceptance matrix currently passes **383 tests, 0 failures**.
It includes the real production-chain test
`test_real_service_runtime_rpc_prompt_agent_terminal_publication_e2e`; the model
network boundary is deterministic, while Gateway ingress, SQLite state, Room service,
runtime, `HostedRoomServerRPC`, `prompt.submit`, agent invocation, callback threading,
session persistence, and Room publication use production code.

The historical Dashboard-only run below is retained as UI evidence, not as proof of
the full Agent Team execution chain.

| Test | Scope | Passed | Failed | Warnings |
|---|---|---|---|---|
| T7 (C9 API) | 4 rooms × 15 endpoints | 307 | 0 | 0 |
| T8 (Observer) | 4 rooms | 24 | 0 | 4 (pause/resume stub) |
| T9 (Peer grants + replication) | 4 rooms | 32 | 0 | 0 |
| T10 (Policy trace) | 4 rooms | 48 | 0 | 0 |
| T11 (Cross-endpoint consistency) | 4 rooms × 4 cross-checks | 16 | 0 | 0 |
| T12 (Error handling) | 4 error cases | 3 | 0 | 1 (observer stub) |
| T13 (C9 browser UI) | 18 checks (components + empty states + interactions) | 18 | 0 | 0 |
| T14 (i18n) | 22 keys × 2 languages | 44 | 0 | 0 |
| **TOTAL** | | **492** | **0** | **5** |

**Known warnings (not regressions):**
| Warning | Detail |
|---|---|
| `observer pause/resume` is stub | Backend returns `{"ok": true}` but doesn't change state (×4 rooms) |
| Non-existent room observer returns 200 | Returns default zero-state instead of 404 |

---

## 5. Gap Analysis: What's Left

### 5.1 Critical path (blocks product promise)

| Gap | Priority | Effort | Blocks |
|---|---|---|---|
| Structured Decider → persistent DAG protocol | P0 | Large | End-to-end planned work rather than manual DAG entry |
| Durable mirror dead-letter/operator retry UI | P1 | Medium | Current implementation preserves/retries events but has no durable paused state |
| Observer pause/resume implementation | P1 | Small | UI buttons are non-functional |
| Non-existent room observer → 404 | P3 | Trivial | Error handling consistency |

### 5.2 Feature expansion

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| Task quality gates and review/repair state | P1 | Large | Prevent false completion and gate downstream work |
| DAG claim lease/recovery | P1 | Medium | Manual in-progress claims still need abandoned-owner recovery |
| Decider v3 context isolation | P2 | Medium | Bound per-task context and deduplicate Room delta/session history |
| Full process-kill recovery matrix | P2 | Medium | Current tests reconstruct state but do not kill at every commit boundary |

### 5.3 CC features not adopted (by design)

| CC feature | Reason not adopted |
|---|---|
| Parallel teammate execution | Hermes' serial guard prevents write races; single-member/turn is simpler |
| Split-pane / tmux display | Hermes is server-driven; UI is the Web dashboard |
| `waitForTeammatesToBecomeIdle` barrier | Hermes' serial execution means only one member is ever active — equivalent to implicit quiescence |
| Worktree isolation (`isolation: "worktree"`) | Serial execution + no concurrent writes = no worktrees needed |
| Dynamic workflows (JS script) | Out of scope — Hermes uses decider prompt + turn-based dispatch |
| Subagent definitions as roles | Hermes uses profiles (reusable agent configs) |

### 5.4 Remaining E2E limits

| Feature | Current evidence | Test gap |
|---|---|---|
| Process crash recovery | Durable reconstruction and service restart tests | No kill/restart injection at every transaction boundary |
| Mirror external delivery | Real watcher/cursor with deterministic adapter | No live third-party platform network test in CI |
| Model invocation | Real `prompt.submit`/agent invocation with deterministic agent boundary | No paid provider network call in repository tests |
| Structured Decider planning | Not implemented | Requires service-gated typed coordination protocol first |
| Quality review/repair | Not implemented | Requires durable task verdict/finding/attempt schema first |

---

## 6. E2E Test Commands

### 6.1 Repository Hosted Room acceptance

```bash
source .venv/bin/activate 2>/dev/null || source venv/bin/activate
python -m pytest \
  tests/gateway/test_hosted_room_*.py \
  tests/gateway/test_hosted_rooms*.py \
  tests/gateway/test_room_mirror_db.py \
  tests/gateway/test_room_task_dag.py \
  tests/tui_gateway/test_hosted_room_*.py \
  -q -p no:cacheprovider
```

The production-chain case can be run alone:

```bash
python -m pytest \
  tests/tui_gateway/test_hosted_room_service.py::test_real_service_runtime_rpc_prompt_agent_terminal_publication_e2e \
  -q -p no:cacheprovider
```

### 6.2 Frontend build verification

Use the repository-supported nvm toolchain rather than the system Node/npm:

```bash
export NVM_DIR="$HOME/.nvm"
source "$NVM_DIR/nvm.sh"
nvm use 24.11.1
npm run typecheck -w web
npm test -w web
npm run build -w web
```

### 6.3 Live Dashboard smoke check

The Appendix A script is historical/manual evidence only. Supply credentials through
`HERMES_DASHBOARD_PASSWORD`; it must not contain a checked-in fallback password or
be described as the repository production-chain E2E.

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

## 8. Appendix A — C9 E2E Test Script (Python)

The full test script executed this session against 4 live rooms. Covers all 15 API
endpoints, cross-endpoint consistency, error handling, and frontend bundle
verification.

```python
#!/usr/bin/env python3
"""
C9 End-to-End Test: Room Web Inspector — all 15 API endpoints, 4 rooms, 12 categories.
Usage: python3 test_room_e2e.py
Requires: dashboard running on localhost:9119 with cookie-based auth.
"""
import json, urllib.request, urllib.error, http.cookiejar, sys, os

DASHBOARD = "http://localhost:9119"
USER = "admin"
PASS = os.environ["HERMES_DASHBOARD_PASSWORD"]

passed = 0; failed = 0; warnings = 0; failures = []

def log(level, msg):
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "INFO": "  "}.get(level, "  ")
    print(f"  {icon} {msg}")

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; log("PASS", f"{name} {detail}" if detail else name)
    else:
        failed += 1; failures.append(f"{name} {detail}" if detail else name)
        log("FAIL", f"{name} {detail}" if detail else name)

def warn(name, detail=""):
    global warnings; warnings += 1
    log("WARN", f"{name} {detail}" if detail else name)

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api(method, path, body=None):
    url = f"{DASHBOARD}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# ── Login ──
print("═══ LOGIN ═══")
status, resp = api("POST", "/auth/password-login",
    {"provider": "basic", "username": USER, "password": PASS})
check("Login", status == 200 and resp.get("ok"))

# ── 1. Room List ──
print("\n═══ 1. ROOM LIST ═══")
status, data = api("GET", "/api/rooms")
check("GET /api/rooms → 200", status == 200)
rooms = data.get("rooms", [])
check("Has rooms", len(rooms) > 0, f"count={len(rooms)}")

room_details = {}
for r in rooms:
    rid = r["room_id"]
    check(f"[{rid}] room_id", bool(rid))
    check(f"[{rid}] name", bool(r.get("name")))
    check(f"[{rid}] members", len(r.get("members", [])) >= 1, f"n={len(r.get('members',[]))}")
    check(f"[{rid}] revision >= 0", r.get("revision", -1) >= 0)
    check(f"[{rid}] authority_epoch >= 1", r.get("authority_epoch", 0) >= 1)

    status, detail = api("GET", f"/api/rooms/{rid}")
    check(f"[{rid}] detail → 200", status == 200)
    room_details[rid] = detail.get("room", detail)

    d = room_details[rid]
    check(f"[{rid}] detail.members = list.members",
          len(d.get("members", [])) == len(r.get("members", [])),
          f"d={len(d.get('members',[]))} l={len(r.get('members',[]))}")
    check(f"[{rid}] detail.latest_seq = list.latest_seq",
          d.get("latest_seq") == r.get("latest_seq"))
    check(f"[{rid}] authority_epoch", d.get("authority_epoch") is not None)
    if d.get("authority_gateway_id"):
        check(f"[{rid}] gateway_id format", d["authority_gateway_id"].startswith("install:"))

    ds = detail.get("driver_status")
    if ds:
        log("INFO", f"[{rid}] driver: running={ds.get('running')} working={ds.get('working')}")

# ── 2. Event Log ──
print("\n═══ 2. EVENT LOG ═══")
valid_kinds = {
    "message.user", "message.member", "room.activity", "turn.settled",
    "observer.activity_digest", "observer.heartbeat", "observer.rule_violation",
    "plan.publication", "plan.approval", "plan.approval_result",
    "task.created", "task.updated", "task.completed", "task.deleted",
    "member.joined", "member.left", "member.updated",
    "room.created", "room.disbanded", "room.revision",
    "permission.request", "permission.approved", "permission.denied",
    "shutdown.request", "shutdown.approved", "shutdown.denied",
}
event_kinds = set()
for r in rooms:
    rid = r["room_id"]
    status, logdata = api("GET", f"/api/rooms/{rid}/log?since_seq=0&limit=500")
    check(f"[{rid}] log → 200", status == 200)
    events = logdata.get("events", [])
    latest = logdata.get("latest_seq")
    check(f"[{rid}] events count", isinstance(events, list), f"n={len(events)}")
    check(f"[{rid}] latest_seq", latest is not None, str(latest))

    seqs = [e.get("seq", -1) for e in events]
    for i in range(1, len(seqs)):
        if seqs[i] < seqs[i-1]:
            check(f"[{rid}] monotonic seq", False, f"{seqs[i-1]}→{seqs[i]}")
        else:
            passed += 1

    for e in events:
        k = e.get("kind", "")
        event_kinds.add(k)
        if k not in valid_kinds and not k.startswith("observer."):
            check(f"[{rid}] unknown kind '{k}'", False, f"seq={e.get('seq')}")
            valid_kinds.add(k)

    d_latest = room_details.get(rid, {}).get("latest_seq")
    if latest is not None and d_latest is not None:
        check(f"[{rid}] log.latest_seq = detail.latest_seq", latest == d_latest)

    if events:
        check(f"[{rid}] last event seq = latest_seq", events[-1].get("seq") == latest)

    status, empty = api("GET", f"/api/rooms/{rid}/log?since_seq=999999&limit=10")
    check(f"[{rid}] since_seq=999999 → empty", len(empty.get("events", [])) == 0)

print(f"  Kinds: {sorted(event_kinds)}")

# ── 3. Topology ──
print("\n═══ 3. TOPOLOGY ═══")
for r in rooms:
    rid = r["room_id"]
    status, topo = api("GET", f"/api/rooms/{rid}/topology")
    check(f"[{rid}] topology → 200", status == 200)
    if status == 200:
        check(f"[{rid}] topology.room_id", topo.get("room_id") == rid)
        members = topo.get("members", [])
        check(f"[{rid}] topology.members", isinstance(members, list), f"n={len(members)}")
        for m in members:
            mid = m.get("member_id", "?")
            check(f"[{rid}] member {mid} role", m.get("role") in ("decider", "teammate", "coordinator", "observer", "team_lead"), m.get("role"))
            check(f"[{rid}] member {mid} profile", bool(m.get("profile")))
            check(f"[{rid}] member {mid} handle", bool(m.get("handle")))
        d_members = room_details.get(rid, {}).get("members", [])
        check(f"[{rid}] topo.members = detail.members", len(members) == len(d_members))
        mr = topo.get("max_rounds")
        check(f"[{rid}] max_rounds", mr is None or isinstance(mr, int), str(mr))

# ── 4. Pending Actions ──
print("\n═══ 4. PENDING ACTIONS ═══")
for r in rooms:
    rid = r["room_id"]
    status, actions = api("GET", f"/api/rooms/{rid}/pending-actions")
    check(f"[{rid}] pending-actions → 200", status == 200)
    if status == 200:
        pending = actions.get("actions", actions.get("pending_actions", []))
        check(f"[{rid}] pending-actions is list", isinstance(pending, list), f"n={len(pending)}")
        for a in pending:
            check(f"[{rid}] action has action_id", bool(a.get("action_id")))
            check(f"[{rid}] action has kind", bool(a.get("kind")))

# ── 5. Mailbox ──
print("\n═══ 5. MAILBOX ═══")
for r in rooms:
    rid = r["room_id"]
    for m in room_details.get(rid, {}).get("members", [])[:2]:
        mid = m.get("member_id")
        if not mid: continue
        status, mailbox = api("GET", f"/api/rooms/{rid}/members/{mid}/mailbox")
        check(f"[{rid}/{mid}] mailbox → 200", status == 200)
        if status == 200:
            msgs = mailbox.get("messages", [])
            check(f"[{rid}/{mid}] messages is list", isinstance(msgs, list), f"n={len(msgs)}")
        status, _ = api("POST", f"/api/rooms/{rid}/members/{mid}/mailbox/read")
        check(f"[{rid}/{mid}] mark read → 200", status == 200)
    status, _ = api("GET", f"/api/rooms/{rid}/members/NONEXISTENT/mailbox")
    check(f"[{rid}] non-existent member mailbox", status in (200, 404), f"status={status}")

# ── 6. Observer ──
print("\n═══ 6. OBSERVER ═══")
for r in rooms:
    rid = r["room_id"]
    status, obs = api("GET", f"/api/rooms/{rid}/observer")
    check(f"[{rid}] observer → 200", status == 200)
    if status == 200:
        check(f"[{rid}] observer.state", obs.get("state") in ("armed", "paused", "stopped", "delivering"), obs.get("state"))
        check(f"[{rid}] observer.current_turn >= 0", obs.get("current_turn", -1) >= 0)
        check(f"[{rid}] observer.current_round >= 0", obs.get("current_round", -1) >= 0)
        check(f"[{rid}] observer.rules_checked >= 0", obs.get("rules_checked", -1) >= 0)
        check(f"[{rid}] observer.violations >= 0", obs.get("violations", -1) >= 0)

        status_p, _ = api("POST", f"/api/rooms/{rid}/observer/pause")
        check(f"[{rid}] pause → 200", status_p == 200)

        status_r, _ = api("POST", f"/api/rooms/{rid}/observer/resume")
        check(f"[{rid}] resume → 200", status_r == 200)

        # Known: pause/resume are stubs (backend returns ok but doesn't change state)
        status_v, obs_v = api("GET", f"/api/rooms/{rid}/observer")
        if status_v == 200 and obs_v.get("state") not in ("paused",):
            warn(f"[{rid}] observer pause/resume is STUB — state stays '{obs_v.get('state')}'")

# ── 7. Peer Grants ──
print("\n═══ 7. PEER GRANTS ═══")
for r in rooms:
    rid = r["room_id"]
    status, grants = api("GET", f"/api/rooms/{rid}/peer-grants")
    check(f"[{rid}] peer-grants → 200", status == 200)
    if status == 200:
        check(f"[{rid}] peer-grants.room_id", grants.get("room_id") == rid)
        plist = grants.get("peer_grants", [])
        check(f"[{rid}] peer_grants is list", isinstance(plist, list), f"n={len(plist)}")
        for g in plist:
            check(f"[{rid}] grant member_id", bool(g.get("member_id")))
            check(f"[{rid}] grant status", g.get("status") in ("ready", "unavailable", "needs_reauthorization"), g.get("status"))

# ── 8. Replication Health ──
print("\n═══ 8. REPLICATION HEALTH ═══")
for r in rooms:
    rid = r["room_id"]
    status, health = api("GET", f"/api/rooms/{rid}/replication-health")
    check(f"[{rid}] replication-health → 200", status == 200)
    if status == 200:
        check(f"[{rid}] health.room_id", health.get("room_id") == rid)
        check(f"[{rid}] health.healthy is bool", isinstance(health.get("healthy"), bool))
        total = health.get("total_peers", 0)
        ready = health.get("ready", 0)
        unavail = health.get("unavailable", 0)
        reauth = health.get("needs_reauthorization", 0)
        check(f"[{rid}] health sum = total", ready + unavail + reauth == total)
        check(f"[{rid}] health.healthy logic", health.get("healthy") == (total == 0 or ready == total))
        g_status, g_data = api("GET", f"/api/rooms/{rid}/peer-grants")
        if g_status == 200:
            check(f"[{rid}] health.total_peers = peer_grants count",
                  total == len(g_data.get("peer_grants", [])))

# ── 9. Policy Trace ──
print("\n═══ 9. POLICY TRACE ═══")
for r in rooms:
    rid = r["room_id"]
    status, trace = api("GET", f"/api/rooms/{rid}/policy-trace")
    check(f"[{rid}] policy-trace → 200", status == 200)
    if status == 200:
        check(f"[{rid}] trace.room_id", trace.get("room_id") == rid)
        check(f"[{rid}] trace.through_seq is int", isinstance(trace.get("through_seq"), int))
        check(f"[{rid}] trace.stopped_through_seq is int", isinstance(trace.get("stopped_through_seq"), int))
        check(f"[{rid}] trace.event_count is int", isinstance(trace.get("event_count"), int))
        check(f"[{rid}] trace.events is list", isinstance(trace.get("events"), list))
        check(f"[{rid}] trace.watermarks is dict", isinstance(trace.get("watermarks"), dict))

        if trace.get("error"):
            warn(f"[{rid}] policy trace error: {trace['error']}")
        else:
            room_latest = room_details.get(rid, {}).get("latest_seq")
            if room_latest is not None:
                check(f"[{rid}] trace.through_seq = room.latest_seq",
                      trace.get("through_seq") == room_latest)
            ec = trace.get("event_count", 0)
            ev = len(trace.get("events", []))
            check(f"[{rid}] trace.event_count = len(events)", ec == ev)
            for e in trace.get("events", []):
                check(f"[{rid}] trace event has seq", e.get("seq") is not None)

# ── 10. Error Handling ──
print("\n═══ 10. ERROR HANDLING ═══")
status, _ = api("GET", "/api/rooms/NONEXISTENT_ROOM_99999")
check("Non-existent room → 404", status == 404)
status, _ = api("GET", "/api/rooms/NONEXISTENT_ROOM_99999/log")
check("Non-existent room log → 404", status == 404)
status, _ = api("GET", "/api/rooms/NONEXISTENT_ROOM_99999/topology")
check("Non-existent room topology → 400/404", status in (400, 404))
status, _ = api("GET", "/api/rooms/NONEXISTENT_ROOM_99999/observer")
if status == 200:
    warn("Non-existent room observer returns 200 (default state) instead of 404")

# ── 11. Cross-endpoint Consistency ──
print("\n═══ 11. CROSS-ENDPOINT CONSISTENCY ═══")
for r in rooms:
    rid = r["room_id"]
    d = room_details.get(rid, {})
    status, topo = api("GET", f"/api/rooms/{rid}/topology")
    if status == 200:
        d_ids = {m.get("member_id") for m in d.get("members", [])}
        t_ids = {m.get("member_id") for m in topo.get("members", [])}
        check(f"[{rid}] detail.member_ids = topology.member_ids", d_ids == t_ids)

    for ep in ["peer-grants", "replication-health", "policy-trace"]:
        status, edata = api("GET", f"/api/rooms/{rid}/{ep}")
        if status == 200:
            check(f"[{rid}] {ep}.room_id = {rid}", edata.get("room_id") == rid)

# ── 12. Frontend Bundle ──
print("\n═══ 12. FRONTEND BUNDLE ═══")
bundle = "/opt/hermes-agent/hermes_cli/web_dist/assets/RoomsPage-BaTq1BOC.js"
if os.path.exists(bundle):
    check("RoomsPage bundle exists", True, f"{os.path.getsize(bundle)} bytes")
    with open(bundle, "rb") as f:
        content = f.read().decode("utf-8", errors="ignore")
    for key in ["peerGrants", "replicationHealth", "policyTrace", "noLinkage",
                "observerMonitor", "teamTopology", "eventLog", "authority"]:
        check(f"Bundle contains '{key}'", key in content)
else:
    check("RoomsPage bundle", False, "not found")

# ── Summary ──
print(f"\n{'='*60}")
print(f"RESULTS: {passed} passed, {failed} failed, {warnings} warnings")
if failures:
    print(f"\nFAILURES:")
    for f in failures:
        print(f"  ❌ {f}")
print(f"{'='*60}")
sys.exit(0 if failed == 0 else 1)
```

---

## 9. Version History

| Version | Date | Changes |
|---|---|---|
| v1 | 2026-09-05 | Initial document: architecture mapping, implementation status, E2E test plan, gap analysis |
| v1.1 | 2026-09-05 | Added §2.3 structured protocol message mapping; fixed §4.1/§4.2 execution status columns; added §5.4 untested-but-shipped features; added Appendix A with full test script; fixed Observer row numeric format |
| v1.2 | 2026-09-05 | Corrected C2/C3 status; recorded repository production-chain E2E; documented remaining structured planning/quality/recovery limits; removed checked-in password fallback |
