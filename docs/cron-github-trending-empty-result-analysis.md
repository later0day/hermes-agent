# Cron GitHub Trending Empty Result Analysis

## Scope

Job:

- ID: `467f40c1f4e0`
- Name: `GitHub AI 热门开源项目 Top10`
- Profile: `xcx`
- Run inspected: `2026-06-20 07:14:38`
- Pre-run script: `/Users/later0day/.hermes/profiles/xcx/scripts/github_ai_trending.py`
- Output file: `/Users/later0day/.hermes/profiles/xcx/cron/output/467f40c1f4e0/2026-06-20_07-14-42.md`

## Evidence

The cron run was not dropped by the scheduler or DingTalk delivery path.

- `agent.log` shows the job completed successfully at `2026-06-20 07:14:42`.
- `agent.log` shows DingTalk delivery via the live adapter at `2026-06-20 07:14:43`.
- `agent.log` also shows `AI Card created (streaming): hermes_90bd617f602f`.
- The session `cron_467f40c1f4e0_20260620_071438` exists in `state.db` with `message_count=2`, `tool_call_count=0`, `api_call_count=1`, and `output_tokens=165`.
- The assistant message stored for that session is exactly:

```text
⚠️ 暂时无法获取 GitHub Trending 数据，请稍后重试。
```

The output file confirms the pre-run script produced this context:

```text
⚠️ 本周 GitHub Trending 未找到 AI 相关项目，请检查网络或稍后重试。
```

The model then followed the cron prompt instruction:

```text
如果脚本输出为空或报错，回复："⚠️ 暂时无法获取 GitHub Trending 数据，请稍后重试。"
```

So the visible "empty" result is actually the fallback response generated from
the script output. It was not an empty LLM response.

## Script Behavior

The profile script fetches three GitHub Trending pages with `curl`:

- `https://github.com/trending?since=weekly`
- `https://github.com/trending/python?since=weekly`
- `https://github.com/trending/typescript?since=weekly`

When rerun shortly after the failed cron run, the script reproduced the failure:

```text
[WARN] Failed to fetch https://github.com/trending?since=weekly: curl failed (exit 28):
[WARN] Failed to fetch https://github.com/trending/python?since=weekly: curl failed (exit 28):
[WARN] Failed to fetch https://github.com/trending/typescript?since=weekly: curl failed (exit 28):
⚠️ 本周 GitHub Trending 未找到 AI 相关项目，请检查网络或稍后重试。
```

Exit `28` is a curl timeout. Later retries succeeded and produced the full Top
10 report, which confirms the parser and cron configuration can work when the
GitHub fetch succeeds.

Address-family checks were inconsistent:

- One `curl -4` request timed out.
- One `curl -6` request returned HTTP 200 and a page with `<article>` entries.
- A later default curl resolved `github.com` to IPv4 `20.205.243.166` and also
  succeeded.

This points to an intermittent GitHub/network fetch failure, not a stable
parser break or a stable "IPv4 always bad" condition.

## Root Cause

`github_ai_trending.py` collapses two different states into the same successful
stdout:

1. GitHub pages were fetched but contained no AI-related repositories.
2. All GitHub page fetches failed or timed out.

In both cases it prints:

```text
⚠️ 本周 GitHub Trending 未找到 AI 相关项目，请检查网络或稍后重试。
```

and exits with code `0`.

`cron/scheduler.py` treats an exit code `0` from the pre-run script as a
successful script run, injects stdout as `## Script Output`, and then runs the
agent. The job prompt tells the agent to return the short fallback when the
script output is empty or indicates an error, so the final response was the
35-character fallback warning.

That is why the cron status says `ok` and delivery says successful even though
the content is effectively empty.

## Non-Causes

- Not a DingTalk delivery failure: the live adapter reported successful
  delivery and AI Card creation.
- Not a missing cron target: the job still resolves to the DingTalk origin
  `cidUKHyy+TSBvzQY6P34TpjPA==`.
- Not a model/tool loop issue: the run had `tool_call_count=0` and ended after
  one API call with a normal `stop`.

## Recommended Fix

Fix the profile script first, not the DingTalk adapter.

The script should distinguish "no AI repos after at least one successful fetch"
from "all fetches failed":

- Track whether at least one Trending page fetched successfully.
- If all fetches fail, exit non-zero with the per-URL fetch errors on stderr.
- Add small retry/backoff around each URL.
- Optionally try address-family fallback per URL, e.g. default curl, then `-6`,
  then `-4`, without making a global Hermes networking special case.
- Optionally cache and resend the last good report when the fetch fails, or
  emit `[SILENT]` if transient fetch failures should suppress delivery.

This makes the cron failure explicit and prevents a successful-looking run from
delivering a near-empty fallback report.

## Applied Change

The profile script was updated at:

```text
/Users/later0day/.hermes/profiles/xcx/scripts/github_ai_trending.py
```

Runtime behavior now:

- Uses local proxy by default: `http://127.0.0.1:10808`.
- Allows override with `GITHUB_TRENDING_PROXY`.
- Passes the proxy to curl via `--proxy`.
- Adds curl retry/backoff for transient fetch failures.
- Tracks whether any Trending source fetched successfully.
- Exits non-zero when all GitHub Trending fetches fail, so the scheduler
  injects `Script Error` context instead of normal `Script Output` for a total
  fetch outage.

Verification:

- The real script produced a full Top 10 report through the local v2rayN/xray
  proxy.
- `py_compile` passed for the edited script.
- Running with `GITHUB_TRENDING_PROXY=http://127.0.0.1:9` fails with curl
  connection errors and exit code `1`, confirming the script is actually using
  the configured proxy path instead of silently falling back to direct network.
