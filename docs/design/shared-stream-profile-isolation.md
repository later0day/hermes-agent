# Shared-Stream Profile Isolation (binding-routed multiplex)

## Problem

On the xcx gateway the `jb` profile (bound to the DingTalk group
"奔波儿灞测试") leaked xcx's skills / config / SOUL / memory / sessions /
credentials. We want **team-mode complete isolation**: every DingTalk group is
an independent profile with its own skills, memory, MCP, model, and cron — while
still letting an Agent Room collaborate across multiple profiles inside one IM
group.

## Hard constraint: one shared DingTalk app for all profiles

All profiles share a **single** `client_id` / `client_secret`. Requiring 1000
DingTalk apps for 1000 profiles is untenable. DingTalk Stream Mode keeps exactly
**one** long-lived WebSocket per app credential, so there is exactly **one**
inbound stream for every group. Isolation therefore cannot come from
per-profile adapters — it must come from **routing each inbound message to the
right profile and running that turn under the profile's scope**.

## Root cause of the leak

The source→agent binding store (`gateway/source_agent_binding.py`, a fork
addition) maps `source:dingtalk:group:{chat_id}:{user_id}` → `profile_name`.
It was consulted **only** in `_resolve_profile_home_for_source` (dimension ①:
which profile *home* the turn runs under) but **never** in the ingress stamping
path `_profile_name_for_source` (dimension ②: the session-key namespace
`agent:<profile>` and the value stamped onto `source.profile`).

Result: a bound group's turn ran under jb's HERMES_HOME, but `source.profile`
stayed empty, so its conversation history and active-profile-derived state fell
back to the active profile (xcx). The two dimensions disagreed → the leak.

## Fix

Introduce a single source of truth, `GatewayRunner._binding_profile_for_source`,
and call it from **both** paths so ① and ② always agree:

- `_profile_name_for_source` (the SINGLE ingress stamping path) now resolves the
  binding **before** static `profile_routes`. Whatever it returns is stamped
  onto `source.profile`, which selects both the session-key namespace and the
  turn's profile home.
- `_resolve_profile_home_for_source` reuses the same helper instead of its own
  inline lookup.

The helper is best-effort: missing store / malformed key / multiplex-off all
return `None`, so routing falls through to `profile_routes` / the active
profile rather than dropping the message.

## Operational requirements

1. **`gateway.multiplex_profiles: true`** on the shared gateway. This activates
   ingress stamping, per-profile session namespacing, per-profile turn scope,
   per-profile cron, and fail-closed secret reads.
2. **Do NOT start per-profile secondary DingTalk adapters.** Under the shared
   credential, `_start_secondary_profile_adapters` detects the duplicate
   credential and skips it (`duplicate_credential`, non-fatal). The single
   active-profile stream serves every group; binding-stamped `source.profile`
   routes each turn. Secondary profiles' `config.yaml` should therefore enable
   **no** platform block. A stray port-binding platform (e.g. `api_server`)
   raises a caught `SecondaryPortBindingConfigError` warning, not a crash.
3. **DingTalk shared-stream safety:** the adapter's `_get_scoped_secret` catches
   `UnscopedSecretError` and falls back to `os.getenv` for
   `DINGTALK_CLIENT_ID/SECRET`, so the shared stream survives fail-closed mode.

## Per-dimension isolation (verified)

| Dimension | Root | Isolated? |
|-----------|------|-----------|
| skills    | `get_hermes_home()/skills` | ✅ per profile home |
| memory    | `get_hermes_home()/memories` | ✅ |
| model/config | `get_hermes_home()/config.yaml` | ✅ |
| sessions  | `agent:<profile>` session namespace | ✅ (after this fix) |
| credentials | profile `.env` secret scope | ✅ |
| cron      | per-profile ticker with `set_hermes_home_override` | ✅ |
| MCP       | per-profile config filter | ⚠️ see below |

### MCP caveat

`_servers[name]` is a global cache keyed by server **name** only
(`mcp_tool.py`). Two profiles that define an MCP server with the **same name**
will share one instance. **Mitigation:** give each profile's MCP servers a
unique / profile-prefixed name.

## Room / Team collaboration

Agent Room turns (`gateway/agent_room_inprocess_runner.py::_run_agent_blocking`)
switch across N member profiles within one process/message using the same
primitives (`set_hermes_home_override` + `set_secret_scope` + `TERMINAL_CWD`),
**independent of the multiplex flag**. This is why per-profile-systemd-processes
were rejected: rooms inherently run multiple profiles inside one message, and
that model doesn't scale to 1000 profiles anyway.
