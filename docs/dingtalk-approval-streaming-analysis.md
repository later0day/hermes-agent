# DingTalk Approval and Streaming Analysis

Date: 2026-06-20

Scope:
- DingTalk conversation `/approve` and `/deny` approval flow
- DingTalk streaming / staged replies
- DingTalk-specific risks around approval reachability and AI Card finalization

## Summary

DingTalk does not currently implement a DingTalk-specific interactive approval button handler. Dangerous-command approval in DingTalk conversations is controlled by the shared gateway approval flow:

1. `tools/approval.py` queues a blocking approval for the current `session_key`.
2. `gateway/run.py` registers a per-session notify callback while the agent turn is running.
3. Because `DingTalkAdapter` has no `send_exec_approval`, the notify callback falls back to sending a plain text prompt that tells the user to reply `/approve`, `/approve session`, `/approve always`, or `/deny`.
4. The inbound DingTalk reply is converted to a normal `MessageEvent`.
5. `gateway/run.py` routes `/approve` and `/deny` around the normal busy-agent interrupt path.
6. `gateway/slash_commands.py` resolves the oldest or all pending approvals through `tools.approval.resolve_gateway_approval`.

DingTalk streaming is controlled by the generic `GatewayStreamConsumer`, but the visible platform behavior is implemented through DingTalk AI Cards:

1. Gateway creates a stream consumer when streaming or interim assistant messages are enabled and the adapter reports `SUPPORTS_MESSAGE_EDITING`.
2. DingTalk only reports edit support when an AI Card template and card SDK are available.
3. The first streaming send creates and delivers an AI Card.
4. Incremental updates call DingTalk `streaming_update` with `is_full=True`.
5. The final update must pass `is_finalize=True` to close DingTalk's streaming/loading state.

## Incoming Media Error Regression

Observed log:

```text
TypeError: MessageEvent.__init__() got an unexpected keyword argument 'media_errors'
```

Cause: `DingTalkAdapter._on_message` preserves media download/cache failures as `media_errors` so messages with failed attachments are not silently dropped. The shared `MessageEvent` dataclass did not expose that field, so any DingTalk message that reached this media-error path crashed before `handle_message(event)`.

Fix:

- `gateway/platforms/base.py` now defines `MessageEvent.media_errors`.
- `gateway/run.py` now turns media errors into an agent-visible placeholder like `[Media attachment unavailable: ...]`.
- The normal inbound path, queued follow-up path, and interrupt path all preserve that placeholder.
- `tests/gateway/test_dingtalk.py` covers DingTalk media-error event construction.

Validation:

- `tests/gateway/test_dingtalk.py`: 69 passed.
- `tests/gateway/test_native_image_buffer_isolation.py`, `tests/gateway/test_queue_consumption.py`, and `tests/gateway/test_media_metadata_contract.py`: 30 passed, 2 skipped.
- Dashboard `/api/status` reported `gateway_running=true`, `gateway_state=running`, `gateway_pid=32863`, and `dingtalk.state=connected`.
- `lsof` showed PID `32863` listening on `*:8644`.
- Direct HTTP probes to `127.0.0.1:8644` returned `404 Not Found`, which is expected for this webhook server when no HTTP webhook routes are configured; it still proves aiohttp is accepting connections on the gateway port.

Related non-error warning:

- `hermes_plugins.raft_platform.adapter: [raft] raft CLI not found in PATH` means the optional raft platform plugin was discovered but the `raft` command is not installed. It is unrelated to DingTalk message dispatch, approvals, streaming, or media handling.

## Approval Control Points

### Auto-Approval Switches

There is no DingTalk-only auto-approve switch. DingTalk uses the same shared approval subsystem as other gateway platforms.

Available auto-approval mechanisms:

- `/yolo` in a DingTalk conversation toggles session-scoped approval bypass through `gateway/slash_commands.py:_handle_yolo_command`. This affects only the current gateway session key.
- `approvals.mode: off` in `config.yaml` globally skips ordinary approval prompts. The default config documents this as equivalent to `--yolo`.
- `approvals.mode: smart` asks the auxiliary LLM to auto-approve low-risk flagged commands; otherwise it escalates to the normal `/approve` prompt.
- `/approve session` stores the matched dangerous pattern for the current session, so later commands matching the same pattern do not prompt again.
- `/approve always` stores the matched dangerous pattern permanently.

Not auto-approval for DingTalk chat:

- `approvals.cron_mode: approve` is for cron/headless jobs. DingTalk messages run in gateway context and use the interactive gateway approval path.

Non-bypassable floors:

- Hardline commands such as disk-wipe / shutdown classes are blocked before `/yolo`, `approvals.mode: off`, smart approval, or cron approve mode.
- The `sudo -S` password-piping guard also runs before `/yolo`, `approvals.mode: off`, and smart approval when `SUDO_PASSWORD` is not configured.

Dashboard configuration:

- Open the dashboard profile you want to edit, for example `http://127.0.0.1:9119/config?profile=xcx`.
- Go to `Config`, search for `approvals.mode`, and choose one of:
  - `manual`: prompt every time a dangerous command needs approval.
  - `smart`: use the auxiliary LLM to approve low-risk commands and escalate risky ones to `/approve`.
  - `off`: globally skip ordinary approval prompts, equivalent to yolo mode.
- Older dashboard schema labels `ask` / `yolo` / `deny` were not the approval subsystem's canonical values. The effective global values are `manual` / `smart` / `off`; `deny` belongs to `approvals.cron_mode`, not interactive DingTalk approvals.
- Save the config. For a running gateway, restart/reload the gateway process so newly created conversations definitely read the updated profile config.
- If you only want the current DingTalk conversation to auto-approve temporarily, send `/yolo` in that DingTalk session instead of changing dashboard config.

Relevant code:
- `gateway/slash_commands.py:2330`
- `tools/approval.py:1036`
- `tools/approval.py:1062`
- `tools/approval.py:1388`
- `tools/approval.py:1397`
- `tools/approval.py:1408`
- `tools/approval.py:1475`
- `tools/approval.py:1483`
- `tools/approval.py:1705`
- `tools/approval.py:1746`
- `hermes_cli/config.py:2148`

### DingTalk Inbound Gate

File: `gateway/platforms/dingtalk.py`

`DingTalkAdapter._process_message` first applies DingTalk-specific gates before a message can become a gateway command:

- dedup by DingTalk message id
- `allowed_users`
- group mention / wake-word / `allowed_chats` / `free_response_chats`
- session webhook capture
- source construction with `chat_id`, `chat_type`, `sender_id`, and `sender_staff_id`

Relevant code:
- `gateway/platforms/dingtalk.py:988`
- `gateway/platforms/dingtalk.py:1005`
- `gateway/platforms/dingtalk.py:1017`
- `gateway/platforms/dingtalk.py:1033`
- `gateway/platforms/dingtalk.py:1063`

Impact: a DingTalk `/approve` reply only works if this inbound gate lets the message reach `handle_message(event)`. In group chats that require mentions, the approval reply may also need to satisfy the mention/wake-word rules unless the chat is allowlisted or mention requirement is disabled.

### Session Key Boundary

Files:
- `gateway/run.py`
- `gateway/session.py`
- `gateway/platforms/base.py`

`GatewayRunner._session_key_for_source` delegates to `SessionStore._generate_session_key(source)`, which ultimately uses `build_session_key`.

For normal group/channel sessions, `build_session_key` includes the participant id when `group_sessions_per_user` is enabled. DingTalk source construction sets:

- `user_id=sender_id`
- `user_id_alt=sender_staff_id`

Relevant code:
- `gateway/run.py:2944`
- `gateway/session.py:646`
- `gateway/session.py:711`
- `gateway/session.py:727`
- `gateway/platforms/base.py:4742`

Impact: by default, ordinary DingTalk group approvals are usually per-user because the session key includes the DingTalk sender identity. If group sessions are configured to be shared, or if a shared thread model is used, approval authority becomes shared by whoever can send a message into that same session key.

### Approval Prompt Creation

File: `gateway/run.py`

During each agent turn, `_handle_message_with_agent` registers `_approval_notify_sync` through `register_gateway_notify(session_key, callback)`.

`_approval_notify_sync` tries button-based approval first when the adapter class implements `send_exec_approval`. DingTalk does not implement that method, so it falls back to a plain text prompt using the adapter's `typed_command_prefix`. DingTalk inherits the default prefix `/`.

Relevant code:
- `gateway/run.py:15545`
- `gateway/run.py:15556`
- `gateway/run.py:15579`
- `gateway/run.py:15607`
- `gateway/run.py:15611`
- `gateway/run.py:15733`

Impact: DingTalk approval UX is text-command based, not button based.

### Approval Queue and Resolution

File: `tools/approval.py`

`_await_gateway_decision` creates an `_ApprovalEntry`, appends it to `_gateway_queues[session_key]`, calls the notify callback, then blocks the agent thread on a `threading.Event` until:

- `/approve` or `/deny` resolves it
- gateway approval timeout expires
- notify fails
- the callback is unregistered during cleanup/interruption

Relevant code:
- `tools/approval.py:703`
- `tools/approval.py:728`
- `tools/approval.py:1274`
- `tools/approval.py:1294`
- `tools/approval.py:1320`
- `tools/approval.py:1330`
- `tools/approval.py:1345`
- `tools/approval.py:1517`
- `tools/approval.py:1540`
- `tools/approval.py:1553`
- `tools/approval.py:1584`

Impact: approval is blocking and consent is explicit. Timeout and denial both return a hard `BLOCKED` result to the tool layer.

### `/approve` and `/deny` Slash Handling

Files:
- `gateway/run.py`
- `gateway/slash_commands.py`

`gateway/run.py` has a special busy-agent bypass for `/approve` and `/deny`. This is necessary because the running agent thread is blocked on an approval event; interrupting the agent would not unblock the event.

`gateway/slash_commands.py` implements:

- `/approve`
- `/approve all`
- `/approve session`
- `/approve all session`
- `/approve always`
- `/approve all always`
- `/deny`
- `/deny all`

Relevant code:
- `gateway/run.py:7623`
- `gateway/run.py:8087`
- `gateway/slash_commands.py:3562`
- `gateway/slash_commands.py:3595`
- `gateway/slash_commands.py:3607`
- `gateway/slash_commands.py:3620`
- `gateway/slash_commands.py:3644`

## Streaming and Staged Replies

### Gateway Streaming Gate

File: `gateway/config.py`

Global streaming defaults to disabled:

- `StreamingConfig.enabled = False`
- `StreamingConfig.transport = "auto"`

File: `gateway/run.py`

Per platform, gateway also checks `display.platforms.<platform>.streaming`. If no platform override exists, it follows the global streaming config.

Streaming setup is skipped when the adapter does not support editing. DingTalk reports editing support only when AI Cards are configured and the DingTalk card SDK is available.

Relevant code:
- `gateway/config.py:393`
- `gateway/config.py:395`
- `gateway/run.py:15100`
- `gateway/run.py:15105`
- `gateway/run.py:15112`
- `gateway/run.py:15130`
- `gateway/run.py:15159`
- `gateway/platforms/dingtalk.py:193`

Impact: webhook-only DingTalk does not do token streaming; it falls back to ordinary sends. AI Card-enabled DingTalk can stream.

### Token Streaming Path

Files:
- `gateway/run.py`
- `gateway/stream_consumer.py`
- `run_agent.py`
- `agent/conversation_loop.py`

Gateway wires `agent.stream_delta_callback` to `GatewayStreamConsumer.on_delta`. The agent emits deltas as model text arrives. Tool boundaries emit `stream_delta_callback(None)`, which the consumer treats as a segment break so text before a tool can be finalized before tool progress or later text appears.

Relevant code:
- `gateway/run.py:15171`
- `gateway/run.py:15322`
- `run_agent.py:4200`
- `agent/conversation_loop.py:3966`
- `gateway/stream_consumer.py:474`
- `gateway/stream_consumer.py:483`
- `gateway/stream_consumer.py:486`
- `gateway/stream_consumer.py:603`
- `gateway/stream_consumer.py:613`
- `gateway/stream_consumer.py:622`

Impact: staged replies can appear as:

- live partial answer text with a cursor
- a finalized pre-tool segment before a tool call
- a fresh segment after tool output
- a final edit that removes the cursor and closes the platform streaming state

### Interim Assistant Messages

Files:
- `gateway/run.py`
- `run_agent.py`
- `gateway/stream_consumer.py`

Interim assistant messages are separate from token streaming. When enabled, real mid-turn assistant commentary is sent through `interim_assistant_callback`.

If a stream consumer exists:

- already streamed interim content becomes a segment break
- otherwise it becomes a commentary message

If no stream consumer exists, gateway sends the commentary directly through the adapter.

Relevant code:
- `gateway/run.py:15179`
- `run_agent.py:4185`
- `gateway/stream_consumer.py:489`
- `gateway/stream_consumer.py:681`
- `gateway/stream_consumer.py:1133`

Impact: these are the "stage replies" that are not final answers. They should not set `already_sent` for the final response.

### Tool Progress Messages

File: `gateway/run.py`

Tool progress is independent from token streaming. It is controlled by `display.tool_progress` / `display.platforms.dingtalk.tool_progress`, with modes like `off`, `all`, `new`, and `verbose`.

Tool progress uses a queue. The first progress line is sent as a new platform message; later lines edit the same progress message when the platform supports editing.

Relevant code:
- `gateway/run.py:14274`
- `gateway/run.py:14301`
- `gateway/run.py:14328`
- `gateway/run.py:14673`
- `gateway/run.py:14700`
- `gateway/run.py:14816`

Impact: DingTalk tool progress can become its own AI Card separate from the final answer card.

### DingTalk AI Card Lifecycle

File: `gateway/platforms/dingtalk.py`

DingTalk uses `reply_to` as the signal that a `send()` call is the final response. Calls without `reply_to` are treated as intermediate: tool progress, commentary, or the stream consumer's first chunk.

AI Card behavior:

- `send(..., reply_to=None)` creates a card with `finalize=False`, tracks it in `_streaming_cards`.
- `send(..., reply_to=<message id>)` creates a card with `finalize=True`, then fires Done reaction.
- `edit_message(..., finalize=False)` calls `streaming_update` and keeps the card tracked as open.
- `edit_message(..., finalize=True)` calls `streaming_update`, removes tracking, and fires Done reaction.
- `_stream_card_content` sends `StreamingUpdateRequest(is_full=True, is_finalize=finalize)`.

Relevant code:
- `gateway/platforms/dingtalk.py:1435`
- `gateway/platforms/dingtalk.py:1482`
- `gateway/platforms/dingtalk.py:1488`
- `gateway/platforms/dingtalk.py:1495`
- `gateway/platforms/dingtalk.py:1498`
- `gateway/platforms/dingtalk.py:2085`
- `gateway/platforms/dingtalk.py:2197`
- `gateway/platforms/dingtalk.py:2216`
- `gateway/platforms/dingtalk.py:2238`
- `gateway/platforms/dingtalk.py:2241`
- `gateway/platforms/dingtalk.py:2253`
- `gateway/platforms/dingtalk.py:2272`

## Risks and Follow-Ups

1. DingTalk approval has no button-level approver authorization.

   Current control is inbound message gating plus session-key matching. That is probably acceptable for text-command fallback, but it means the effective approver is "whoever can send an accepted message into the same gateway session". In shared group sessions this may be broader than intended.

2. Group mention gating can block approval replies.

   If a DingTalk group requires mention and `/approve` does not mention the bot or match wake-word patterns, it may be dropped before gateway sees it. `free_response_chats`, `allowed_chats`, or disabling mention requirement for the relevant chat can avoid that.

3. Tool-progress finalize can fire Done early.

   The generic progress edit path passes `finalize=True` for adapters with `REQUIRES_EDIT_FINALIZE`:

   - `gateway/run.py:14673`
   - `gateway/run.py:14679`

   DingTalk's `edit_message(finalize=True)` always calls `_fire_done_reaction`:

   - `gateway/platforms/dingtalk.py:2241`
   - `gateway/platforms/dingtalk.py:2252`

   If a DingTalk tool-progress card is edited more than once, this can flip the original message from Thinking to Done before the final answer has landed. Because `_fire_done_reaction` is idempotent per chat, the real final answer may then have no visible Thinking-to-Done transition left to perform.

   A cleaner contract would distinguish "close this intermediate card" from "turn final is done", for example by adding metadata or an explicit `turn_final` argument to `edit_message`, or by making DingTalk fire Done only on final-response paths.

4. Webhook fallback cannot stream.

   If AI Card creation fails, DingTalk falls back to markdown via session webhook. That path can send final messages and progress messages, but it cannot edit or close a streaming card.

## Test Coverage Observed

Relevant coverage exists in:

- `tests/gateway/test_dingtalk.py` for AI Card finalization, intermediate cards, `_streaming_cards`, sibling cleanup, and Done reaction behavior.
- `tests/gateway/test_stream_consumer.py` for `REQUIRES_EDIT_FINALIZE`.
- Gateway slash-command tests cover shared `/approve` and `/deny` behavior through the generic gateway path.

Coverage gap worth adding:

- A DingTalk-specific regression where a tool-progress edit finalizes an intermediate card but must not fire Done for the whole turn.
- A DingTalk group-gate test confirming whether bare `/approve` is accepted when `require_mention` is enabled. If the current behavior is intended, document the operational requirement.
