# DingTalk Pairing / Allow-All Analysis

Date: 2026-06-20

## Symptom

The dashboard Pairing page showed a pending DingTalk request and clicking approve returned:

```text
429: Platform 'dingtalk' is locked out after too many failed approvals.
```

At the same time, DingTalk messages that previously worked directly started entering the user-pairing flow.

## Findings

This was user-authorization pairing, not dangerous-command approval.

The gateway log showed:

```text
Unauthorized user ... on dingtalk
```

The root config had:

```yaml
dingtalk:
  allow_all_users: true
```

but `gateway/config.py` only bridged DingTalk `allowed_users` and `allowed_chats` from YAML into environment variables. It did not bridge `allow_all_users` into `DINGTALK_ALLOW_ALL_USERS`, while `gateway/authz_mixin.py` authorizes DingTalk allow-all through that env var. As a result, the dashboard config looked open, but the gateway authorization path still treated the sender as unauthorized after restart.

A second issue made the dashboard Pairing page misleading. `PairingStore.list_pending()` returned a field named `code`, but that value was only the first 8 characters of the stored code hash. The original pairing code is intentionally not stored in plaintext and cannot be recovered from disk. The Pairing page used that hash prefix as the approval code, so clicking approve repeatedly sent an invalid code and triggered the platform lockout after 5 failed attempts.

The lockout is persisted in:

```text
~/.hermes/pairing/_rate_limits.json
```

It only blocks pairing-code approval for the platform until expiry; it does not matter once `DINGTALK_ALLOW_ALL_USERS=true` is active.

## Fix

Backend:

- `gateway/config.py` now bridges `dingtalk.allow_all_users` to `DINGTALK_ALLOW_ALL_USERS`, without overriding an explicit env var.
- `gateway/pairing.py` now returns pending hash prefixes as `code_hint`, not `code`.
- `hermes_cli/pairing.py` labels the pending value as a hint and no longer tells operators it is a pending code.

Dashboard:

- `web/src/pages/PairingPage.tsx` now displays `hash:<prefix>` only as a stored hash hint.
- Approval requires manually entering the real 8-character pairing code from the user's DM.
- The approve button is disabled until a code is entered.

Tests:

- Added `dingtalk.allow_all_users` YAML bridge coverage.
- Updated pairing tests to assert pending entries do not expose `code`.
- Fixed dashboard pairing endpoint test isolation so it does not read the operator's real `~/.hermes/pairing` state.

## Verification

Passed:

```text
.venv/bin/python3 -m pytest tests/gateway/test_pairing.py \
  tests/gateway/test_config.py::TestGetConnectedPlatforms::test_dingtalk_allow_all_users_bridged_from_yaml \
  tests/gateway/test_config.py::TestGetConnectedPlatforms::test_dingtalk_allow_all_users_env_wins_over_yaml \
  tests/hermes_cli/test_dashboard_admin_endpoints.py::TestPairingEndpoints -q

46 passed

npm --workspace web run typecheck
passed

npm --workspace web run build
passed
```

Runtime after restart:

```text
dashboard: PID 35098 listening on 127.0.0.1:9119
gateway:   PID 35040 listening on *:8644
status:    gateway_running=true, gateway_state=running
platforms: dingtalk=connected, webhook=connected
```

The new gateway startup no longer logs `No user allowlists configured`, confirming the DingTalk allow-all YAML bridge is active.

## Runtime Remediation

The existing DingTalk lockout entry was cleared from:

```text
~/.hermes/pairing/_rate_limits.json
```

Then the real one-time pairing code shown in DingTalk was approved through `PairingStore.approve_code()`.

Current pairing state after remediation:

```text
dingtalk-pending.json: {}
dingtalk-approved.json: user "$:LWCP_v1:$NoDPZEnaHdl5WfqKihxdt899LnbQ0x+0" ("弓淼")
_rate_limits.json: no _lockout:dingtalk entry; _failures:dingtalk=0
```

Dashboard status after remediation still shows the gateway running and DingTalk connected.
