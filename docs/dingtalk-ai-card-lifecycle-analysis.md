# DingTalk AI Card Lifecycle Analysis

Date: 2026-06-20

Scope:

- `gateway/platforms/dingtalk.py`
- `gateway/stream_consumer.py`
- `gateway/run.py` tool-progress delivery

## Runtime Capability Gates

`DingTalkAdapter.SUPPORTS_MESSAGE_EDITING` returns true only when both an AI
Card template and the DingTalk card SDK are available. This is what lets the
gateway decide whether it may use progressive edit-based streaming.

`DingTalkAdapter.REQUIRES_EDIT_FINALIZE` has the same gate and tells
`GatewayStreamConsumer` that DingTalk needs an explicit `finalize=True` edit to
close the card's streaming state, even if the final text equals the last
visible streamed text.

## Adapter State Machine

`DingTalkAdapter.send()` creates a card through:

1. `_close_streaming_siblings(chat_id)`
2. `_create_and_stream_card(..., finalize=is_final_reply)`
3. `_stream_card_content(..., is_finalize=finalize)`

The low-level card update is always:

```text
StreamingUpdateRequest(
  out_track_id=<card id>,
  key=<configured content key>,
  content=<text>,
  is_full=True,
  is_finalize=<finalize>,
  is_error=False,
)
```

`finalize=False` keeps the card in streaming state and stores the card id in
`_streaming_cards[chat_id]`.

`finalize=True` closes the card. On `edit_message(..., finalize=True)`, the
adapter also removes the card from `_streaming_cards` and fires the Done
reaction.

`_close_streaming_siblings()` closes any still-open cards for the chat before a
new `send()` creates another card. This prevents old tool-progress or
commentary cards from remaining in a loading state after the conversation moves
on.

## Stage-by-Stage Lifecycle

### Stage 0: Capability Gate

Before any streaming card flow is used, the gateway checks the DingTalk
adapter's runtime capability:

- `SUPPORTS_MESSAGE_EDITING` is true only when both `_card_template_id` and
  `_card_sdk` exist.
- `REQUIRES_EDIT_FINALIZE` has the same gate. For DingTalk AI Cards this means
  the gateway must make an explicit `finalize=True` call to close the card's
  loading/streaming state.

If either card template or SDK is missing, DingTalk behaves like a non-editable
platform and falls back to normal send paths.

### Stage 1: Gateway Creates the Stream Consumer

When `gateway/run.py` handles an incoming DingTalk message, it constructs
`GatewayStreamConsumer` with:

- `adapter=<DingTalkAdapter>`
- `chat_id=<conversation id>`
- `metadata=<thread/message routing metadata>`
- `initial_reply_to_id=<incoming DingTalk message id>`

This `initial_reply_to_id` is important: the stream consumer uses it to anchor
the first visible assistant bubble to the user's original message.

### Stage 2: First Visible Assistant Text

The first non-empty assistant text chunk enters
`GatewayStreamConsumer._send_or_edit()` with no existing `_message_id`.

That path calls:

```text
adapter.send(
  chat_id=<chat>,
  content=<first visible accumulated text + cursor>,
  reply_to=initial_reply_to_id,
  metadata={"expect_edits": True, ...}
)
```

The semantic intent from the stream consumer is:

- This is a preview/stream-start bubble.
- It should return a stable `message_id`.
- Later deltas should update the same bubble through `edit_message`.
- `expect_edits=True` is the metadata signal for "this send is expected to be
  edited later".

The current DingTalk adapter does not use `expect_edits` in the final/non-final
decision. It computes:

```text
is_final_reply = reply_to is not None
```

That means a stream-consumer first send can currently be classified as final
whenever `initial_reply_to_id` is present.

### Stage 3: DingTalk `send()` Chooses AI Card vs Webhook

`DingTalkAdapter.send()` normalizes content and tries to find a cached
`session_webhook`, but it does not fail immediately when the webhook is absent.
That is intentional because AI Card delivery does not need the webhook.

It then looks up `current_message = _message_contexts[chat_id]`. The card path
is available only when all of these are true:

- card template id exists,
- DingTalk card SDK exists,
- the inbound message context exists,
- mention payload is representable by AI Card delivery.

If the card path is not available or fails, `send()` falls back to the session
webhook path.

### Stage 4: Closing Older Streaming Siblings

Before creating a new card, `send()` calls `_close_streaming_siblings(chat_id)`.

That method pops all tracked open cards for the chat from `_streaming_cards` and
sends one final update for each:

```text
_stream_card_content(out_track_id, token, last_content, finalize=True)
```

The purpose is ordering and cleanup:

- a tool-progress card should not stay loading after the final answer starts,
- a commentary/intermediate card should not remain open forever,
- a new card starts with older siblings finalized.

This is not the normal final-answer signal; it is a cleanup pass for previously
tracked open cards.

### Stage 5: Create Card

`_create_and_stream_card()` gets an access token, creates a new
`out_track_id` like:

```text
hermes_<12 hex chars>
```

It calls DingTalk card `create_card` with:

- configured `card_template_id`,
- initial card param map,
- `callback_type="STREAM"`,
- group and robot open-space models.

`callback_type="STREAM"` is what makes later `streaming_update` calls valid for
this card.

### Stage 6: Deliver Card

After creation, `_create_and_stream_card()` delivers the card into the DingTalk
conversation.

For group chats:

```text
open_space_id = dtv1.card//IM_GROUP.<conversation_id>
robot_code = configured robot code
```

For one-to-one robot chats:

```text
open_space_id = dtv1.card//IM_ROBOT.<sender_staff_id>
space_type = IM_ROBOT
```

If a DM has no `sender_staff_id`, card delivery is skipped and the caller can
fall back to webhook/native delivery.

### Stage 7: Initial Streaming Update

Immediately after delivery, `_create_and_stream_card()` calls:

```text
_stream_card_content(out_track_id, token, content, finalize=<caller value>)
```

The low-level DingTalk request is:

```text
StreamingUpdateRequest(
  out_track_id=<card id>,
  guid=<uuid>,
  key=<configured content key>,
  content=<full content>,
  is_full=True,
  is_finalize=<finalize>,
  is_error=False,
)
```

`is_full=True` means each update replaces the whole visible content for that
card key, instead of appending a delta.

The meaning of `is_finalize` is the core lifecycle switch:

- `False`: card remains in streaming/loading state and can be updated again.
- `True`: card is closed/finalized after this content is rendered.

### Stage 8: Adapter Records Open Cards

After `_create_and_stream_card()` succeeds, `send()` returns the card
`out_track_id` as `SendResult.message_id`.

If DingTalk classified this send as non-final:

```text
_streaming_cards[chat_id][out_track_id] = content
```

That card can later be closed by either:

- `edit_message(..., finalize=True)`, or
- the next `send()` via `_close_streaming_siblings()`.

If DingTalk classified this send as final:

- it does not track the card in `_streaming_cards`,
- it fires Done reaction immediately.

### Stage 9: Subsequent Stream Deltas

Once the first send succeeds, the stream consumer stores:

```text
_message_id = result.message_id
```

Later chunks enter `_send_or_edit()` with an existing `_message_id`, so they call:

```text
adapter.edit_message(
  chat_id=<chat>,
  message_id=<out_track_id>,
  content=<full accumulated text + cursor>,
  finalize=False,
)
```

For DingTalk, `edit_message(finalize=False)` calls `streaming_update` again with
`is_full=True` and `is_finalize=False`, then records the card as open in
`_streaming_cards`.

This is the normal "typing/streaming update" phase.

### Stage 10: Segment Break / Tool Boundary

When the stream consumer sees a segment break, it calls:

```text
_send_or_edit(display_text, finalize=True, is_turn_final=False)
```

For DingTalk this is important because `REQUIRES_EDIT_FINALIZE=True`. The
segment break is not the user's final answer, but it must still close the
current card before a tool-progress card or next assistant segment appears
below it.

The stream consumer then resets message state so the next visible segment can
start as a fresh card.

### Stage 11: Final Answer Completion

When the model stream ends, the stream consumer makes sure the final accumulated
answer is delivered without the cursor and with `finalize=True`.

If the content was already visibly delivered in the same tick, most platforms
can skip the redundant final edit. DingTalk cannot skip it when the card still
needs explicit closure, because `REQUIRES_EDIT_FINALIZE=True`.

The DingTalk path therefore should receive:

```text
edit_message(..., content=<final full answer>, finalize=True)
```

`DingTalkAdapter.edit_message(finalize=True)` then:

- sends `StreamingUpdateRequest(is_finalize=True)`,
- removes the card from `_streaming_cards`,
- fires Done reaction.

### Stage 12: Tool Progress Cards

Tool progress is separate from assistant text streaming and lives in
`gateway/run.py`.

The first tool-progress bubble calls `adapter.send(...)`. Later progress lines
try to call `adapter.edit_message(...)` against that progress bubble.

For adapters with `REQUIRES_EDIT_FINALIZE`, the current progress edit helper
passes:

```text
finalize=True
```

That means DingTalk progress cards are closed eagerly on each edit. If a later
non-final edit reopens a card, `_close_streaming_siblings()` on the next
`send()` still closes any tracked open progress card.

### Stage 13: Fallback and Failure Handling

There are three relevant fallback points:

- If the card path is unavailable, `send()` uses the session webhook.
- If card create/deliver/stream fails, `send()` logs a warning and falls back to
  webhook.
- If stream edits fail repeatedly, `GatewayStreamConsumer` disables editing and
  sends only the missing final tail to avoid duplicating already visible text.

For DingTalk AI Card correctness, the critical invariant is:

```text
Every card created with is_finalize=False must eventually receive
StreamingUpdateRequest(is_finalize=True).
```

The project currently enforces that through final edits, segment-break edits,
and sibling cleanup.

## `reply_to` May Not Be Sufficient Alone

Current DingTalk logic treats any `reply_to` as final:

```text
is_final_reply = reply_to is not None
```

That is correct for base final delivery, but not for stream previews.
`GatewayStreamConsumer` first-sends stream preview content with
`reply_to=initial_reply_to_id` so the message is anchored to the original user
message. It also sets metadata:

```text
expect_edits=True
```

The safer final-reply test appears to be:

```text
is_final_reply = reply_to is not None and (notify or not expect_edits)
```

Implications:

- `reply_to + expect_edits=True` is a streaming preview: create the card with
  `finalize=False`, do not fire Done.
- `reply_to + expect_edits=True + notify=True` is a one-shot final stream send:
  create the card with `finalize=True`.
- `reply_to` without `expect_edits` remains the normal final-response path.

No implementation change is currently left applied from this audit. This is a
candidate fix to evaluate separately, because changing it affects the
cross-platform stream-consumer contract and should be reviewed with live
streaming behavior, not landed as a drive-by change.

## Stream Consumer Flow

For normal assistant text streaming:

1. First visible chunk calls `adapter.send(..., reply_to=initial_reply_to_id,
   metadata={"expect_edits": True})`.
2. DingTalk creates an open AI Card (`finalize=False`) and returns its
   `out_track_id`.
3. Later deltas call `adapter.edit_message(..., finalize=False)`.
4. Segment breaks call `_send_or_edit(..., finalize=True, is_turn_final=False)`
   to close the current card before tool-progress or the next segment starts.
5. Final answer completion calls `_send_or_edit(..., finalize=True)` when
   needed. DingTalk requires this because `REQUIRES_EDIT_FINALIZE=True`.

## Tool Progress Flow

Tool progress in `gateway/run.py` has a separate progress bubble:

- First progress line uses `adapter.send(..., reply_to=None)`, so DingTalk
  creates a streaming card.
- Progress edits currently pass `finalize=True` whenever
  `REQUIRES_EDIT_FINALIZE` is true.
- A later progress edit or new final `send()` may reopen/close through the same
  `StreamingUpdateRequest` mechanism.

This works, but it is noisier than the main stream flow: progress edits are
closed eagerly instead of remaining open until the next sibling close. The
existing sibling close still protects against permanently open progress cards.

## Tests Run During Audit

```text
.venv/bin/python3 -m pytest tests/gateway/test_dingtalk.py -q
80 passed before reverting the unrequested candidate change

.venv/bin/python3 -m pytest tests/gateway/test_stream_consumer_thread_routing.py -q
9 passed, 2 warnings

.venv/bin/python3 -m py_compile gateway/platforms/dingtalk.py gateway/stream_consumer.py gateway/run.py
passed
```
