# Dashboard/Gateway Cleanup Warning Analysis

Date: 2026-06-20

Scope:
- Dashboard restart side effects
- `tui_gateway.ws` disconnect logs
- `tools.terminal_tool` cleanup thread `FileNotFoundError`
- `raft CLI not found` plugin warning

## Findings

The `tui_gateway.ws` close messages with code `1012` are expected when the dashboard or embedded TUI connection is restarted. They indicate browser-side WebSocket clients disconnected and reconnected; they are not the root error.

The repeated `hermes_plugins.raft_platform.adapter: [raft] raft CLI not found in PATH` warnings come from the optional raft platform plugin. The plugin is enabled or discovered, but the `raft` CLI binary is not installed on `PATH`. This is unrelated to approvals, DingTalk streaming, or dashboard config.

The actionable warning is:

```text
tools.terminal_tool: Error in cleanup thread: [Errno 2] No such file or directory
```

Earlier in the same log window, a browser cleanup path also reported:

```text
Error: ENOENT: process.cwd failed with error no such file or directory
```

That points to a deleted/stale working directory condition. The terminal cleanup thread is a background maintenance loop; if the process cwd or a child tool cwd is removed while the process is still alive, stdlib helpers can raise `FileNotFoundError` while the cleanup loop is building terminal environment config.

## Runtime State

At inspection time:

- Dashboard was running on `127.0.0.1:9119` as PID `32048`.
- Long-running gateway was running as PID `978`, started at `2026-06-20 01:23:11`, listening on port `8644`.
- Gateway PID `978` was still using the old in-memory code, so it continued logging the cleanup warning after the dashboard was restarted.

## Fix Applied

`tools/terminal_tool.py` now calls `_repair_deleted_cwd()` before terminal environment config is read. This covers both the background cleanup thread and foreground tool paths such as cron-triggered `execute_code`, because both read `_get_env_config()`. If `os.getcwd()` raises `FileNotFoundError`, it tries to move the process cwd to, in order:

1. `TERMINAL_CWD`
2. `HERMES_CWD`
3. the repository root inferred from `tools/terminal_tool.py`
4. the user's home directory

`tools/code_execution_tool.py` also falls back to the staging directory if project-mode cwd resolution sees a deleted process cwd after an invalid `TERMINAL_CWD`.

Regression coverage was added in `tests/tools/test_terminal_task_cwd.py`.

## Verification

Targeted tests passed:

```text
.venv/bin/python3 -m pytest tests/tools/test_code_execution_modes.py::TestResolveChildCwd tests/tools/test_terminal_task_cwd.py -q
18 passed

.venv/bin/python3 -m pytest tests/tools/test_terminal_tool_pty_fallback.py -q
3 passed
```

The dashboard process was restarted after the fix. The gateway process must also be restarted for the cleanup-thread fix to take effect there.

## 2026-06-20 05:13 `tui_gateway.ws` Close Check

Observed log:

```text
2026-06-20 05:13:28,737 INFO tui_gateway.ws: ws closed peer=127.0.0.1:51204 reason=client_disconnect(code=1001,reason=) messages=1 parse_errors=0 dispatch_crashes=0 send_failures=0 reaped_sessions=1 detached_sessions=0
```

This is not a DingTalk gateway crash. The logger is `tui_gateway.ws`, which is the dashboard/embedded TUI WebSocket path. The close handling lives in `tui_gateway/ws.py`; when `receive_text()` raises `WebSocketDisconnect`, the server records `client_disconnect(code=..., reason=...)`, closes or detaches any sessions owned by that socket, and logs the counters from the single teardown path.

The important fields in this instance:

- `peer=127.0.0.1:51204`: local browser/dashboard client.
- `code=1001`: client went away, such as page navigation, refresh, tab close, or the embedded terminal reconnecting.
- `messages=1`: the socket only handled the initial setup message before disconnect.
- `parse_errors=0 dispatch_crashes=0 send_failures=0`: no server-side parse, dispatch, or send failure.
- `reaped_sessions=1 detached_sessions=0`: one close-on-disconnect sidecar session was cleaned up; no orphaned detached session remained.

Current runtime verification at inspection time:

```text
dashboard: PID 32395 listening on 127.0.0.1:9119
gateway:   PID 32863 listening on *:8644
status:    gateway_running=true, gateway_state=running
platforms: dingtalk=connected, webhook=connected
```

The same `gui.log` window shows a new WebSocket accepted immediately after the close:

```text
2026-06-20 05:13:29,000 INFO tui_gateway.ws: ws accepted peer=127.0.0.1:51227
```

That pattern is consistent with a dashboard/TUI reconnect, not a gateway outage.
