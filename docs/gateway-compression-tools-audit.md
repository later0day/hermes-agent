# Gateway, Compression, and Tool Safety Audit

Date: 2026-06-20

Scope:
- `gateway/run.py` agent cache and session lifecycle
- `hermes_state.py` compression lineage
- tools safety boundaries and end-to-end test coverage

## Executive Summary

The gateway cache and compression lineage paths are intentionally conservative and have useful regression coverage around session rotation, cache invalidation, and concurrent compression. The highest-risk gap found in this pass was in the file patch tool: V4A `*** Move File: src -> dst` headers were supported by the parser but were not included in the `patch_tool` preflight path scan. That meant move sources and destinations did not receive the same traversal, sensitive-path, and cross-profile checks as `Update`, `Add`, and `Delete` headers.

## Gateway Agent Cache and Session Lifecycle

Key paths:
- `gateway/run.py` initializes `_agent_cache` as an LRU cache keyed by gateway session, with cache entries carrying agent, config signature, and current database message count.
- `_handle_message_with_agent` reuses a cached agent only when the config signature still matches and the cached message count matches the live `SessionDB.message_count`.
- After a successful turn and database flush, `_refresh_agent_cache_message_count` updates the cached baseline so the just-written turn does not self-invalidate prompt cache reuse.
- `_release_running_agent_state` is called in `finally` paths to avoid zombie running-agent entries after failures, stops, resets, or session switches.
- `_evict_cached_agent` distinguishes soft cache eviction from hard cleanup. Idle/cap eviction releases API clients but preserves terminal/browser/process state; session reset/resume/finalization performs harder cleanup.

Strengths:
- Config signature checks prevent reuse across model/provider/toolset/config changes.
- Message-count checks protect against most cross-process or out-of-band session writes.
- Run generation and unconditional release reduce races around `/new`, `/reset`, `/resume`, and `/stop`.
- Session expiry finalization prunes cached/running agent state and per-session gateway state.

Residual risks:
- The cache-entry comment still describes a 2-tuple in places, while runtime entries can be 3-tuples.
- The coherence guard is count-based. If an external rewrite preserves the same `message_count`, a cached agent can retain stale in-memory history. A future revision or transcript version would be stronger than count alone.
- Some cache tests assert helper contracts rather than a full gateway turn through `_handle_message_with_agent`, which is pragmatic but leaves a smaller integration gap.

## Compression Lineage

Key paths:
- `hermes_state.py` represents branches, compression continuations, and subagent/delegate children through `parent_session_id` plus marker/end-reason rules.
- Compression children are identified by parent `end_reason='compression'` and child `started_at >= parent.ended_at`.
- `get_compression_tip` follows the forward compression chain with a depth cap.
- `resolve_resume_session_id` resolves a requested session to its compression tip before falling back to empty-head descendant recovery.
- `list_sessions_rich` can project compression roots to tips while keeping branches and delegate children out of the default list.
- `agent/conversation_compression.py` serializes compression with `compression_locks`, ends the old session as `compression`, creates the child session, updates gateway/session context, resets flush cursors, and emits compression events.

Strengths:
- Compression locks prevent concurrent forks from the same pre-compression session.
- `end_session` preserves the first terminal reason, which keeps compression lineage stable.
- Resume and list paths are covered for parent-with-messages, compression tips, branches, delegates, and failure synchronization.

Residual risks:
- Lineage discrimination depends partly on timestamps. Depth caps prevent infinite loops, but malformed or migrated data can still produce ambiguous parent/child relationships.
- The empty-head fallback in `resolve_resume_session_id` is deliberately broader than strict compression lineage. It is useful for degraded histories and should remain covered with branch/delegate edge cases.

## Tool Safety Boundaries

Key paths:
- Terminal execution runs through command guards, hardline blocklists, sudo-stdin checks, dangerous-command approval, and gateway approval callbacks.
- File reads block known credential stores and device paths as defense-in-depth, but terminal access remains the true OS boundary.
- File writes and patches have an upper `file_tools.py` guard for sensitive system paths, Hermes config, cross-profile writes, sandbox mirrors, and container mirrors.
- `file_operations.py` has a lower write denylist, but it is narrower than the upper `patch_tool` sensitive-path guard.

Finding fixed from this audit:
- V4A `Move File` headers were parsed and executable by `tools/patch_parser.py`, but `patch_tool` only scanned `Update`, `Add`, and `Delete` headers before dispatching to `patch_v4a`.
- The fix extracts V4A paths via `parse_v4a_patch`, checks both move source and move destination, and fails closed on malformed V4A patches before dispatch.
- Regression coverage now includes direct `patch_tool` checks for move source traversal, move destination traversal, sensitive move destination, and dispatcher-level `handle_function_call("patch", ...)` coverage.

Recommended follow-ups:
- Add a transcript revision or last-message id to gateway cache coherence so equal-count rewrites cannot reuse stale cached agents.
- Add a small gateway-level cache-hit integration test that exercises `_handle_message_with_agent` reuse and live `message_count` invalidation together.
- Keep file read guard tests framed as defense-in-depth, not a hard security boundary, because terminal can still read files as the same OS user.
