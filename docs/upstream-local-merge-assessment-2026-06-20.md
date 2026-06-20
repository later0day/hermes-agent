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
