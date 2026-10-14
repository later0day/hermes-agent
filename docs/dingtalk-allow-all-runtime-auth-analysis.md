# DingTalk allow-all runtime auth analysis

Date: 2026-06-20

## Symptom

The `xcx` profile has:

```yaml
dingtalk:
  allow_all_users: true
```

but the running gateway still logged:

```text
WARNING gateway.run: Unauthorized user: ... on dingtalk
```

for a DingTalk sender.

## Findings

- The `xcx` profile config was correct: `dingtalk.allow_all_users: true`.
- The running gateway was using `--profile xcx`.
- The message-time authorization path in `gateway/authz_mixin.py` only checked environment variables:
  - `DINGTALK_ALLOW_ALL_USERS`
  - `GATEWAY_ALLOW_ALL_USERS`
- `gateway.config.load_gateway_config()` does bridge `dingtalk.allow_all_users` into `DINGTALK_ALLOW_ALL_USERS`, but not every runtime config read goes through that bridge. `gateway.run._load_gateway_config()` returns raw YAML and does not mutate `os.environ`.
- That made the authorization chain brittle: a correct `config.yaml` value could be present in `GatewayConfig.platforms[dingtalk].extra` while the final authorization check still denied the user because the env bridge was absent.

## Fix

`gateway/authz_mixin.py` now treats `PlatformConfig.extra.allow_all_users` as a first-class authorization source.

Environment variables still keep precedence:

- `DINGTALK_ALLOW_ALL_USERS=true` allows.
- `DINGTALK_ALLOW_ALL_USERS=false` does not allow, even if config says `allow_all_users: true`.
- If the platform env var is unset, `dingtalk.allow_all_users: true` authorizes the sender.

## Regression Coverage

Added tests in `tests/gateway/test_config_driven_access_policy.py`:

- `test_platform_config_allow_all_authorizes_without_env_allowlist`
- `test_platform_allow_all_env_false_overrides_config_allow_all`

## Remaining Separate Issue

This fixes DingTalk sender authorization. It does not fix DingTalk AI Card delivery. AI Card delivery is still blocked by DingTalk API IP allowlist errors:

```text
Forbidden.AccessDenied.IpNotInWhiteList
request ip=2409:8a28:ec1:aa10:9880:6e1a:3c25:bc02
```

That requires adding the observed IPv6 address to the DingTalk app allowlist or forcing outbound traffic through an allowlisted IPv4 route.
