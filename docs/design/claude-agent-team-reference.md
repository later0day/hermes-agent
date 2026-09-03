# Claude Code Agent Teams — Complete Architecture & Communication Reference

Status: research reference (source-mined, not a Hermes design)
Sources (all read this session):
- `@anthropic-ai/claude-code@2.1.226` `sdk-tools.d.ts` (Agent/Task tool types)
- `docs.claude.com/en/docs/claude-code/{agent-teams,sub-agents,agents,agent-view,
  cross-session-messaging,workflows,worktrees,hooks}.md`

Purpose: the definitive, source-verified picture of CC's multi-agent design, so
Hermes' decider (star) / C2 (bridge) / C3 (kanban) decisions rest on facts, not
on the earlier "isolated fire-and-forget star" mischaracterization.

## 1. The three surfaces (one spectrum, not one design)

| Surface | Topology | Coordinator | Worker↔worker | Where results live | Enable |
|---|---|---|---|---|---|
| **Subagents** | near-star | main agent delegates + collects | named subagents can `SendMessage` | caller's context (summary) | always on |
| **Agent teams** | **mesh + shared task DAG** | `team-lead` assigns/synthesizes | ✅ direct via mailbox | own context + shared task list | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |
| **Dynamic workflows** | script-driven star | a JS script (not the model) | via the script | script variables | paid plans / `/config` |

Key: **teams are a MESH** (teammates message each other directly), not a star.
A true supervisor-star is LangGraph, not CC. CC's lead does NOT hide teammates —
the user can arrow-select and directly message any teammate. This is the exact
delta for Hermes' decider (single external voice).

## 2. Agent teams — architecture

| Component | Role | On-disk location |
|---|---|---|
| **Team lead** | main session; spawns teammates, coordinates, synthesizes | — (the session itself) |
| **Teammates** | separate full CC instances, own context window | — |
| **Task list** | shared work items, claim/complete, dependency DAG | `~/.claude/tasks/{team-name}/` (persists across resume) |
| **Mailbox** | per-agent JSON inbox for direct messages | `~/.claude/teams/{team-name}/inboxes/{agent-name}.json` |
| **Team config** | members[] (name + agent type; lead's type = `team-lead`), session IDs, tmux pane IDs | `~/.claude/teams/{team-name}/config.json` (removed on session end) |

- team-name = `session-` + first 8 chars of session ID.
- Task list is **never uploaded**; retained per `cleanupPeriodDays`.
- Teammates read `config.json` `members[]` to discover peers.
- No project-level team config (`.claude/teams/teams.json` is treated as an
  ordinary file, not config).

## 3. Communication protocol (the "how they talk" answer)

### 3.1 Transport: mailbox files
- Each agent has `inboxes/{agent-name}.json`. A send = a validated append to the
  recipient's mailbox file. **"Sent" is reported only when the file write
  succeeds** (plain text OR structured protocol messages like plan-approval /
  shutdown). Write failure (disk full, non-writable dir) = sender gets an error,
  nothing is sent.
- On read, CC validates every entry; malformed entries are reported as errors and
  removed, valid ones still delivered. (Before v2.1.207 one bad entry blocked the
  whole mailbox.)

### 3.2 Message tools (runtime, not in the SDK d.ts)
- `SendMessage({to: <name>, ...})` — deliver text to one agent by name. To reach
  everyone: one send per recipient (no broadcast). Same tool reaches subagents,
  teammates, and cross-session peers.
- `ListAgents` — discover which agents are reachable.
- The lead names every teammate on spawn (`AgentInput.name`); any teammate can
  message any other by that name. Give predictable names in the spawn prompt.

### 3.3 Delivery semantics
- **Automatic delivery**: messages are pushed to recipients; the lead never polls.
- **Mid-turn vs idle**: the receiver reads a message between tool calls during an
  active turn (never interrupts a running tool); if idle, the message starts a
  new turn.
- **Idle notification**: when a teammate finishes and stops, it auto-notifies the
  lead with its final answer. On API error it notifies the lead with the error
  text.
- **Trust boundary (auto mode)**: a message from another agent is untrusted input
  — a relayed approval claim is NOT treated as your confirmation; a denied action
  can't be relayed through a peer to bypass the check. Every message (plain or
  structured) is classifier-reviewed before delivery; a blocked one never arrives.

### 3.4 Structured protocol messages (beyond plain text)
- **Plan approval**: a teammate spawned while the lead is in plan mode works
  read-only until its plan is ready, then sends a plan-approval request; the lead
  session auto-approves it (the designed exception to "approve in lead session").
- **Shutdown request**: "ask the X teammate to shut down" → lead sends a shutdown
  request; teammate approves (exits gracefully) or rejects with explanation.

## 4. Shared task list = a dependency DAG (C3 reference)

Tool I/O from `sdk-tools.d.ts`:
- `TaskCreateOutput`: `{task:{id, subject}}`
- `TaskGetOutput`: `{task:{id, subject, description, status:
  "pending"|"in_progress"|"completed", blocks[], blockedBy[]} | null}`
- `TaskUpdateOutput`: `{success, taskId, updatedFields[], error?,
  statusChange?:{from,to}}`
- `TaskListOutput`: `{tasks:[{id, subject, status, owner?, blockedBy[]}]}`

Behavior:
- Three states: pending / in_progress / completed (TaskUpdate also has "deleted").
- **Dependencies**: a pending task with unresolved `blockedBy` cannot be claimed;
  completing a task auto-unblocks its dependents (no manual action).
- **Claiming**: lead assigns, or a teammate self-claims the next unassigned,
  unblocked task. Claim uses **file locking** against races.
- Only agents that have the Task tools use the list; others coordinate by messages.

## 5. Context model
- Each teammate = own context window, fully independent. On spawn it loads the
  same project context as a normal session (CLAUDE.md, MCP servers, skills) + the
  lead's spawn prompt. **The lead's conversation history does NOT carry over** —
  so the spawn prompt must be self-contained.
- Subagent definitions are reusable as teammate roles: `tools` (in-process adds
  SendMessage + Task tools), `model`, body (in-process: appended to default
  prompt; split-pane: replaces it), `skills` (NOT applied to teammates),
  `mcpServers` (split-pane only).

## 6. Model, permissions, display
- Teammate model resolution order (v2.1.251+): spawn-prompt name → subagent def
  `model` → `CLAUDE_CODE_SUBAGENT_MODEL` (≠inherit) → lead's model; allowlist
  substitution applies; `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` overrides.
- Teammates inherit the lead's permission mode and effort level; per-teammate mode
  can only be changed after spawn, not at spawn time. Teammate permission prompts
  appear in the lead session.
- Display: in-process (agent panel, arrow+Enter to view/message; `x` to stop;
  Ctrl+T task list) or split-pane (tmux / iTerm2 `it2`). Default `"in-process"`.

## 7. Quality gates via hooks (Hermes risk-D reference)
Fire on every occurrence (no matchers):
- **`TeammateIdle`** — payload `{teammate_name, team_name(deprecated)}`.
  Exit 2 → teammate gets stderr as feedback and keeps working instead of idling.
  `{"continue":false,"stopReason":...}` → stop the teammate entirely.
- **`TaskCreated`** — payload `{task_id, task_subject, task_description?,
  teammate_name?, team_name?}`. Exit 2 or `decision:"block"` → deletes the task,
  returns message to Claude. `continue:false` ignored.
- **`TaskCompleted`** — same payload. Exit 2 → not marked complete, stderr fed
  back. `continue:false` stops teammate only when a turn-finish (not TaskUpdate)
  triggered it.
- Also `SubagentStart`/`SubagentStop` in the nested agentic loop.

## 8. What CC does NOT provide (the Hermes-decider gap)
1. **No single external voice.** CC's lead is a coordinator *for the user*; it
   does not proxy/hide teammates from a downstream chat surface. Hermes' decider
   (expose only the decider to the chat group) has no CC equivalent — it maps to
   the C2 mirror's `member_filter` column, not to anything in CC.
2. **Mesh, not enforced star.** Teammates talk directly; there is no built-in
   "workers may only address the coordinator" constraint. Hermes' role-aware
   `_unaddressed_member_mentions` filter is the mechanism CC lacks.
3. **No transport-neutral log as source of truth.** CC state = local files
   (mailbox/task/config); Hermes rooms = append-only event log with authority-epoch
   fencing. CC's Task DAG is a data-model reference for C3, not a transport model.

## 9. Direct mapping to Hermes work
| CC concept | Hermes analogue | Design doc |
|---|---|---|
| team-lead coordinates | decider role (but ALSO hides workers) | hosted-room-decider.md |
| mailbox / SendMessage(to:name) | @handle mentions over the event log | hosted-room-decider.md (mention filter) |
| shared task DAG (blocks/blockedBy) | C3 Room↔Kanban glue | (C3, TBD) |
| TeammateIdle/TaskCompleted exit-2 gate | decider "never (pass)" enforcement (risk D) | hosted-room-decider.md |
| lead does NOT hide teammates | C2 mirror member_filter = single voice | hosted-room-chat-bridge.md |
| spawn prompt self-contained (no history) | _build_prompt bounded delta (shared) / v3 isolation | hosted-room-decider.md B1 |
