# DingTalk Config Page Visibility Analysis

Date: 2026-06-20

## Symptom

The Dashboard Config page no longer showed DingTalk settings, even though the
user config still contained a root `dingtalk:` section with behavior and AI Card
settings.

## Cause

The Config page renders only fields returned by `/api/config/schema`. That
schema is generated from `hermes_cli.config.DEFAULT_CONFIG`, and the default
config did not contain a root `dingtalk` section. The frontend still had Chinese
labels for `dingtalk.*`, but labels do not create schema entries, so search and
category navigation could not surface those fields.

There was also a runtime gap: some root `dingtalk.*` settings were only bridged
to env vars, while AI Card and enterprise fields such as `card_template_id`,
`app_code`, `corp_id`, `agent_id`, and `reply_at_sender` need to reach
`PlatformConfig.extra` before the DingTalk adapter is initialized.
`card_content_key` is intentionally left out of that bridge because the adapter
already reads it dynamically from root config for each card update.

## Fix

- Added the root `dingtalk` behavior and AI Card settings back to
  `DEFAULT_CONFIG`, which makes `/api/config/schema` expose them.
- Added `dingtalk` to the Config page category order and icon map.
- Bridged root `dingtalk.*` runtime fields into DingTalk `PlatformConfig.extra`
  during gateway config loading, except `card_content_key`, which stays dynamic.
- Kept DingTalk connection credentials on the Channels page:
  `DINGTALK_CLIENT_ID` and `DINGTALK_CLIENT_SECRET`.

## Verification

- Added schema coverage for `dingtalk.allow_all_users` and
  `dingtalk.card_template_id`.
- Added gateway config coverage that root `dingtalk.*` fields reach
  `PlatformConfig.extra` and `DINGTALK_ALLOW_ALL_USERS`.
