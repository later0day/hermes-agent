# DingTalk AI Card 500 — content_key / Profile Inheritance Analysis

Date: 2026-08-20

Scope:

- `plugins/platforms/dingtalk/adapter.py` (`_current_card_content_key`,
  `_card_initial_param_map`, `_stream_card_content`, adapter `__init__`)
- `gateway/config.py` (DingTalk config→extra bridge whitelist, ~L1637-1647)
- `gateway/run.py` (`_profile_runtime_scope`, `_make_default_profile_message_handler`)
- `hermes_cli/config.py` (`load_config_readonly`, `get_config_path`)
- `hermes_cli/profiles.py` (`create_profile`)
- `/root/.hermes/config.yaml` + `/root/.hermes/profiles/*/config.yaml`

## Symptom

DingTalk AI Card replies failed with HTTP 500 (`unknownError`, `未知错误`) at
the **third** lifecycle step only — `streaming_update`. `create_card` and
`deliver_card` succeeded. Over three days: ~37 failures/day, **0 successes**
for every profile *except* `xcx`. Messages routed to `xcx` worked; everything
else (jb group, DMs falling to the default/root profile, etc.) 500'd.

## Root Cause — timing mismatch between template and content key

A single DingTalk adapter is created at gateway startup under the **root**
(default) profile scope, bound to the one DingTalk credential. It holds two
card fields with **different resolution timing**:

| Field | When resolved | Source |
|-------|---------------|--------|
| `_card_template_id` | **Once, in `__init__`** (adapter.py ~L408-412) | bridged from root config via the `gateway/config.py` whitelist → the custom template `26e55230-...schema`, whose data variable is named **`content`** |
| `_card_content_key` | **Dynamically, every streaming update** (adapter.py ~L3295 calls `_current_card_content_key()`) | `load_config_readonly()`, which reads `get_hermes_home()/config.yaml` — and `get_hermes_home()` is switched per inbound message to the **routed profile** by `_profile_runtime_scope` |

`card_template_id` **is** in the config→extra bridge whitelist, so it is fixed
globally to the root template. `card_content_key` is **not** in the whitelist,
so `_card_content_key_override` is empty and the key is resolved dynamically
per routed profile.

Because multiplex profiles load only `DEFAULT_CONFIG + profiles/<name>/config.yaml`
and do **not** inherit `/root/.hermes/config.yaml`, a profile that omits a
`dingtalk` section resolves `card_content_key` to empty and falls through to
`DEFAULT_AI_CARD_CONTENT_KEY = "msgContent"`.

Result: the request streams to `key="msgContent"`, but the template
`26e55230` only has a `content` variable. Key ≠ template variable →
DingTalk returns 500.

### Why xcx worked and the others didn't

`xcx` was the **only** routed profile that had its own `dingtalk` section with
`card_content_key: content`. Its dynamic resolution returned `content`, which
matched the fixed root template variable → success.

All other profiles (and the default/root fallback handler for unrouted
messages) had **no** `dingtalk` section → resolved to `msgContent` → 500.

Verified per-profile resolution (before fix):

```
root:    content_key='content'   template='26e55230-...'   -> match  OK
xcx:     content_key='content'   template='26e55230-...'   -> match  OK
jb:      content_key=''  -> DEFAULT 'msgContent'           -> mismatch 500
reverse: content_key=''  -> DEFAULT 'msgContent'           -> mismatch 500
(all other 13 profiles same as jb/reverse)
```

### Note on the whitelist gap

`gateway/config.py`'s DingTalk bridge whitelist omits `card_content_key`
(it lists `card_template_id` but not the key). That is a real gap, but it is
**not** the direct cause here: the effective resolution path is
`load_config_readonly()` under the profile scope, not the extra override. The
direct cause is the template-fixed / key-dynamic timing mismatch combined with
profiles not inheriting the root config.

## Fix applied (Option A — config alignment)

Added a top-level `dingtalk` section to the **15 profiles that lacked one**,
matching the default/root and `xcx` values:

```yaml
dingtalk:
  card_content_key: content
  card_template_id: 26e55230-9e7a-436e-9409-c4a5d8a2dcb8.schema
```

Profiles updated: `backend_engineer`, `ctf`, `customer_service`,
`devops_engineer`, `finance`, `frontend_engineer`, `jb`,
`logistics_returns_specialist`, `product_owner`, `qa_engineer`, `reverse`,
`room_cs_logistics_team_observer`, `room_dev_team_observer`,
`room_planned_room_observer`, `tech_support`.

Backups: `/root/.hermes/_bak_cardfix_20260820-111419/`.

**No gateway restart required.** `card_content_key` is read dynamically per
streaming update, and `load_config_readonly()`'s cache signature includes each
file's `(mtime_ns, size)` — editing a profile config invalidates its cache
entry, so the next card picks up the new value in the already-running process.
(This differs from code changes, which DO require a restart.)

Post-fix: card 500 count dropped from 37 (00:00–11:14) to 0 (after 11:14:32).

## Operational rule — new profiles MUST inherit the default profile

The **default profile is the root config** `/root/.hermes/config.yaml`
(`gateway/run.py:_make_default_profile_message_handler` falls back to
`Path(get_hermes_home())`, i.e. the root home, for unrouted messages). Root
already carries the authoritative card config
(`card_content_key: content` + `card_template_id: 26e55230-...schema`),
originally aligned with `xcx`.

`hermes profile create <name>` does **NOT** copy any config by default
(`hermes_cli/profiles.py` create: with no clone flags, `source_dir is None`
and no `config.yaml` is written — the profile relies on code `DEFAULT_CONFIG`
only, which has NO custom card template). `--clone` without a source clones the
**active** profile, not the default.

Therefore, to avoid regressing this 500, **new DingTalk-serving profiles must
be created by cloning the default profile**:

```sh
hermes profile create <name> --clone-all default
```

or, in the Dashboard, use the **"clone from default"** flow. Either path carries
the `dingtalk` card section forward so the new profile's dynamically-resolved
`card_content_key` matches the fixed root template variable (`content`).

A profile created empty (no clone, or `--clone` from an unconfigured active
profile) will resolve `card_content_key` to `msgContent` and reintroduce the
500. If you must create such a profile, add the two `dingtalk` lines above by
hand, or re-run the Option A alignment.

## Root-cause remediation (Option B — deferred, not applied)

The durable fix is to make `_current_card_content_key()` fall back to the
**default (root) profile's** `card_content_key` before the generic
`DEFAULT_AI_CARD_CONTENT_KEY`, and/or resolve `_card_template_id` dynamically
under the same scope so template and key are always read from one profile.
This removes the per-profile hand-copy requirement entirely. Deferred in favor
of Option A + the operational clone rule above; revisit if empty-profile
creation keeps regressing the 500.
