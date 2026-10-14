# Upstream/Main Merge Assessment

Date: 2026-08-13

Local branch (product work): `feat/agent-room-m1-design` @ `07d3b33553`

Merge branch: `merge/upstream-2026-08-13` @ `0cbc112e3d`

Upstream target: `upstream/main` @ `89a84e1ae6` (tip 2026-08-12)

## Divergence

- Merge-base: `f15a38ee73`.
- Upstream ahead by **942** commits; local ahead by 144.
- `upstream/main` is NOT an ancestor of our HEAD; `origin/main` (fork) IS an
  ancestor of HEAD.
- Real textual conflicts (7 files), all hand-composed:
  - `cron/jobs.py`
  - `cron/scheduler.py`
  - `hermes_cli/web_server.py`
  - `tests/agent/test_turn_context.py`
  - `tests/gateway/test_config_driven_access_policy.py`
  - `tools/cronjob_tools.py`
  - `tools/send_message_tool.py`
- Agent-room surface (`gateway/agent_room_*.py`, `tools/room_decompose_tool.py`)
  merged with ZERO conflicts — upstream did not touch it.

## Executive Direction

Same shape as the 2026-06-20 merge — NOT "ours wins" / "theirs wins":

1. Take upstream wholesale for independent official areas (desktop, plugins,
   skills, docs, web frontend, MCP, release/version bumps).
2. Preserve local product work: agent-room, DingTalk origin/metadata delivery,
   dashboard/profile unification, cron profile partitioning.
3. Hand-compose the 7 real conflicts by keeping BOTH sides' behavior.
4. Semantically review auto-merged runtime-boundary files even though Git
   merged them textually.

Method matches the repo precedent: a single `git merge upstream/main` producing
a true merge commit with both parents (like `a46d821001`, an 862-commit
one-shot), NOT a rebase or squash.

## Conflict Resolutions (compose both sides)

- **`cron/jobs.py`** (4 conflicts): kept our `profile` param + normalization AND
  upstream's `monitor_script`/`monitor_url`. `create_job` persists
  `owner_profile`/`profile`/`run_profile` and `monitor_script`/`monitor_url`;
  `update_job` keeps both normalization loops.
- **`tools/cronjob_tools.py`** (3 conflicts): kept both `profile` and
  `monitor_script`/`monitor_url` across signature, `create_job` call, and
  registry dispatch lambda.
- **`cron/scheduler.py`** (1 conflict, imports): combined `import uuid` +
  `from contextlib import contextmanager` + `from datetime import datetime,
  timezone`.
- **`hermes_cli/web_server.py`** (1 conflict, profile-list dict): kept our field
  superset (`agent_id`, `template`, `binding_count`, `binding_summary`) AND
  adopted upstream's `entry_path` variable + `_safe(...)` wrapping for
  `has_env`.
- **`tests/agent/test_turn_context.py`** (1 conflict): kept both our
  persisted-cooldown-survives-restart test and upstream's
  machine-driven-runs-not-titled test.
- **`tests/gateway/test_config_driven_access_policy.py`** (2 conflicts): kept
  upstream's stored-route-cannot-reuse-adapter-auth test AND our two DingTalk
  allow_all tests.
- **`tools/send_message_tool.py`** (3 real semantic conflicts):
  1. Target resolution — kept our `origin`/`current` branch (incl. DingTalk
     `send_metadata` conversation_type) and routed the non-origin path through
     upstream's new `prepare_send_message_platforms()` +
     `resolve_send_target(platform_name, target_ref)` (which subsumes our old
     manual `_parse_target_ref` + `resolve_channel_name` fallback).
  2. `send_kwargs` — kept BOTH `metadata` (ours) and `args` for custom
     handlers (upstream); they don't overlap.
  3. `_send_to_platform` — kept our `_await_on_gateway_loop` helper and merged
     the signature to include both `metadata=None` and `args=None`.

## Semantic Review of Auto-Merged Runtime-Boundary Files

- **`gateway/run.py`** — compiles; DingTalk per-turn `turn_status_card` (37
  refs) and MCP reload (`_handle_reload_mcp_command`/`_execute_mcp_reload`)
  both intact and coexist with upstream's changes. No markers.
- **`gateway/session.py`, `toolsets.py`, `agent/tool_executor.py`** — compile
  clean.
- **`gateway/platforms/base.py`** — upstream added a ~210-line docker
  container->host media-path translation block with a credential denylist
  (`_translate_docker_container_media_path`,
  `_docker_persistent_home_host_root`). Reviewed: `/root/.hermes/*` is
  explicitly refused for home-mount translation, and the per-file credential
  denylist (`auth.json`, `.env`, `mcp-tokens/`, ...) still blocks delivery.
  Verified on the live host: `validate_media_delivery_path("/root/.hermes/
  auth.json")` returns `None`.

## Regression Status

Baseline established by running the failing files at pre-merge HEAD
`07d3b33553` in an isolated worktree, to separate pre-existing failures from
merge-introduced ones.

- Conflict-file test areas — `tests/tools/test_send_message_tool.py`,
  `tests/agent/test_turn_context.py`,
  `tests/gateway/test_config_driven_access_policy.py`: **72 passed, 0 failed**.
- Cron + agent-room areas: **718 passed, 0 failed** (after fixing two
  pre-existing stale test-doubles in `tests/cron/test_cron_profile.py` —
  singular `advance_next_run` -> plural `advance_next_runs`, and `fake_run_job`
  now accepts `extra_prompt`; both were already broken at pre-merge HEAD, not
  merge-caused).
- Full agent-room suite: **239 passed, 0 failed**.
- Broad gateway + web_server + mcp sweep (728 files, ~5927 tests):
  6539 passed, **38 failed**.
  - 37 of those 38 are IDENTICAL at pre-merge HEAD `07d3b33553` (pre-existing;
    `test_delegate_command`, `test_turn_status_card`,
    `test_profile_runtime_context`, `test_web_server_profile_dashboard`,
    `test_background_command` [`_toolsets_override` NameError in our own M4
    code], `test_gateway_utf8_encoding`, `test_mcp_security`,
    `test_web_server_cron_profiles`, `test_mcp_serve`).
  - The 1 delta —
    `test_platform_base.py::test_container_credential_path_never_translates_through_home`
    — is a NEW upstream test that is host-environment-sensitive, not a
    merge-logic defect: it assumes `/root/.hermes/auth.json` does not exist on
    the host, but this production box runs as root and the file exists, so with
    `HERMES_HOME` monkeypatched to a tmp dir the denylist prefix no longer
    covers the real host file. In real runtime (`HERMES_HOME=/root/.hermes`)
    the denylist catches it and delivery is refused.

**Net: zero merge-introduced logic regressions.**

## Bottom Line

Merge committed as `0cbc112e3d` (parents `07d3b33553` + `89a84e1ae6`). All 7
conflicts composed to keep both sides; runtime-boundary files reviewed and our
product features (agent-room, DingTalk delivery, MCP reload, status card, cron
profiles) verified intact.

## Follow-Ups

- Pre-existing failures (37) are unrelated to this merge and tracked
  separately; the two stale cron test-doubles were fixed opportunistically here
  since they sat in a conflict-adjacent area.
- Product gate (non-code): A/B dogfooding of the agent-room decompose path
  (15 needs / 7 days).
