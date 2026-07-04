# Profile Gateway Launchd Runtime Analysis

Date: 2026-06-20

## Symptom

The Dashboard action log for `profile=xcx` showed old DingTalk tracebacks, and
the profile gateway status stayed stopped after a restart. The profile launchd
job exited immediately with code 1.

## Findings

The `media_errors` traceback in the Dashboard action log was historical output
from the append-only `gateway-restart.log`. Current source has
`MessageEvent.media_errors`, and direct import against the current checkout sees
the field.

The live failure was separate: the generated profile launchd plist used
`WorkingDirectory=/Users/later0day/.hermes/profiles/xcx`, then launched
`python -m hermes_cli.main`. In a source checkout that is not installed as a
site package, Python cannot import `hermes_cli` from a profile directory, so the
job fails before the gateway starts.

The xcx profile also had no DingTalk credentials in its profile `.env`, so a
correctly scoped xcx gateway would not enable DingTalk until those credentials
are configured for that profile.

After the profile service was fixed, a second profile-only runtime issue
appeared: profile homes contain a runtime state directory named `cron/`. A
gateway running with cwd set to the profile home can cache that directory as the
top-level `cron` namespace package, so later imports such as
`cron.scheduler_provider` fail even though the Hermes source tree is present.

After the gateway reached DingTalk, the next live request exposed profile
isolation for model credentials as well. The xcx profile had
`model.provider=alibaba` / `qwen3.6-plus`, but its profile `.env` did not carry
the DashScope model key or base URL from the default profile. The resulting
agent call used the provider fallback endpoint and returned HTTP 401. This was
not related to the earlier `MessageEvent.media_errors` constructor mismatch.

The Dashboard restart panel was also misleading because per-action logs were
opened in append mode. A fresh gateway restart could therefore display old
tracebacks above the current start marker.

## Fix

- Add `PYTHONPATH=<project root>` to generated systemd and launchd gateway
  service definitions. This preserves the stable `HERMES_HOME` working
  directory while keeping source-checkout imports working.
- Add regression coverage that service definitions keep `WorkingDirectory`
  stable and include `PYTHONPATH`.
- Force gateway imports of the Hermes `cron` package to prefer the project root
  and clear any profile-state `cron` namespace cached in `sys.modules`.
- Configure DingTalk credentials in the target profile when running a profile
  scoped gateway.
- Configure the target profile with its own model provider credentials. Profiles
  are isolated by design; they do not inherit secrets from the default profile
  at runtime.
- Replace per-action Dashboard logs on each action start/completion instead of
  appending, so the restart/status panel reflects the current action only.

## Runtime Verification

- `profile=xcx` launchd service runs with
  `PYTHONPATH=/Users/later0day/Desktop/hermes-agent` and
  `HERMES_HOME=/Users/later0day/.hermes/profiles/xcx`.
- Dashboard status for `profile=xcx` reports `gateway_running=true`,
  `gateway_state=running`, and DingTalk `state=connected`.
- A stale local Python bytecode cache briefly kept an older gateway module in
  service after the source fix. Clearing the generated `.pyc` files and
  restarting the profile service forced the launchd process to compile current
  source.
- The first cron fix inside `gateway.run` was not early enough for the CLI
  gateway entrypoint. `hermes_cli.gateway.run_gateway()` now also prefers the
  project `cron` package before importing `gateway.run`, so profile runtime
  directories cannot poison the import path during startup.
- After the 06:03 restart, the gateway started cleanly and stayed running with
  DingTalk connected. Recent DingTalk messages were handled end-to-end with
  `response ready` logged, and there are no new `media_errors`, DashScope 401,
  or `cron.scheduler*` startup failures after that restart.
- DingTalk AI Card requests still return `Forbidden.AccessDenied.IpNotInWhiteList`
  until the current outbound IP is allowed in the DingTalk app console. That is
  an external DingTalk allowlist failure; the gateway falls back to webhook
  sending after the card failure.
