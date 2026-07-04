# DingTalk Channel Profile State Analysis

Date: 2026-06-20

## Symptom

The `xcx` profile gateway was running and `/api/status?profile=xcx` reported
DingTalk as connected, but the Dashboard Channels page still displayed the
DingTalk card as disabled.

## Cause

The Channels page reads `/api/messaging/platforms?profile=xcx`. In
profile-scoped mode that endpoint intentionally reads only the target profile's
own `config.yaml` and `.env` to avoid leaking the root profile's credentials.

The `xcx` profile had DingTalk credentials and root-level `dingtalk:` settings,
but `platforms.dingtalk` was an empty object. The profile-scoped payload logic
therefore set `enabled=false` and then overwrote the runtime `connected` state
with `disabled`, even though the gateway process was already connected.

## Fix

When the profile-scoped messaging payload has a live running gateway and the
runtime platform state is `connected`, the payload now reports the platform as
enabled and configured. This keeps the Channels card aligned with the actual
gateway runtime while preserving profile isolation for secrets.

The current `xcx` profile should also keep `platforms.dingtalk.enabled: true`
so future edits/restarts do not rely only on runtime state.

