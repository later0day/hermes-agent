# Upstream/Main Merge Assessment

Date: 2026-08-15

Local branch (product work): `merge/upstream-2026-08-13` @ `d4cace6937`

Merge commit: `merge/upstream-2026-08-13` @ `7286a3397e`

Upstream target: `upstream/main` @ `924074906e` (tip 2026-08-15)

## Divergence

- Merge-base: `89a84e1ae6` (the previous merge's upstream tip).
- Upstream ahead by **395** commits (2026-08-13 .. 2026-08-15); local ahead
  by 147.
- Real textual conflicts (4 files):
  - `cron/scheduler.py`
  - `gateway/run.py` (3 separate conflict blocks in the same file)
  - `package.json`
  - `package-lock.json`
- Agent-room surface (`gateway/agent_room_*.py`, `tools/room_decompose_tool.py`)
  merged with ZERO conflicts — confirmed upstream did not touch it at all in
  this window (`git diff 89a84e1ae6..upstream/main -- gateway/agent_room_*`
  is empty).

## Executive Direction

Same shape as the 2026-08-13 and prior merges — NOT "ours wins" / "theirs
wins":

1. Take upstream wholesale for independent official areas (desktop, plugins,
   skills, docs, web frontend, cron self-heal, model metadata/pricing, Honcho
   memory plugin fixes, native Slack task cards, `/loop` recurring wakeups).
2. Preserve local product work: agent-room (M1-M4 + no-match escalation),
   DingTalk `turn_status_card` + MCP reload, dashboard/profile unification,
   cron profile partitioning (workdir/profile sequential dispatch).
3. Hand-compose the 4 real conflicts by keeping BOTH sides' behavior.
4. Semantically review auto-merged runtime-boundary files even though Git
   merged them textually.

## Conflict Resolutions (compose both sides)

- **`cron/scheduler.py`** (1 conflict): kept our sequential-pass docstring +
  partition condition (`workdir OR profile` touches process-global env
  state — not just workdir) AND upstream's new `_running_futures` tracking
  (lets the stale-job sweep distinguish "still executing" from "claim leaked
  before/after the future").

- **`gateway/run.py`** (3 conflicts, same file):
  1. Tool-progress relay gate — composed upstream's new native Slack
     task-card short-circuit (`_native_slack_task_cards`: skip
     name-correlated text events when `chat.startStream` cards are active,
     since those consume the authoritative ID-bearing tool_start/complete
     callbacks instead) with our `turn_status_card` bypass (DingTalk's
     status card still needs real tool lifecycle events even when
     `tool_progress` is off).
  2. Toolset resolution for `_run_agent_inner` — kept our explicit kwargs
     `_toolsets_override` mechanism (agent-room's observer-lockdown call
     sites pass `_toolsets_override=["room_observer"]` directly, injecting
     into `platform_toolsets` + forcing `tool_search: off`) as the
     higher-priority path, falling through to upstream's new adapter-driven
     `_resolve_enabled_toolsets_for_source()` (webhook per-route toolset
     overrides queried from the platform adapter) when no override is given.
     These are independent mechanisms for different call sites, not two
     implementations of the same feature — composed, not chosen-between.
  3. `needs_progress_queue` — composed upstream's `_native_slack_task_cards`
     signal into the OR-chain while keeping our
     `and not _turn_status_card_enabled` outer gate (an active turn status
     card fully replaces the plain progress-queue path regardless of
     platform).

- **`package.json` / `package-lock.json`** (1 conflict each): took
  upstream's side outright. Upstream's 2026-07-15 fix (`5f5f8d5b62`) already
  removed `agent-browser`/`@streamdown/math` from root `dependencies`
  (moved to lazy `npx` resolution / `apps/desktop`'s own `package.json`)
  because `npm ci` reliably prunes root-only deps under workspace-scoped
  installs — our side still carried the pre-fix dependency block from
  before that upstream commit existed. Also dropped a dangling `puppeteer`
  root dependency (added during a 2026-06-20 merge conflict resolution,
  `0aa5bd710c`) that is not imported anywhere in the tree (`apps/`, `web/`,
  `tools/`, `ui-tui/` — zero hits). Regenerated `package-lock.json` via
  `npm install --package-lock-only --workspaces=false` rather than
  hand-editing the lockfile, to get a consistent dependency graph.

## Semantic Review of Auto-Merged Runtime-Boundary Files

Compiled + spot-checked (no conflict markers, but touched by the automerge):
`gateway/run.py`, `gateway/session.py`, `gateway/platforms/base.py`,
`hermes_cli/web_server.py`, `cron/jobs.py`, `tools/cronjob_tools.py`,
`tools/delegate_tool.py`, `tools/terminal_tool.py`, `toolsets.py`,
`agent/tool_executor.py` — all `ast.parse` clean.

- DingTalk `turn_status_card` (38 refs) and MCP reload handlers
  (`_handle_reload_mcp_command`/`_execute_mcp_reload`) intact and unmodified
  in this merge window.
- Docker credential denylist re-verified live on this host:
  `validate_media_delivery_path("/root/.hermes/auth.json")` still returns
  `None`.

## Regression Status

Ran the full suite (`tests/ gateway/ tools/ cron/ hermes_cli/`, ~28,600
tests) twice — once at pre-merge HEAD `d4cace6937` in an isolated worktree,
once on the merged tree — and diffed the per-file failure lists.

- **35 files / 111 tests: IDENTICAL failure set both before and after the
  merge.** Same pre-existing baseline documented in the 2026-08-13 merge
  assessment (environment/tool-chain gaps on this host, upstream test debt
  from the 2026-08-07 `verify` subsystem integration — none are
  merge-introduced or agent-room-related).
- 2 files differed between the two runs
  (`tests/test_tui_gateway_server.py`, `tests/honcho_plugin/test_pin_peer_name.py`):
  confirmed NOT merge-introduced.
  - `test_pin_peer_name.py::TestPinTransition::test_cache_busting_signature_reflects_pin_peer_name`
    fails in isolation even at pre-merge HEAD `d4cace6937` — a
    `mtime_ns`-resolution race in `GatewayRunner`'s Honcho
    cache-busting memoization (two rapid file writes can land on the same
    `mtime_ns`, so the "pinned" and "unpinned" signatures collide). Not
    reproducible from this merge; pre-existing flakiness.
  - `test_tui_gateway_server.py::test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_threading`
    is a process-global `completion_queue` race under concurrent test
    execution (own docstring documents pollers from other tests in the same
    file can legitimately steal-and-requeue foreign-session events) —
    independent of this merge's changes.

**Net: zero merge-introduced regressions.**

## Bottom Line

Merge committed as `7286a3397e` (parents `d4cace6937` + `924074906e`). All 4
conflicts composed to keep both sides; runtime-boundary files reviewed and
local product features (agent-room no-match escalation, DingTalk delivery,
MCP reload, status card, cron profile partitioning) verified intact.

## Follow-Ups

- Pre-existing failures (35 files / 111 tests) are unrelated to this merge,
  identical to the 2026-08-13 baseline, and tracked separately.
- `scratchpad/` (untracked process-verification scripts from the no-match
  escalation work) intentionally left out of this and the prior commit —
  disposition (keep/delete/archive) still pending a decision.
