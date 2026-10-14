# Gateway stale restart runtime ownership analysis

Date: 2026-06-20
Profile: `xcx`

## Symptom

The DingTalk allow-all configuration had already been verified in current code,
but `gateway.log` still showed:

```text
WARNING gateway.run: Unauthorized user: $:LWCP_v1:$NoDPZEnaHdl5WfqKihxdt899LnbQ0x+0 (弓淼) on dingtalk
```

The same log also repeated an old `MessageEvent(media_errors=...)` traceback
even though the current `MessageEvent` dataclass accepts `media_errors`.

## Runtime evidence

`lsof` showed multiple Python processes writing the same profile log:

```text
27585 python3 -m hermes_cli.main -p xcx gateway restart
32965 python3 -m hermes_cli.main -p xcx gateway restart
37676 python3 -m hermes_cli.main -p xcx gateway restart
43088 python  -m hermes_cli.main --profile xcx gateway run --replace
```

`gateway-exit-diag.log` confirmed the first three processes had entered the
gateway start flow while their argv still said `gateway restart`.

After killing `27585`, `32965`, and `37676`, only `43088` remained as a writer.
The live `43088` process continued to receive DingTalk messages, send responses,
and create AI Cards. That proves the recurring unauthorized warning came from
stale gateway instances, not from the current authorization decision.

## Root cause

The manual restart fallback can call `run_gateway()` inside the same
`hermes gateway restart` process. That process is then a real gateway runtime,
but its command line remains `gateway restart`.

The runtime liveness matcher intentionally rejects `gateway restart` command
lines so CLI management commands do not look like gateway daemons. That is
correct for ordinary restart commands, but not for a restart process that has
actually entered `start_gateway()`.

Because the PID/lock/status metadata only stored argv, this created a bad split:

- the process was a real gateway and could receive DingTalk events;
- liveness checks could classify it as not a gateway;
- another gateway could start and write the same profile log/status files;
- old instances could later overwrite `gateway_state.json` with stale platform
  state such as `dingtalk=disconnected`.

## Fix

The fix is generic runtime ownership, not a DingTalk special case:

- `gateway.status.mark_current_process_as_gateway_runtime()` marks a process once
  it enters `start_gateway()`.
- PID, lock, and runtime-status records written after that point include
  `runtime: "gateway-runtime"`.
- `_record_looks_like_gateway()` accepts that marker even when argv is
  `gateway restart`.
- `write_runtime_status()` refuses to overwrite `gateway_state.json` when it is
  already owned by another live gateway process.

This preserves the strict command-line matcher for normal CLI commands while
still identifying a process that has genuinely become the gateway runtime.

## Regression coverage

Added coverage in `tests/gateway/test_status.py`:

- `test_get_running_pid_accepts_gateway_runtime_marker_for_restart_argv`
- `test_write_runtime_status_does_not_clobber_other_live_gateway`

Verification run:

```text
tests/gateway/test_status.py: 62 passed
tests/gateway/test_gateway_command_line_matcher.py + tests/gateway/test_config_driven_access_policy.py: 72 passed
tests/gateway/test_dingtalk.py + tests/gateway/test_project_package_imports.py: 71 passed
tests/gateway/test_status.py + tests/gateway/test_gateway_command_line_matcher.py + tests/gateway/test_restart_drain.py: 106 passed
tests/tools/test_approval.py::TestGatewayProtection + launchd restart no-foreground fallback test: 11 passed
```

Broader files also surfaced pre-existing unrelated failures:

```text
tests/hermes_cli/test_gateway_service.py: 158 passed, 6 failed
  Failure class: systemd user D-Bus preflight on macOS test host.

tests/hermes_cli/test_gateway_restart_loop.py: 41 passed, 1 failed
  Failure class: cron create_job() signature does not accept profile.
```
