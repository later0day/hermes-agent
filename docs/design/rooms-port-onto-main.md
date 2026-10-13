# Rooms / agent-team port onto `integration/rebase-onto-main`

Branch `integration/rooms-on-main` = `integration/rebase-onto-main` (upstream 2026-10-12 + re-ported fork features)
plus the 48 Rooms / agent-team commits from `integration/port-fork-features`, cherry-picked in order with `-x`,
followed by post-port fixes. This file records every place where the port had to *adapt* a fork commit to the
new upstream, so the reasoning survives the `/tmp` worktree it was done in.

## Structural moves (upstream refactors)

- `hermes_cli/web_server.py` was split into `hermes_cli/web_routers/*`: all 18 `/api/rooms*` routes and the
  team-creation request models live in the fork-owned `hermes_cli/web_routers/rooms.py`, registered with
  `include_router` before `mount_spa`.
- `gateway/run.py` was split into mixins: the `/room` handler is registered via `run_busy._PLAIN_COMMANDS`;
  the C2 mirror watcher via `run_startup._PRE_RECONNECT_WATCHERS`; C2 inbound routing lives in the fork-owned
  `gateway/room_mirror_watcher.py` (`GatewayRoomMirrorMixin`) with a 5-line hook in `run_inbound._handle_message`.
- `gateway/hosted_room_discussion.py` was compacted / table-driven upstream; decider v1/v2, C4/C5, cross-process
  and roster-rail logic were re-applied by intent onto the new code (role validated in `_validate_member`,
  `terminals` became a `{coordinate: event}` dict, decisions go through upstream's `decide()` partial).
- `HostedRoomService._owned_room` became `_owned_authority` (same check, returns `(gateway_id, epoch)`).
- Hosted-room storage moved from `state.db` to `shared-state.db`; upstream's legacy import copies the
  `hosted_room*` tables (verified byte-identical for the 4 live rooms). It does **not** copy the fork sidecars
  (`room_notify_subs`, `room_task_dag`, `room_task_deps`, fork `hosted_room_pending_actions`); they were empty
  on the live install at port time.

## Behaviour changes made during the port (all tested, negative-controlled)

- **F1 decider whitelist**: profile resolution uses `hermes_constants.profile_name_for_home` (the default home's
  basename `.hermes` is not the id `default`, so a default-profile decider failed open); the new upstream
  `methods_tools._refresh_live_sessions` (reload.mcp / plugin activation) re-applies the filter instead of handing
  a decider its full toolset back. Fail-closed for `bot_room` sessions was kept: only local hosted-room turns
  build through `tui_gateway._make_agent` (peer turns run under the api_server `RoomExecutionPolicy`).
  Tests: `tests/tui_gateway/test_room_decider_toolset_enforcement.py`.
- **Anti-injection**: upstream's member control-frame relabel and the fork's CC three-layer wrapper are layered
  (relabel -> tag escape -> XML wrap) and the `summary` attribute is derived from the guarded text.
- **Failed turns**: the room log stores the classified `reason_code` as `error`, never raw provider output.
- **DAG sweep**: reads the room log only when in_progress DAG dispatches exist (keeps upstream's bounded replay).
- **Deferred turns**: the fork's blanket "keep any discussion with a deferred turn pending" rule was dropped; it
  contradicted upstream's settled-retry contract, which already recovers compacted sources. The task-dag-anchor
  deferral is kept.
- **C2 inbound hot path**: unbound chats are rejected from an in-memory index so `_handle_message` keeps its
  no-await-before-claim guarantee (`test_session_race_guard`). Test: `tests/gateway/test_room_inbound_index.py`.
- **Rooms page request storm** (pre-existing in the fork UI): `useRoomsWorkspace` reads inline callbacks through
  refs; one open tab previously issued ~17-20k `GET /api/rooms` per 5 s.
- `/room`: documented in `reference/slash-commands.md`; English fallback added to the 15 non-en/zh locales
  (`TODO(i18n)`); demoted to `/hermes room` on Slack (50-slash cap); `CommandDef.subcommands` completed.

## Deliberately not carried

- `agent/anthropic_adapter.py` / `anthropic_endpoints.py` Claude-Code client-identity injection for loopback
  proxy gateways: unrelated to Rooms, accidentally swept into fork commit 7fcb08f6ec; no live profile used it.

## Per-commit resolution log

- c0caa91fb1 /room command: handler table moved upstream to gateway/run_busy.py _PLAIN_COMMANDS -> added "room" there (run.py kept as HEAD). slash_commands: dropped stale _handle_status_command context lines (moved out upstream). zh.yaml: kept upstream kanban reformat + appended room block; zh/en key parity verified.
- 233f4e497b inspector: upstream split web_server.py into web_routers/* -> kept HEAD web_server.py, moved the 3 /api/rooms routes into NEW hermes_cli/web_routers/rooms.py (router=APIRouter), registered via include_router before mount_spa. api.ts: kept both type blocks (shared closing brace).
- fa6ecd61b1 /room add|remove: upstream renamed HostedRoomService._owned_room -> _owned_authority (same check+error, returns (gw,epoch)); update_members now calls _owned_authority; send() kept upstream one-line signature.
- a6466a91a7 decider v1: upstream refactored discussion.py (+453/-1118, compact style, _validate_member extracted). Re-applied intent: role validated in _validate_member (decider must be local), at-most-one-decider in validate_roster, star filter in _unaddressed_member_mentions, _decider() helper, decider/worker prompt rules, round-0 decider-only. role NOT in _member_digest (task identity unchanged).
- 27c80c0891 C2 mirror: startup moved to gateway/run_startup.py -> added _room_mirror_watcher to _PRE_RECONNECT_WATCHERS (after kanban watchers, supervised name unchanged); GatewayRoomMirrorMixin added to GatewayRunner bases + import. Verified MRO/watcher table via real import; mirror_db + lifecycle/reconnect/scale-to-zero tests pass.
- bef1b3bbdd C2 inbound: _handle_message moved to gateway/run_inbound.py -> 5-line hook inserted at the equivalent point (after idle command dispatch, before telegram lobby check); _maybe_route_inbound_to_room + sentinel moved into fork-owned gateway/room_mirror_watcher.py mixin (renamed INBOUND_ROOM_NOT_ROUTED) instead of run.py. 'not command' -> event.get_command(). service.send gained actor_id on upstream signature. Verified: actor id unconstrained by discussion validator; unbound source falls through (real room_mirror_db).
- 83846af9c0 decider v2: discussion.py auto-merged (rounds 3->5, regex [0-4]); test conflict took fork's drive-to-bounded loop over upstream's range(MAX*2). No other round-cap assumptions in py/ts (git grep).
- ae2ceb3b7d C4: conflict only because upstream table-drove plan_publication; kept upstream plan_publication, appended ProjectedTask + project_task_dag unchanged (helper signatures compatible). 81 discussion+dag tests pass.
- 11bfc2cafd C5 [BEHAVIOR ADJUSTED]: upstream prepare_room now replays a bounded policy-checkpoint snapshot (test_policy_checkpoint_bounds_replay asserts read count). C5's _sweep_completed_dag_dispatches read the FULL log via self._events() every tick (802 vs 401 rows). Changed sweep to take snapshot.events: reply is published + re-snapshotted earlier in the same pass and its thread only leaves the snapshot after room.activity syncs next pass, so the sweep still sees it. Both upstream bounded-replay test and C5 closed-loop test pass; negative control (empty events) fails C5 test.
- 77de632b88 F1 decider whitelist [SECURITY HARDENED]:
  * execution_policy: kept upstream formatting, re-added ORCHESTRATION_ONLY_TOOLSETS / orchestration_only_toolsets / __all__.
  * server._make_agent: reuse upstream's session/platform locals; filter applied to enabled_toolsets; upstream disabled_toolsets kept.
  * FIX: profile resolution Path(profile_home).name -> hermes_constants.profile_name_for_home (default home basename ".hermes" != "default" -> whitelist failed OPEN for a default-profile decider).
  * FIX: NEW upstream bypass methods_tools._refresh_live_sessions (reload.mcp / plugin activation) re-resolved enabled toolsets from config for every live agent, handing a decider its full toolset back. Now re-applies _room_decider_toolset_filter.
  * create_room persists role (upstream compact style). New tests tests/tui_gateway/test_room_decider_toolset_enforcement.py (default-profile decider, live reload); both negative-controlled. Upstream mcp-reload/bot-chat refresh tests still green.
- 621ca73284 F1 pending_title: verified upstream session.create still stashes pending_title; folded into compact filter. Tests (incl. pending_title assertion) pass.
- a0c828a9ea CC anti-injection [MERGED WITH UPSTREAM GUARD]: upstream added its own member control-frame relabel ([System: -> [member-quoted System:). Layered both: relabel -> teammate-tag escape -> XML wrap; summary attribute now derived from the GUARDED text (raw summary would bypass both guards). New test test_member_text_guards_also_cover_the_teammate_summary_attribute (negative-controlled). 62 discussion tests pass incl. upstream control-frame test.
- 7fcb08f6ec C9 inspector: 12 routes ported into web_routers/rooms.py (web_server.py kept upstream). EXCLUDED unrelated agent/anthropic_adapter.py + anthropic_endpoints.py changes (Claude-Code client-identity spoofing for loopback proxy gateways) that were accidentally swept into this fork commit by git add -A; not Rooms, not on rebase base, and no live profile uses a loopback Anthropic base_url. All 11 service methods exercised on a real room on new upstream.
- 58670709dd harden runtime:
  * inbound hardening (fail-closed once bound, identifier-safe room_thread_id) applied to the moved method in room_mirror_watcher.py; mirror cursor hardening auto-merged.
  * prune: 4 fork sidecar tables added to upstream _DEPENDENT_TABLES (upstream already table_exists-guards).
  * service: durable pending actions (hosted_room_actions) in upstream style; approval clear goes through _set_pending_action outside lock.
  * DAG sweep: ADOPTED this commit's design (only read the log when in_progress dispatches exist) and REVERTED my C5 snapshot-param variant: more robust across restarts/missed passes; upstream bounded-replay test still green.
  * F1 now FAIL-CLOSED for bot_room sessions. Verified safe on new upstream: bot_room source is only produced by local hosted-room turns (titled "Group: <id>"); peer turns run via api_server /v1/runs under RoomExecutionPolicy and never reach tui _make_agent; desktop Bot Mode does not use bot_room. Filter rewritten compact (pending_title + profile_name_for_home + fail-closed).
  * test fix: HostedRoomServerRPC.submit gained required member_id upstream (plugin hook); fork e2e test updated.
- 5866dd273b roster rail: decider whitelist dropped 'delegation' (auto-merged); prompt rules re-applied compact; resolve_mentions punctuation peel auto-merged; my F1 enforcement test updated to [bot_room, clarify, todo].
- 70b2f3fb27 cross-process: re-applied task-dag anchor bypass + deferred_turn idle decisions onto upstream compact plan_next_task (via decide() partial); terminals converted from set to {coordinate: event} dict (membership semantics unchanged). Pending action created_at preserved. 208 room tests pass.
- cf0c8ddd7d workspace: /api/rooms enrichment + /api/rooms/{id}/workspace ported into web_routers/rooms.py (logger via web_routers._common). Real TestClient route tests + views tests pass.
- 6b96cd4318 research-team: [SECURITY] failed-turn log payload 'error' now stores the classified reason_code instead of raw provider error text (may carry prompts/URLs/credentials; rendered in dashboard views). Applied to upstream's table-driven _failed_effects. Upstream turn.failed tests + room suite (180) green; desktop group-turns reads driver inflight state, not this payload.
- 8de10f7322 team creation: POST /api/rooms + POST /api/rooms/{id}/messages and RoomMemberInput/RoomCreateInput/RoomMessageInput ported into web_routers/rooms.py (fork-owned; upstream web_models untouched). service.send recipient check uses self._room() roster since _owned_authority returns only (gw,epoch). Real TestClient route tests pass.
- [post-pick fix] removed fork's blanket deferred_turn hold (contradicted upstream settled-retry contract, which already recovers compacted sources); task-dag anchor deferral kept.
