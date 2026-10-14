# Upstream/Main Merge Assessment

Date: 2026-08-24

Local branch (product work): `main` @ `a9b91dfb1f`

Upstream target: `upstream/main` @ `057dcdf236` (tip 2026-08-24)

## Divergence

- Merge-base: `f293e7206b` (the previous merge's upstream tip).
- Upstream ahead by **386** commits (358 non-merge) in this window.
- Real textual conflicts (1 file only):
  - `tools/environments/docker.py` (1 conflict block — same bug, two fixes)
- 26 files were touched by BOTH sides since the merge-base; 25 auto-merged
  cleanly, only docker.py collided.
- Agent-room subsystem (`gateway/agent_room_*.py`, `tools/room_*_tool.py`,
  `tools/environments/agentproxy.py`, `web/src/pages/RoomsPage.tsx`) is 100%
  ours — it never existed upstream, so the merge does not touch it
  (`git cat-file -e upstream/main:gateway/agent_room_router.py` → absent).
  NOTE: an early diff `a9b91dfb1f..upstream/main` mislabeled our own
  additions as "deletions" because a9b91dfb1f is OUR merge commit; the true
  merge-base is f293e7206b. There is no upstream subsystem removal.
- Merged set compiles: 262 touched .py files all pass `py_compile`.
- Merge stat: 514 files changed, +44,818 / -6,163.

## Executive Direction

Same shape as prior merges — NOT "ours wins" / "theirs wins":

1. Take upstream wholesale for independent official areas: desktop (88
   commits: X11/Wayland HUD, group-chat tiles, torn-bundle guards),
   cron liveness surfacing, model-metadata/pricing (max_tokens vs context
   window fix), gateway.ping heartbeat + WS reconnect, bot-relay Windows
   path/injection hardening, batch_runner discard tombstones.
2. Preserve local product work: agent-room (M1-M4 + no-match escalation),
   DingTalk turn_status_card / AI-card work, dashboard-profile unification,
   agentproxy backend, per-profile terminal scope, MALLOC_ARENA_MAX + jb
   apiserver-key + shutdown_forensics LoadState guard.
3. Hand-compose the single real conflict (docker.py) — see below.
4. Semantically review the auto-merged runtime-boundary files even though
   Git merged them textually (done — see Verification).

## High-Value Upstream Commits For This Fork

Security / multiplex-isolation (directly reinforces our profile model):
- `2912c36aa4` fix(gateway): stop multiplex allowlist leak + bot-relay
  `python -c` injection. `_auth_env` fell through to os.environ on a scoped
  miss → one profile inherited another's allowlists/allow-all. WE WANT THIS.
- `7befc1d2dd` / `d7e4204e77` fix(gateway): route platform authorization
  reads (weixin/yuanbao/signal/wecom) through the profile secret scope.
  Under multiplex these adapters read allowlists via raw os.getenv →
  fail-closed (drop all DMs) or fail-open (inherit default's ALLOW_ALL).
  Reinforces our isolation work. Auto-merged clean.

ctfbox-relevant (docker backend + container_persistent:true):
- `fb381e8055` fix(docker): sanitize session-key task_id used as sandbox
  path. Colon-delimited session keys made `docker run` exit 125 "too many
  colons" on first tool call. THIS IS THE CONFLICT — we have a parallel
  local fix `6a1ac785c8`.

Robustness:
- `4b659f0e33` retry failed session DB opens; `80cec2785d` preserve routing
  state across recovery; `ee8a66233f` preserve replacement handles across
  close races.
- auth: `37411f349a` rotate creds after 401/429; `c527b2c0a4`/`030edf9774`
  provider-pool key canonicalization (relevant to DASHSCOPE/multi-provider).

## Conflict Resolution (compose / pick-better)

- **`tools/environments/docker.py`** (1 conflict): TAKE UPSTREAM.
  Both sides fix the identical bug (colon in `session:<key>` task_id breaks
  the `-v` bind-mount spec → exit 125). Upstream's `sanitize_task_id_for_path`
  (in tools/environments/base.py) is strictly superior to our local
  `_RE_UNSAFE_PATH_SEG.sub("_", task_id)`:
    * ours is NOT injective — `a:b` and `a_b` collapse to the same sandbox
      dir, leaking one session's /root into another's container;
    * upstream appends a sha256 digest of the original id on any rewrite, so
      collisions can't happen, and returns already-safe ids verbatim (so the
      shared `default` sandbox + RL/benchmark ids keep their existing dir);
    * upstream also guards Windows trailing-dot/space aliasing and length.
  Resolution: `git checkout --theirs tools/environments/docker.py`, drop our
  now-redundant `_RE_UNSAFE_PATH_SEG`. Our commit 6a1ac785c8 is superseded.

## Verification (auto-merged boundary files, semantically checked)

- `gateway/authz_mixin.py`: upstream's `_auth_env` scoped-miss no-fallback
  fix present (line 59-63) AND our chat-scoped allowlist comments intact.
- `gateway/platforms/weixin.py`: `_wx_secret` scoped reads present.
- `gateway/platforms/signal.py`: `_sig_secret` helper present.
- `gateway/shutdown_forensics.py`: our LoadState guard intact
  (`if load_state is not None and load_state != "loaded"`, line 398).
- agent-room subsystem: zero diff from merge (untouched).
- 262 merged .py files compile.

## Risk

- LOW. Single trivial conflict with a clear "take upstream" verdict.
- Post-merge live E2E required (per standing instruction: real HTTP
  round-trips, not just unit tests), with focus on:
    * ctfbox docker backend first-tool-call (the fb381e8055 path) — verify
      persistent sandbox dir resolves and /root state is stable;
    * multiplex per-profile authz still fail-closed (jb/ctfbox/xcx);
    * no regression in agent-room DingTalk routing.
