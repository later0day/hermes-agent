# Upstream/Main Merge Assessment

Date: 2026-06-20

Local branch: `main`

Upstream target: `origin/main`

Current divergence:

- Local `main` is ahead of `origin/main` by 14 commits.
- Local `main` is behind `origin/main` by 53 commits.
- `git merge-tree --write-tree main origin/main` reports one textual conflict:
  `cron/scheduler.py`.
- Files changed on both sides since the merge base:
  - `cron/scheduler.py`
  - `gateway/run.py`
  - `hermes_cli/config.py`
  - `hermes_cli/web_server.py`
  - `tests/hermes_cli/test_web_server.py`
  - `tools/delegate_tool.py`
- Current untracked files that should not be accidentally committed:
  - `e2e_verify.js`
  - `extracted_strings.json`
  - `translated_zh.json`

## Executive Direction

Do not treat this as "ours wins" or "theirs wins".

The right merge shape is:

1. Take upstream `origin/main` wholesale for independent official feature
   areas: desktop, MCP, Signal/Teams/Telegram, image shrink recovery, title
   generation, release/version bumps, optional-skill expansion.
2. Reapply/preserve local product work where it adds profile/DingTalk/dashboard
   behavior not present upstream.
3. Hand-resolve the one true conflict in `cron/scheduler.py` by composing both
   sides: upstream delivery-error diagnostics and env sanitization plus local
   profile-aware/DingTalk-origin delivery.
4. Semantically review the auto-merged gateway/dashboard files; Git can merge
   them textually, but they sit on runtime boundaries.

## Official Updates To Preserve

### Desktop App

Official changes:

- Floating draggable composer with dock/undock behavior.
- Composer polish: shared panel styling, chip deletion, model-picker scroll
  sizing, focus styling.
- Remote-display GPU-disable notification.
- Desktop slash command dispatch fixes.
- Managed Node resolution improvements on Windows.

Local overlap:

- No meaningful local desktop app changes in this branch.

Assessment:

- Preserve official implementation as-is.
- Risk is low because local changes are primarily dashboard/gateway, not
  `apps/desktop`.

Validation:

- Desktop TypeScript tests/build if dependencies are present.
- At minimum, include any desktop slash tests touched by upstream.

### MCP / Tool Discovery / Elicitation

Official changes:

- MCP late-connecting tools can refresh between turns without mutating an
  in-flight prompt cache.
- `/reload-mcp` now uses `refresh_agent_mcp_tools`.
- MCP elicitation handler with gateway-aware approval routing.
- Config adds `mcp_discovery_timeout`.
- Tests cover capability gating, refresh, elicitation.

Local overlap:

- Local gateway has approval, tool-progress, and source/session customizations.
- Local `tools/delegate_tool.py` relays subagent tool lifecycle metadata for
  DingTalk turn-status cards.

Assessment:

- Preserve all upstream MCP changes.
- Local `delegate_tool` metadata relay is compatible with upstream's
  documentation-only subagent model bullet. Both should stay.
- Recheck gateway approval routing after merge because both MCP elicitation and
  DingTalk approval/status UX touch gateway callbacks.

Validation:

- `tests/tools/test_mcp_capability_gating.py`
- `tests/tools/test_mcp_elicitation.py`
- `tests/tools/test_refresh_agent_mcp_tools.py`
- Local DingTalk/status-card tests.

### Gateway Core

Official changes:

- Prevent gateway restart loops by stripping dangling assistant tool-call tails
  after a process kill/restart.
- Improve resume prompt text so old restart/shutdown commands are not
  re-executed.
- Gateway `/reload-mcp` preserves each cached agent's toolset selection.
- Telegram topic rename from `/title`.
- Session/source improvements used by dashboard sidecar sessions.

Local changes:

- DingTalk single live status card per turn (`TurnStatusCardCoordinator`).
- Stage-aware DingTalk text-emotion lifecycle.
- Per-session pending reply outcome state for final DingTalk reactions.
- Source-agent binding, profile runtime, agent audit, kanban delegate support.
- Profile-safe source-tree import preference.
- Tool progress metadata propagation for status cards and subagents.

Conflict/risk:

- `gateway/run.py` auto-merges textually, but this is high-risk because both
  sides alter session lifecycle and tool metadata behavior.
- Need verify three behaviors together:
  1. restart-loop stripping still happens before resume,
  2. MCP reload still refreshes cached agents correctly,
  3. DingTalk status card still receives top-level and subagent tool events.

Strategy:

- Keep upstream restart-loop and MCP reload fixes.
- Keep local DingTalk status-card wiring and progress-callback metadata marker.
- Do not add special-case behavior for individual commands; keep lifecycle
  rules generic.

Validation:

- `tests/gateway/test_restart_resume_pending.py`
- `tests/gateway/test_turn_status_card.py`
- `tests/gateway/test_run_progress_topics.py`
- `tests/gateway/test_run_progress_interrupt.py`
- `tests/gateway/test_run_cleanup_progress.py`
- `tests/tools/test_mcp_elicitation.py`
- py-compile `gateway/run.py`.

### DingTalk Platform

Official changes:

- No major DingTalk-specific upstream changes in this incoming range.

Local changes:

- Expanded DingTalk adapter: Stream mode message parsing, media download
  handling, AI Card lifecycle, card content key config, IPv4/media upload
  behavior, robot code precedence, group/DM routing, allow-all user config,
  AI Card image/media paths, status-card capability, final reactions.
- DingTalk config defaults added to `hermes_cli/config.py`.
- Dashboard channel/profile views expose DingTalk state/config.
- Docs capture runtime incidents and analysis.

Conflict/risk:

- Low textual conflict with upstream because upstream did not heavily touch
  DingTalk.
- Higher integration risk through gateway/core changes.

Strategy:

- Preserve local DingTalk adapter and tests.
- Merge upstream gateway fixes around it.
- After merge, runtime-test an actual DingTalk message with:
  - plain answer,
  - tool call,
  - subagent tool call,
  - image/media send,
  - approval flow if configured.

Validation:

- `tests/gateway/test_dingtalk.py`
- `tests/gateway/test_turn_status_card.py`
- live gateway smoke test under profile `xcx`.

### Cron / Scheduler

Official changes:

- Live-adapter delivery now records detailed target errors.
- Standalone fallback includes accumulated live-adapter errors.
- Cron job script subprocesses now use `_sanitize_subprocess_env` to avoid
  leaking Hermes/provider credentials.
- Security docs mention cron job script credential scoping.

Local changes:

- Per-job profile runtime context: `run_profile/profile/owner_profile`.
- Profile jobs run sequentially because profile context mutates runtime state.
- DingTalk origin delivery via captured `session_webhook`.
- DingTalk home target validation.
- Origin delivery preserves `chat_type`.
- Fallback delivery passes metadata and can route DingTalk group/DM context.

Conflict/risk:

- This is the only actual textual conflict.
- The conflict is real because both sides edit `_deliver_result()` and related
  cron execution/delivery flow.

Strategy:

- Compose both sides:
  - Keep upstream `target_errors` accumulation and sanitized script env.
  - Keep local DingTalk session webhook resolution and metadata routing.
  - On live adapter failure, preserve upstream target error messages before
    local DingTalk webhook or standalone fallback.
  - In `_run_job_script()`, keep upstream sanitized env unconditionally.
  - In `tick()`, keep local inline sequential execution for workdir/profile
    jobs unless upstream changes introduce a safer isolation mechanism.

Validation:

- `tests/cron/test_cron_script.py`
- local `tests/cron/test_cron_profile.py`
- DingTalk cron delivery smoke test if credentials/profile are available.

### Dashboard / Web Server

Official changes:

- Dashboard hides sidecar sessions from history.
- Chat sidebar adds reasoning-effort picker.
- Model assignment clears stale custom endpoint credentials.
- Config helpers clear stale `api_key/api/api_mode`.

Local changes:

- Profile dashboard and profile-scoped management endpoints.
- Profile memory/SOUL/skill editor endpoints.
- Source-agent binding endpoints and summaries.
- Profile logs endpoint support.
- DingTalk/channel config/state visibility.
- Action log replacement for gateway start/restart operations.
- Large i18n restructuring and Chinese translations.

Conflict/risk:

- `hermes_cli/web_server.py` auto-merges but is semantically high-risk because
  local changes add a large profile-management surface while upstream adds
  model/reasoning/session behavior.
- `tests/hermes_cli/test_web_server.py` auto-merges but must be run because
  both sides add/modify assertions around model assignment and local profile
  endpoints.

Strategy:

- Preserve upstream reasoning picker API expectations and stale credential
  clearing.
- Preserve local profile endpoints and DingTalk/channel profile display.
- Ensure `_apply_main_model_assignment` uses upstream credential clearing while
  not regressing local profile-scoped config saves.
- Keep upstream sidecar-session filtering.

Validation:

- `tests/hermes_cli/test_web_server.py`
- `tests/hermes_cli/test_web_server_profile_dashboard.py`
- `tests/hermes_cli/test_dashboard_admin_endpoints.py`
- Dashboard smoke:
  - `/api/status?profile=xcx`
  - `/api/config/schema`
  - profile detail endpoints
  - model info/reasoning picker path.

### Web Frontend / i18n

Official changes:

- Reasoning picker UI in `ChatSidebar`.
- Supporting `web/src/lib/reasoning-effort.ts` and tests.

Local changes:

- Large custom i18n restructuring across language files.
- `configLabels.ts`.
- ProfilesPage port: memory/skill editors, binding summary.
- Dashboard channel/config/profile UX updates.
- Local themes and custom entrypoint restoration.

Conflict/risk:

- No direct textual conflict in merge-tree, but many files differ between local
  main and origin/main.
- Upstream `ChatSidebar` reasoning picker must be preserved inside local
  dashboard layout.
- i18n files are large and previously conflict-prone; run TypeScript checks.

Strategy:

- Accept upstream `ReasoningPicker` component and helper tests.
- Keep local i18n architecture and add any new upstream keys into local
  translation/types structure instead of replacing the local i18n files.
- Confirm no `configLabels.ts` deletion.

Validation:

- `npm`/web typecheck if dependencies are installed.
- At minimum, run targeted vitest for `reasoning-effort` and build/typecheck
  for `web` if available.

### Messaging Platforms

Official changes:

- Signal delivery failure logging and live adapter improvements.
- Teams native send_video/send_voice/send_document.
- Telegram opt-in online/offline status indicator.
- Discord reply channel context hydration.
- Raft check_fn no longer spams warnings when raft CLI is missing.

Local changes:

- Platform plugin YAML descriptions translated to Chinese.
- DingTalk-specific runtime/config work.

Conflict/risk:

- Preserve official platform fixes.
- Chinese plugin metadata should be re-applied where upstream changed plugin
  YAML or adapter behavior.
- `origin/salvage/signal-trio` is a newly fetched branch not yet in
  `origin/main`; it adds Signal AAC/markdown formatting changes. Treat it as a
  separate optional follow-up, not part of the main merge unless explicitly
  targeted.

Validation:

- `tests/gateway/test_signal.py`
- `tests/gateway/test_teams.py`
- `tests/gateway/test_telegram_status_indicator.py`
- plugin raft test.

### Agent Core / Models / Images / Titles

Official changes:

- Language-aware title generation.
- Pixel-correct image shrink recovery.
- Memory shutdown hook warning logs.
- CLI active agent reference so memory `on_session_end` fires.
- Model picker clears stale custom endpoint credentials.

Local changes:

- Agent/tool executor metadata support for DingTalk status cards.
- Local docs around compression lineage and gateway lifecycle.

Conflict/risk:

- Low textual conflict, but agent/tool metadata tests should be kept.
- Preserve upstream image/title fixes exactly; they are broad core correctness
  fixes.

Validation:

- `tests/agent/test_title_generator.py`
- `tests/run_agent/test_image_shrink_recovery.py`
- `tests/agent/test_subagent_progress.py`
- local status-card tests.

### Tools

Official changes:

- MCP tool refresh/elicitation.
- Approval-tool additions.
- Image generation i2i tests.
- File tool/session-source and terminal/session env tests.

Local changes:

- Cross-profile/file/terminal/code-execution safety work.
- DingTalk/subagent tool progress metadata.
- Send-message/media behavior work.

Conflict/risk:

- `tools/delegate_tool.py` auto-merges and should preserve both:
  - upstream description bullet about subagent model inheritance,
  - local subagent tool completed/start metadata relay.
- `tools/send_message_tool.py` differs heavily in local vs upstream comparison;
  merge-tree does not report a conflict from the merge base, but run its tests
  because messaging/media behavior has been active locally.

Validation:

- `tests/tools/test_mcp_elicitation.py`
- `tests/tools/test_refresh_agent_mcp_tools.py`
- `tests/tools/test_file_tools.py`
- `tests/tools/test_terminal_task_cwd.py`
- `tests/tools/test_code_execution_modes.py`
- `tests/tools/test_send_message_tool.py` if present after merge.

## Recommended Merge Order

1. Save or ignore untracked scratch files explicitly.

   Do not let `e2e_verify.js`, `extracted_strings.json`, or
   `translated_zh.json` into the merge commit unless they are intentionally
   part of the change.

2. Merge `origin/main` into local `main`.

3. Resolve `cron/scheduler.py` manually.

   This is the only expected textual conflict. Compose upstream diagnostics/env
   sanitization with local profile/DingTalk delivery.

4. Review auto-merged high-risk files:

   - `gateway/run.py`
   - `hermes_cli/web_server.py`
   - `hermes_cli/config.py`
   - `tools/delegate_tool.py`
   - `tests/hermes_cli/test_web_server.py`

5. Run targeted tests in this order:

   ```bash
   .venv/bin/python3 -m py_compile gateway/run.py gateway/platforms/dingtalk.py gateway/turn_status_card.py cron/scheduler.py hermes_cli/web_server.py tools/delegate_tool.py
   .venv/bin/python3 -m pytest tests/cron/test_cron_script.py tests/cron/test_cron_profile.py -q
   .venv/bin/python3 -m pytest tests/gateway/test_turn_status_card.py tests/gateway/test_dingtalk.py tests/gateway/test_restart_resume_pending.py -q
   .venv/bin/python3 -m pytest tests/tools/test_mcp_elicitation.py tests/tools/test_refresh_agent_mcp_tools.py tests/tools/test_mcp_capability_gating.py -q
   .venv/bin/python3 -m pytest tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_profile_dashboard.py -q
   ```

6. Then run broader gateway/tools/web checks as time allows.

## Practical Risk Ranking

High:

- `cron/scheduler.py`: only real conflict; delivery and env safety both matter.
- `gateway/run.py`: auto-merge but core lifecycle/progress/MCP semantics.
- `hermes_cli/web_server.py` + web frontend: profile/dashboard surface plus
  official reasoning/model changes.

Medium:

- `tools/delegate_tool.py`: small diff, important for DingTalk subagent
  progress.
- `hermes_cli/config.py`: default config composition and schema visibility.
- i18n files: large surface, typecheck risk.

Low:

- Desktop app official features: mostly independent.
- Optional creative skill expansion.
- Release/version/doc-only changes.

## Bottom Line

The merge is feasible and should not require sacrificing official features.
The only required hand conflict is cron. The real work is semantic verification
after an apparently clean auto-merge, especially for gateway runtime lifecycle
and dashboard/profile config behavior.

## Merge Execution Notes

Status after applying the merge:

- `cron/scheduler.py` was the only textual conflict and was resolved manually.
- Upstream cron target-error diagnostics and `_sanitize_subprocess_env()` were
  preserved.
- Local DingTalk/session-webhook cron delivery and per-profile cron ownership
  were preserved.
- `cron/jobs.py` now stores logical profile ownership fields when dashboard
  creates or updates jobs.
- Dashboard cron API keeps physical cron storage centralized under the default
  profile and filters by `owner_profile` for profile views.
- Dashboard source imports use a source-tree import helper so `plugins/cron`
  cannot shadow the built-in `cron` package.
- Duplicate profile route registrations were split so the enhanced profile
  endpoints handle `/api/profiles` while legacy handlers are retained under
  `/api/profiles-legacy`.
- Fresh dashboard profile creation preserves the official bundled-skill seeding
  behavior.
- Dashboard config/template clone preserves the local privacy behavior: clone
  `config.yaml`/identity without copying `.env` or source skills.
- Profile metadata now includes the local `template` flag.
- Profile list rows include local binding summary fields.
- Session rows derived from `agent:<profile>:<source...>` IDs now include
  source-binding annotations.
- Profile deletion now clears source bindings, removes owned cron jobs, sends
  best-effort DingTalk session-webhook notifications, and appends a redacted
  audit event.

Validation run:

```bash
.venv/bin/python3 -m py_compile hermes_cli/profiles.py hermes_cli/web_server.py cron/jobs.py cron/scheduler.py
.venv/bin/python3 -m pytest tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_profile_dashboard.py tests/hermes_cli/test_dashboard_admin_endpoints.py -q
.venv/bin/python3 -m pytest tests/hermes_cli/test_profiles.py -q
.venv/bin/python3 -m pytest tests/cron/test_cron_script.py tests/cron/test_cron_profile.py -q
.venv/bin/python3 -m pytest tests/gateway/test_turn_status_card.py tests/gateway/test_dingtalk.py tests/gateway/test_restart_resume_pending.py -q
.venv/bin/python3 -m pytest tests/tools/test_mcp_elicitation.py tests/tools/test_refresh_agent_mcp_tools.py tests/tools/test_mcp_capability_gating.py -q
npm --workspace web run test -- src/lib/reasoning-effort.test.ts
git diff --check
```

Results:

- Profile/dashboard/admin endpoints: `414 passed, 1 warning`
- Profile CLI/unit coverage: `138 passed`
- Cron profile/script coverage: `58 passed, 1 warning`
- Gateway DingTalk/status/restart coverage: `187 passed`
- MCP elicitation/refresh/capability coverage: `53 passed, 1 warning`
- Web reasoning-effort coverage: `1 passed`, `6 tests passed`
- `git diff --check`: passed

## Live Dashboard/Gateway Validation

After the explicit E2E run, official `main` advanced again by 2 commits through:

- `65561e9de Merge pull request #49563 from NousResearch/salvage/signal-quote-history`

The additional upstream changes preserve quoted reply context for Signal.
The merge into local `main` completed cleanly.

Current upstream coverage:

```bash
git rev-list --left-right --count HEAD...origin/main
git merge-base --is-ancestor origin/main HEAD
```

Result:

- Local branch is `ahead 17`, `behind 0`.
- `origin/main` is an ancestor of local `HEAD`.

Run-state validation used the actual dashboard and gateway processes, not only
unit tests:

```bash
PYTHONPATH=/Users/later0day/Desktop/hermes-agent \
  /Users/later0day/Desktop/hermes-agent/.venv/bin/python \
  -m hermes_cli.main --profile xcx dashboard --port 9119 --skip-build --no-open
```

Dashboard result:

- Listening on `127.0.0.1:9119`.
- `/sessions?profile=xcx` returned `200 text/html`.
- Authenticated dashboard API endpoints for status, profiles, profile details,
  sessions, profile sessions, messaging platforms, cron jobs, MCP servers,
  toolsets, and config all returned `200`.

Gateway was restarted through the dashboard API:

```bash
POST /api/gateway/restart?profile=xcx
```

Result:

- Restart request returned `{"ok": true, "name": "gateway-restart"}`.
- Poll sequence observed `draining:disconnected` -> `stopped:none` ->
  `running:connected`.
- `/api/status?profile=xcx` reported DingTalk as connected:
  `gateway_running=true`, `gateway_state=running`,
  `gateway_platforms.dingtalk.state=connected`.
- `/api/messaging/platforms/dingtalk/test?profile=xcx` returned
  `{"ok": true, "state": "connected"}`.
- After restart settled, waiting another 10 seconds produced `0` new
  `gateway.error.log` lines.

The restart itself produced expected shutdown noise from the DingTalk Stream SDK
while closing the old websocket:

- `ERROR dingtalk_stream.client: [start] network exception, error=`

No new instances were observed after the restart settled for the previously
reported runtime failures:

- `TypeError: MessageEvent.__init__() got an unexpected keyword argument 'media_errors'`
- `ModuleNotFoundError: No module named 'cron.scheduler_provider'`
- `Unauthorized user`
- DingTalk 429 approval lockout
- DingTalk image/media IP whitelist failures
- `RuntimeWarning: coroutine ... was never awaited`

Additional validation after the live restart:

```bash
.venv/bin/python3 -m py_compile gateway/run.py gateway/platforms/base.py gateway/platforms/signal.py gateway/platforms/dingtalk.py gateway/turn_status_card.py hermes_cli/web_server.py cron/scheduler.py cron/jobs.py
.venv/bin/python3 -m pytest tests/gateway/test_signal.py tests/gateway/test_signal_format.py tests/gateway/test_reply_to_injection.py tests/tools/test_send_message_tool.py -q
.venv/bin/python3 -m pytest tests/gateway/test_dingtalk.py tests/gateway/test_turn_status_card.py tests/gateway/test_restart_resume_pending.py -q
.venv/bin/python3 -m pytest tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_profile_dashboard.py tests/hermes_cli/test_dashboard_admin_endpoints.py -q
scripts/run_tests.sh tests/e2e --include-integration -j 4
git diff --check
```

Results:

- Signal/reply/send-message coverage: `328 passed, 2 warnings`
- DingTalk/status-card/restart coverage: `189 passed`
- Dashboard web-server coverage: `414 passed, 1 warning`
- Explicit E2E runner: `57 passed, 0 failed`
- `git diff --check`: passed

Runtime bug found and fixed during live validation:

- DingTalk stage-reaction updates were invoked from the agent worker thread.
  The adapter used `asyncio.create_task()` directly, which can fail outside the
  adapter event loop and leak an unawaited `_swap()` coroutine warning.
- `DingTalkAdapter` now records its connection loop and schedules background
  reaction coroutines via `asyncio.run_coroutine_threadsafe()` when called from
  another thread, while preserving direct task scheduling on the adapter loop.
  A regression test covers this worker-thread scheduling path.

## Full Regression And E2E Status

The default full regression runner was attempted after the upstream/local merge:

```bash
scripts/run_tests.sh -j 8
```

Important runner behavior:

- The default runner discovers `tests/` but excludes `tests/integration`,
  `tests/e2e`, and `tests/docker`.
- E2E coverage must therefore be invoked explicitly with either an explicit
  `tests/e2e` path or `--include-integration`.

Default full-regression result:

- `1578` test files discovered.
- `33155` tests discovered.
- Exit code: `1`.
- `24` files had test failures, for `83` failed tests.
- `8` files had no runnable result because of collection/import errors or
  timeout before collection.

Failing files from the default run:

- `tests/agent/test_anthropic_adapter.py`
- `tests/cron/test_cron_workdir.py`
- `tests/cron/test_parallel_pool.py`
- `tests/gateway/test_background_command.py`
- `tests/gateway/test_delegate_command.py`
- `tests/gateway/test_matrix.py`
- `tests/gateway/test_gateway_shutdown.py`
- `tests/gateway/test_shutdown_forensics.py`
- `tests/hermes_cli/test_dashboard_profiles_nav_label.py`
- `tests/hermes_cli/test_gateway_wsl.py`
- `tests/hermes_cli/test_gateway_service.py`
- `tests/hermes_cli/test_mcp_security.py`
- `tests/hermes_cli/test_profile_describer.py`
- `tests/hermes_cli/test_service_manager.py`
- `tests/hermes_cli/test_signal_handler_kanban_worker.py`
- `tests/hermes_cli/test_web_server_cron_profiles.py`
- `tests/plugins/test_kanban_dashboard_swarm.py`
- `tests/plugins/web/test_parallel_keyless_mcp.py`
- `tests/run_agent/test_real_interrupt_subagent.py`
- `tests/test_live_system_guard_self_test.py`
- `tests/test_tui_gateway_server.py`
- `tests/tools/test_managed_browserbase_and_modal.py`
- `tests/tools/test_send_message_missing_platforms.py`
- `tests/tools/test_web_keyless_default_fallback.py`

Files with no runnable result in the default run:

- `tests/agent/test_model_metadata_ssl.py`
- `tests/gateway/test_agent_command.py`
- `tests/gateway/test_profile_runtime_context.py`
- `tests/gateway/test_recent_image_resend.py`
- `tests/gateway/test_source_agent_binding.py`
- `tests/hermes_cli/test_auth_ssl_macos.py`
- `tests/run_agent/test_primary_runtime_restore.py`
- `tests/run_agent/test_run_agent.py`

E2E was then run explicitly:

```bash
scripts/run_tests.sh tests/e2e --include-integration -j 4
.venv/bin/python3 -m pytest tests/e2e -q -rs
```

E2E result:

- Parallel runner: `3` files, `57` tests passed, `0` failed.
- Direct pytest with skip reporting: `57 passed, 7 skipped`.
- Skips included the Matrix x-sign bootstrap tests because the homeserver was
  not reachable at `http://127.0.0.1:26167`.
- Two platform-command shortcut tests skipped because those shortcut scopes are
  intentionally limited.

The Matrix Docker-backed real E2E path was not started automatically. It
requires the fixture service from:

```bash
docker compose -f tests/e2e/matrix_xsign_bootstrap/docker-compose.yml up -d
```

Current conclusion:

- Official upstream coverage is complete at the git ancestry level:
  `origin/main` is an ancestor of local `HEAD`, and local is `ahead 16`,
  `behind 0`.
- Targeted merge-risk regressions passed.
- Explicit lightweight E2E passed.
- The default full regression suite is not green yet, so this should not be
  reported as a fully clean regression.

## Follow-Up Upstream Coverage Check

After the first merge commit, `git fetch origin` found that official `main`
advanced by 10 additional commits, through:

- `ff50a8861 Merge pull request #49558 from NousResearch/salvage/env-var-guards-48735`

Those additional official commits include:

- Signal markdown formatting shared across send paths.
- Signal ADTS AAC voice-note detection/remuxing.
- Safer malformed env var parsing helpers.
- Desktop hidden link-title window audio muting.

The second merge completed cleanly with no textual conflicts. Current coverage
state after the second merge:

```bash
git merge-base --is-ancestor origin/main HEAD
git rev-list --left-right --count HEAD...origin/main
```

Result:

- `origin/main` is an ancestor of local `HEAD`.
- Local branch is `ahead 16`, `behind 0`.

Additional validation after the second merge:

```bash
.venv/bin/python3 -m py_compile hermes_cli/profiles.py hermes_cli/web_server.py cron/jobs.py cron/scheduler.py gateway/run.py gateway/platforms/dingtalk.py gateway/turn_status_card.py gateway/platforms/signal.py gateway/platforms/signal_format.py tools/delegate_tool.py tools/send_message_tool.py utils.py
.venv/bin/python3 -m pytest tests/gateway/test_signal.py tests/gateway/test_signal_format.py tests/tools/test_send_message_tool.py -q
.venv/bin/python3 -m pytest tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_profile_dashboard.py tests/hermes_cli/test_dashboard_admin_endpoints.py -q
.venv/bin/python3 -m pytest tests/cron/test_cron_script.py tests/cron/test_cron_profile.py tests/hermes_cli/test_profiles.py -q
.venv/bin/python3 -m pytest tests/gateway/test_turn_status_card.py tests/gateway/test_dingtalk.py tests/gateway/test_restart_resume_pending.py -q
.venv/bin/python3 -m pytest tests/tools/test_mcp_elicitation.py tests/tools/test_refresh_agent_mcp_tools.py tests/tools/test_mcp_capability_gating.py -q
npm --workspace web run test -- src/lib/reasoning-effort.test.ts
git diff --check
```

Results:

- Signal/send-message coverage: `319 passed, 2 warnings`
- Profile/dashboard/admin endpoints: `414 passed, 1 warning`
- Cron/profile coverage: `196 passed, 1 warning`
- Gateway DingTalk/status/restart coverage: `187 passed`
- MCP elicitation/refresh/capability coverage: `53 passed, 1 warning`
- Web reasoning-effort coverage: `1 passed`, `6 tests passed`
- `git diff --check`: passed

## Current Final State

Latest verified state after the final upstream merge and live process checks:

- `origin/main` is an ancestor of local `HEAD`.
- Local branch is `ahead 17`, `behind 0`.
- Dashboard is running on `127.0.0.1:9119` from the current workspace code.
- xcx gateway is running under launchd from the current workspace code.
- xcx DingTalk state is `connected`.
- Dashboard API successfully restarted gateway and saw it return to
  `running:connected`.
- No new `gateway.error.log` lines appeared during the 10-second settle window
  after restart.
- Targeted regressions and explicit E2E passed; the default all-tests runner
  was attempted but is still not fully green.

## Latest Full Default Regression Run

After the live dashboard/gateway checks, the default full regression runner was
executed from the current workspace:

```bash
scripts/run_tests.sh -j 8
```

Final result:

- `1578` files discovered.
- `33180` tests executed by the default runner.
- `32570` tests passed.
- `83` tests failed.
- Wall time: `493.7s` with `8` workers.

The default suite is therefore still not green. The runner reported failures in
these files:

- `tests/agent/test_anthropic_adapter.py` - 3 failures
- `tests/cron/test_cron_workdir.py` - 1 failure
- `tests/cron/test_parallel_pool.py` - 1 failure
- `tests/gateway/test_background_command.py` - 1 failure
- `tests/gateway/test_delegate_command.py` - 9 failures
- `tests/gateway/test_matrix.py` - 1 failure
- `tests/gateway/test_gateway_shutdown.py` - 1 failure
- `tests/gateway/test_shutdown_forensics.py` - 1 failure
- `tests/hermes_cli/test_dashboard_profiles_nav_label.py` - 1 failure
- `tests/hermes_cli/test_gateway_wsl.py` - 2 failures
- `tests/hermes_cli/test_gateway_service.py` - 6 failures
- `tests/hermes_cli/test_mcp_security.py` - 1 failure
- `tests/hermes_cli/test_profile_describer.py` - 2 failures
- `tests/hermes_cli/test_service_manager.py` - 2 failures
- `tests/hermes_cli/test_signal_handler_kanban_worker.py` - 1 failure
- `tests/hermes_cli/test_web_server_cron_profiles.py` - 1 failure
- `tests/plugins/test_kanban_dashboard_swarm.py` - 3 failures
- `tests/plugins/web/test_parallel_keyless_mcp.py` - 34 failures
- `tests/run_agent/test_real_interrupt_subagent.py` - 1 failure
- `tests/test_live_system_guard_self_test.py` - 4 failures
- `tests/test_tui_gateway_server.py` - 1 failure
- `tests/tools/test_managed_browserbase_and_modal.py` - 1 failure
- `tests/tools/test_send_message_missing_platforms.py` - 1 failure
- `tests/tools/test_web_keyless_default_fallback.py` - 4 failures

The runner also reported 8 files where tests did not run because of collection
errors or per-file timeout:

- `tests/agent/test_model_metadata_ssl.py`
- `tests/gateway/test_agent_command.py`
- `tests/gateway/test_profile_runtime_context.py`
- `tests/gateway/test_recent_image_resend.py`
- `tests/gateway/test_source_agent_binding.py`
- `tests/hermes_cli/test_auth_ssl_macos.py`
- `tests/run_agent/test_primary_runtime_restore.py`
- `tests/run_agent/test_run_agent.py`

Post-run sanity checks:

```bash
git diff --check
git rev-list --left-right --count HEAD...origin/main
git merge-base --is-ancestor origin/main HEAD
lsof -nP -iTCP:9119 -sTCP:LISTEN
.venv/bin/python3 -m hermes_cli.main --profile xcx gateway status
```

Results:

- `git diff --check`: passed.
- Upstream coverage remains complete: local is `ahead 17`, `behind 0`, and
  `origin/main` is an ancestor of local `HEAD`.
- Dashboard is still listening on `127.0.0.1:9119`.
- xcx gateway service remains loaded under launchd from this checkout, PID
  `29802`, with DingTalk reported as `connected` by `/api/status?profile=xcx`.
