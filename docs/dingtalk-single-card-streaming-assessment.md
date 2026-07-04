# DingTalk Single-Card Streaming Assessment

Date: 2026-06-20

Branch: `feature/dingtalk-single-card-streaming`

Scope:

- `gateway/run.py`
- `gateway/stream_consumer.py`
- `gateway/stream_events.py`
- `gateway/stream_dispatch.py`
- `gateway/platforms/base.py`
- `gateway/platforms/dingtalk.py`
- `agent/tool_executor.py`
- `agent/conversation_loop.py`

Goal:

The underlying product problem is not "single card at all costs". It is:

- too many DingTalk AI Cards for one user question,
- progress is hard to perceive while tools are running,
- completed work is not retained as visible state,
- the latest streaming content and active tool state compete across separate
  bubbles/cards.

One possible solution is a single turn-level card. This document evaluates that
option alongside lower-risk alternatives, then records the accepted first
iteration that was implemented.

## Accepted Direction

The selected first iteration is:

1. Use "live status card + final answer card" as the primary UX.
2. Keep completed tools as a compact checklist with status, tool name, preview,
   and duration/error state.
3. Build the mechanism as a generic gateway capability, but enable it only for
   DingTalk first.

Reasoning:

- This directly addresses the real pain: too many progress cards and unclear
  tool state while the agent is working.
- It avoids the highest-risk part of a strict "single card for everything"
  design: mixing the final answer, tool history, media, footer, and streaming
  state into one potentially long card.
- It gives a staged path. Once the status-card coordinator is stable, moving
  final answer content into the same card becomes a smaller product choice
  rather than a full architecture rewrite.
- It keeps the core agent and prompt cache untouched. The change stays in the
  gateway presentation layer.

Expected user-visible result:

```text
Live status AI Card:
  - current/running tool
  - completed tool checklist with check marks
  - latest short assistant/status text while work continues

Final answer:
  - normal final DingTalk AI Card/message
  - no duplicate progress cards
```

This is not "do only what the user literally said". It is the lower-risk
solution to the underlying problem: card spam plus weak progress perception.

## Implemented First Iteration

Implemented in branch `feature/dingtalk-single-card-streaming`.

Files:

- `gateway/turn_status_card.py`: new turn-scoped live status-card coordinator.
- `gateway/run.py`: wires the coordinator behind adapter capability, routes
  tool lifecycle and optional assistant/interim text into it, and disables the
  separate progress-message worker for that turn.
- `gateway/platforms/base.py`: adds `SUPPORTS_TURN_STATUS_CARD = False`.
- `gateway/platforms/dingtalk.py`: enables `SUPPORTS_TURN_STATUS_CARD` when AI
  Card template + SDK are available.
- `agent/tool_executor.py`: exposes stable tool identity metadata
  (`tool_call_id`, `index`) to opt-in callbacks while preserving the legacy
  four-argument callback contract.
- `tests/gateway/test_turn_status_card.py`: covers plain-text no-card behavior,
  one-card tool lifecycle, finalization, duplicate same-name tools matched by
  call id, and the gateway `_run_agent` integration path that uses the status
  card instead of progress bubbles.

Runtime behavior now:

1. Plain answer with no tools: no live status card is created; final answer is
   delivered normally.
2. First tool start: one live status card is created with
   `metadata.expect_edits=True`.
3. Later tool starts/completions: the same card is edited.
4. Completed tools stay visible with a compact completed marker, preview, and
   duration.
5. Duplicate same-name tools are matched by `tool_call_id` when available.
6. Turn end: the live status card is finalized once; the final answer remains a
   separate normal final response.
7. Delegated subagent tool events are mapped into the same live status card.
   Subagents emit `subagent.tool` / `subagent.tool.completed`; the gateway maps
   these to the status-card tool lifecycle while preserving `subagent_id`,
   `tool_call_id`, and `index` identity metadata.

The coordinator intentionally does not persist anything to conversation history
and does not change the agent's model-visible messages, prompt, or tool schema.

Runtime finding from 2026-06-20 09:41:

The DingTalk turn `调研一下获取ip的网站` did not show tool progress because the
terminal calls were made inside a delegated subagent:

- parent turn: `platform=dingtalk`
- child turn: `platform=subagent`
- actual terminal calls logged under the child session
  `20260620_094125_f9305e`

The first iteration handled top-level `tool.started` / `tool.completed` events
only. Subagent progress already existed in `tools/delegate_tool.py`, but it was
emitted upward as `subagent.tool` plus batched `subagent.progress` summaries.
For a status-card platform, that left real child tool calls out of the card.

Fix:

- `tools/delegate_tool.py` now relays child `tool.completed` as
  `subagent.tool.completed`, and marks its callback as accepting tool identity
  metadata so child `tool_call_id` / `index` are preserved.
- `gateway/run.py` maps `subagent.tool` and `subagent.tool.completed` into the
  turn status-card lifecycle.
- `gateway/turn_status_card.py` scopes identity keys by `subagent_id` or child
  session id so sibling subagents using the same tool-call index do not collide.

Validation:

- `tests/gateway/test_turn_status_card.py -q`: 5 passed.
- `tests/gateway/test_run_progress_topics.py tests/gateway/test_run_progress_interrupt.py -q`:
  35 passed.
- DingTalk/stream/progress regression batch:
  `tests/gateway/test_turn_status_card.py`,
  `tests/gateway/test_run_progress_topics.py`,
  `tests/gateway/test_run_progress_interrupt.py`,
  `tests/gateway/test_run_cleanup_progress.py`,
  `tests/gateway/test_display_config.py`,
  `tests/gateway/test_dingtalk.py`,
  `tests/gateway/test_stream_consumer.py`,
  `tests/gateway/test_stream_consumer_thread_routing.py`,
  `tests/gateway/test_stream_consumer_fresh_final.py`: 292 passed.
- Tool executor callback contract regression:
  `tests/run_agent/test_tool_call_guardrail_runtime.py`,
  `tests/run_agent/test_run_agent.py::TestExecuteToolCalls`,
  `tests/run_agent/test_run_agent.py::TestConcurrentToolExecution`: 51 passed,
  1 third-party deprecation warning from `discord/player.py`.
- Subagent progress regression:
  `tests/agent/test_subagent_progress.py tests/tools/test_delegate.py tests/gateway/test_turn_status_card.py`:
  167 passed.
- DingTalk/status-card/stream/subagent regression batch:
  `tests/gateway/test_turn_status_card.py`,
  `tests/gateway/test_run_progress_topics.py`,
  `tests/gateway/test_run_progress_interrupt.py`,
  `tests/gateway/test_run_cleanup_progress.py`,
  `tests/gateway/test_display_config.py`,
  `tests/gateway/test_dingtalk.py`,
  `tests/gateway/test_stream_consumer.py`,
  `tests/gateway/test_stream_consumer_thread_routing.py`,
  `tests/gateway/test_stream_consumer_fresh_final.py`,
  `tests/tui_gateway/test_subagent_child_mirror.py`: 303 passed.
- `py_compile` for touched Python files passed.
- `git diff --check` passed.

## Questions For Product Direction

Before implementing, these choices should be explicit:

1. Should the final answer and tool progress live in the same AI Card, or is
   "one progress card + one final answer card" acceptable?
2. During a long tool run, should the user see only the latest running tool, or
   a full checklist of all tools so far?
3. After the answer is complete, should completed tool history remain visible,
   collapse into a short summary, or disappear?
4. If one turn produces media attachments, should media remain separate native
   messages, or should the card show placeholders/links for them?
5. Is this behavior DingTalk-only for now, or should it be designed as a generic
   gateway capability for card-like platforms?
6. What is more important for the first iteration: minimum implementation risk,
   or the cleanest single-card user experience?

Accepted recommendation: implement a DingTalk-enabled generic coordinator, but
start with a conservative visual model: one live status card per turn, compact
tool checklist, final answer delivered separately, media still delivered
through the existing native path.

## Current Runtime Order

### 1. DingTalk inbound message arrives

`gateway/platforms/dingtalk.py` receives a `ChatbotMessage`, derives:

- `chat_id`
- `conversation_id`
- `conversation_type`
- `sender_staff_id`
- `message_id`

It stores the inbound DingTalk message in `_message_contexts[chat_id]`. That
message context is required later because DingTalk AI Card delivery needs the
conversation id for groups or the sender staff id for one-to-one robot chats.

### 2. Gateway prepares display state

`gateway/run.py` resolves display settings for the current platform:

- token streaming on/off,
- `tool_progress`,
- `tool_progress_grouping`,
- `thinking_progress`,
- `interim_assistant_messages`.

If tool progress or thinking progress is enabled, it creates `progress_queue`.
This queue is separate from assistant token streaming.

### 3. Gateway creates `GatewayStreamConsumer`

When streaming or interim assistant messages are enabled, `gateway/run.py`
constructs `GatewayStreamConsumer` with:

- `adapter=<DingTalkAdapter>`
- `chat_id=<DingTalk conversation>`
- `metadata=<thread/routing metadata>`
- `initial_reply_to_id=<incoming DingTalk message id>`
- `on_new_message=lambda: progress_queue.put(("__reset__",))`

The consumer owns assistant text streaming. The progress queue owns tool
progress. They are currently independent rails.

### 4. Agent callbacks are wired

For this turn, the cached or newly created `AIAgent` gets these callbacks:

- `stream_delta_callback`: sends assistant text deltas to
  `GatewayStreamConsumer.on_delta()`.
- `interim_assistant_callback`: sends completed interim assistant messages to
  `GatewayStreamConsumer.on_commentary()` or `on_segment_break()`.
- `tool_progress_callback`: sends tool lifecycle events to the gateway progress
  callback.
- `tool_start_callback`: separate voice-ack path for Discord, not relevant to
  DingTalk cards.

These callbacks are per-turn state and are not baked into the cached agent
constructor, so changing their presentation behavior should not invalidate the
agent prompt cache by itself.

### 5. First assistant text chunk creates the first AI Card

The first visible assistant text enters `GatewayStreamConsumer._send_or_edit()`
with no `_message_id`.

It calls:

```text
adapter.send(
  chat_id=<chat>,
  content=<accumulated assistant text + cursor>,
  reply_to=initial_reply_to_id,
  metadata={"expect_edits": True, ...}
)
```

The stream-consumer intent is "create an editable preview message". It expects
the returned `message_id` to be edited later.

For DingTalk, `send()` tries AI Card delivery first. If card delivery is
available, it calls:

```text
_close_streaming_siblings(chat_id)
_create_and_stream_card(..., finalize=is_final_reply)
```

Current DingTalk final detection is:

```text
is_final_reply = reply_to is not None
```

That is a mismatch for stream previews because the first stream send has
`reply_to=initial_reply_to_id` and `metadata.expect_edits=True`. It can be
classified as a final reply even though the consumer expects edits.

### 6. DingTalk card is created, delivered, then updated

`_create_and_stream_card()` does three DingTalk SDK calls:

1. `create_card` with `callback_type="STREAM"`.
2. `deliver_card` to either:
   - `dtv1.card//IM_GROUP.<conversation_id>`, or
   - `dtv1.card//IM_ROBOT.<sender_staff_id>`.
3. `streaming_update` through `_stream_card_content()`.

The low-level update is always full replacement:

```text
StreamingUpdateRequest(
  out_track_id=<card id>,
  key=<content key>,
  content=<full rendered content>,
  is_full=True,
  is_finalize=<finalize>,
  is_error=False,
)
```

This is good for the desired feature: a single card can be updated repeatedly by
rendering the entire desired markdown state and sending `is_full=True`.

### 7. Model reaches a tool boundary

When the model emits assistant text and then tool calls,
`agent/conversation_loop.py` does this before executing tools:

```text
agent.stream_delta_callback(None)
```

In `GatewayStreamConsumer`, `None` means segment break. The consumer finalizes
the current assistant message/card and prepares the next assistant text segment
to become a fresh message.

This is one direct cause of multiple DingTalk cards.

### 8. Tool start is sent on a separate progress rail

`agent/tool_executor.py` emits:

```text
tool_progress_callback("tool.started", tool_name, preview, args)
```

`gateway/run.py` formats that as a progress line and puts it on
`progress_queue`.

`send_progress_messages()` then creates or edits a separate progress message:

```text
adapter.send(... progress text ...)
adapter.edit_message(... progress text ...)
```

For DingTalk, those calls also use AI Cards. That means tool progress can become
its own card, separate from the assistant card.

### 9. Tool completion currently does not update visible progress

`agent/tool_executor.py` also emits:

```text
tool_progress_callback(
  "tool.completed",
  tool_name,
  None,
  None,
  duration=<seconds>,
  is_error=<bool>,
  result=<tool result>,
)
```

But `gateway/run.py` currently ignores ordinary `tool.completed` events, except
for the long-tool onboarding hint. Therefore current DingTalk progress can show
that a tool started, but it does not mark that tool as complete in the visible
card.

This is the signal needed for "completed tool calls get a check mark"; it is
already emitted, but the current renderer does not consume it for display.

### 10. Assistant content resumes after tools

After tool execution, the agent sets `_stream_needs_break=True`. The next real
assistant text delta gets a paragraph break prepended and goes back through
`stream_delta_callback`.

Because the previous segment was finalized/reset, `GatewayStreamConsumer` can
create another message/card for the resumed assistant content.

This is another direct cause of multiple DingTalk cards.

### 11. New assistant message resets progress bubble

When `GatewayStreamConsumer` creates a fresh content bubble, it calls
`on_new_message`. In `gateway/run.py`, that enqueues:

```text
("__reset__",)
```

The progress worker treats this as "content resumed; stop editing the old
progress bubble and start a new one for later tools".

This behavior is correct for chronological multi-message chat, but it is the
opposite of the requested single-card behavior. For one-card DingTalk, tool
history and assistant content need to share one render state, not reset each
other into new cards.

### 12. Final assistant answer closes the stream

At the end of the model run, `GatewayStreamConsumer.finish()` causes a final
send/edit with `finalize=True`.

For DingTalk, `REQUIRES_EDIT_FINALIZE=True`, so an explicit final
`edit_message(..., finalize=True)` is required to close the AI Card streaming
indicator. Then `_fire_done_reaction()` swaps the original user-message reaction
from Thinking to Done.

### 13. Gateway suppresses the normal final send

After `_run_agent()` finishes, `gateway/run.py` checks the stream consumer:

- `final_response_sent`
- `final_content_delivered`
- `response_previewed`

If streaming already delivered the final response, it sets
`response["already_sent"] = True`, so the outer normal `adapter.send()` is
skipped. This prevents duplicate final answers.

This suppression will still be needed in single-card mode.

### 14. Media/footer can still create extra messages

Even when streaming delivered the body, gateway may still send:

- media extracted from `MEDIA:` tags,
- a runtime footer as a trailing message.

Those are outside the AI Card text lifecycle. If "single card" must be strict,
media/footer behavior needs separate decisions. If the scope is only text +
tool progress, they can stay as separate delivery paths.

## Why Multiple Cards Happen Today

The current design intentionally separates:

- assistant streaming content: `GatewayStreamConsumer`,
- tool progress: `progress_queue` and `send_progress_messages()`,
- final fallback delivery: normal gateway send path.

It also intentionally finalizes assistant text at tool boundaries so progress
messages can appear chronologically below the last assistant segment.

For DingTalk AI Cards, each `send()` can create a card. Therefore:

```text
assistant preamble card
tool progress card
assistant continuation card
possibly more tool/progress cards
final card or final edit
```

is the expected result of the current architecture.

## Solution Options

### Option A: Keep Separate Final Answer, Collapse Tool Progress Into One Progress Card

Runtime shape:

```text
progress AI Card:
  running/completed tool checklist, edited in place

final answer AI Card/message:
  final assistant response
```

How it solves the problem:

- Reduces many tool cards to one progress card.
- Shows completed tools with check marks.
- Keeps final answer visually clean.

What remains:

- Still at least two cards/messages for tool-heavy turns.
- Assistant preamble/continuation can still produce extra cards unless
  streaming text before tools is suppressed or folded into progress.

Engineering impact:

- Lowest-risk path.
- Reuses the current `progress_queue` worker.
- Needs `tool.completed` rendering and stable tool identity.
- Does not require replacing `GatewayStreamConsumer`.

Risk:

- It improves "progress cannot be perceived", but only partially solves
  "card count is high".

Fit:

- Good first step if we want quick improvement and minimal blast radius.

### Option B: One "Live Status" Card Plus Final Answer

Runtime shape:

```text
live status AI Card:
  current assistant phase
  running tool
  completed tool checklist
  compact "answer is being prepared" status

final answer AI Card/message:
  only the final answer
```

How it solves the problem:

- User can perceive progress throughout the turn.
- Tool history remains visible until final.
- The final response is not mixed with progress chrome.

What remains:

- The turn still ends with a second card/message for the final answer.
- Need to decide whether live status card stays, finalizes, or gets deleted.

Engineering impact:

- Medium risk.
- Can build a coordinator for progress/status only, while leaving final
  delivery mostly unchanged.

Risk:

- If the live status card remains after final, chat still has visual clutter.
- If it is deleted/collapsed, DingTalk deletion/card mutation behavior needs
  live verification.

Fit:

- Good if final answers should remain clean and permanent, while progress is
  treated as temporary operational UI.

### Option C: One Unified Turn Card

Runtime shape:

```text
single AI Card for the whole user turn:
  assistant text
  completed tool checklist
  currently running/latest tool
  final answer
```

How it solves the problem:

- Strongest fix for card count: one card per turn.
- Strongest progress perception: all state is in one place.
- Completed tools can remain checked while the latest tool/content streams.

Engineering impact:

- Highest implementation cost.
- Requires merging assistant stream and tool progress into one per-turn render
  state.
- The current `GatewayStreamConsumer` is built around segment breaks creating
  new messages; unified-card mode should use a separate coordinator rather than
  forcing the existing consumer into two incompatible behaviors.

Risk:

- Long card content can hit DingTalk limits.
- Tool matching needs stable call identity.
- Final-response suppression must be exact or duplicate answers appear.
- Error/interrupt finalization needs explicit coverage.

Fit:

- Best match if the product requirement is truly "one card per user question".

### Option D: Suppress Most Intermediate Assistant Cards, Keep Tool Card Editing

Runtime shape:

```text
one editable tool/progress card
final answer message/card
```

Assistant text before tools is not rendered as separate card unless it is long
or meaningful.

How it solves the problem:

- Removes many low-value "I'll check..." cards.
- Keeps tool progress visible.
- Avoids combining final answer with tool chrome.

Engineering impact:

- Medium-low risk.
- Requires policy for which interim assistant text is visible.
- Less invasive than unified turn card.

Risk:

- Some useful reasoning/preamble may disappear from DingTalk.
- Product behavior becomes more heuristic.

Fit:

- Good if most card spam comes from short assistant preambles around tool calls.

### Option E: Keep Current Delivery, Add Cleanup/Collapse After Final

Runtime shape:

```text
during run:
  existing multiple cards/messages

after final:
  delete or finalize/collapse temporary progress cards
  keep final answer
```

How it solves the problem:

- Reduces final chat clutter after completion.

What remains:

- During execution the user may still see multiple cards.
- Progress perception remains limited unless completion marks are added.

Engineering impact:

- Lowest or medium risk depending on DingTalk delete/collapse capability.
- Similar to existing `cleanup_progress`, but DingTalk-specific verification is
  needed.

Risk:

- DingTalk card/message deletion may not be available for every route.
- Does not solve the live "what is happening now" problem by itself.

Fit:

- Good as an add-on, not sufficient as the main solution.

## Desired Runtime Order For Option C

The desired behavior should be:

### 1. First visible turn event creates one card

The first assistant text or first tool event creates one DingTalk AI Card for
the whole user turn.

The returned `out_track_id` becomes the turn card id.

### 2. Every later event updates the same card

Assistant chunks, tool starts, tool completions, commentary, and final answer
all update the same `out_track_id` with:

```text
StreamingUpdateRequest(is_full=True, is_finalize=False)
```

until the turn is complete.

### 3. Completed tool calls remain in the rendered state

The renderer keeps a per-turn list of tool entries:

```text
[
  {id/index, name, preview, status=completed, duration, is_error},
  {id/index, name, preview, status=running},
]
```

Completed tools stay visible. Running/latest tools are updated in place.

### 4. Assistant content is rendered below or around tool state

The full card content is rebuilt on every update. A simple markdown layout:

```text
<assistant text before/current tools>

工具调用
- [done] read_file: ...
- [done] terminal: ...
- [running] web_search: ...

<latest assistant streaming content>
```

Exact symbols should match the existing gateway visual language. In code, this
should be centralized in the renderer so tests assert status semantics rather
than visual glyphs everywhere.

### 5. Final turn update closes the same card

At the end of the turn, the same card receives:

```text
StreamingUpdateRequest(is_full=True, is_finalize=True)
```

Then DingTalk fires Done reaction once.

## Feasibility

This is feasible because DingTalk AI Card streaming updates are full-replace
updates. We do not need to create multiple cards to show multiple phases.

The main work is not the DingTalk SDK call; the main work is merging two
gateway presentation rails into one per-turn render state.

## Existing Infrastructure To Reuse

`gateway/stream_events.py` already defines a structured presentation event
vocabulary:

- `MessageChunk`
- `MessageStop`
- `Commentary`
- `ToolCallChunk`
- `ToolCallFinished`
- `LongToolHint`
- `GatewayNotice`

`gateway/stream_dispatch.py` already routes these events through adapter render
hooks, but it is not currently wired into the main `gateway/run.py` DingTalk
flow. Its current default behavior still sends tool lines to the progress queue
and ignores `ToolCallFinished` for visible chrome.

This is the right direction to build on. It avoids inventing a second
DingTalk-only event vocabulary.

## Recommended Design

### Recommended First-Class Design: Turn-Level Presentation Coordinator

Add a gateway-side coordinator for platforms/adapters that opt into unified
turn cards. Conceptually:

```text
TurnCardCoordinator
  - owns one adapter/chat/metadata/reply anchor
  - owns one message_id/out_track_id
  - owns assistant text state
  - owns tool list state
  - renders full markdown
  - sends first card once
  - edits that same card for every update
  - finalizes once at turn end
```

This should be presentation-layer only. It must not mutate agent conversation
history, tool schemas, system prompt, or cached agent state.

For Option A/B/D, the same coordinator can be scoped down to progress/status
only. That means the work can be staged: first use the coordinator to render
one progress/status card, then decide whether final assistant content should
move into it.

### Activate through adapter capability, not hardcoded platform branching

Avoid a direct `if platform == DINGTALK` behavior fork in `gateway/run.py`.

Prefer an adapter capability such as:

```text
SUPPORTS_UNIFIED_TURN_CARD = True
```

or a small method probe, with DingTalk returning true only when AI Card support
is configured.

This keeps the implementation generic enough for future card-like platforms,
while still concrete because DingTalk is the first consumer.

### Use one render path for assistant and tools

In unified-card mode:

- `stream_delta_callback(text)` should update coordinator assistant state.
- `stream_delta_callback(None)` should mark a segment boundary in state, not
  finalize the card and reset `_message_id`.
- `interim_assistant_callback` should append/update commentary in the same
  card.
- `tool_progress_callback("tool.started")` should add or update a running tool
  entry.
- `tool_progress_callback("tool.completed")` should mark that entry completed,
  failed, or cancelled.

The existing separate `progress_queue` worker should not send DingTalk AI Cards
for that turn when unified-card mode is active.

### Extend tool lifecycle identity before relying on check marks

Current `tool.completed` callback does not include a stable call id or index.
For duplicate/concurrent tool calls, matching completion to start by tool name
is ambiguous.

Before implementing check marks robustly, extend the tool progress callback
kwargs in `agent/tool_executor.py` to include at least one stable identity:

- `tool_call_id`, preferred because the model/tool call already has it, or
- per-turn `index`, acceptable if emitted consistently for started/completed.

This change is presentation metadata only; it does not change model-visible
messages.

### Keep `expect_edits` semantics correct

For DingTalk AI Cards, stream preview sends should not be treated as final just
because they have `reply_to`.

The final/non-final decision should account for:

```text
metadata.expect_edits
metadata.notify
reply_to
```

Without this, the first card can be prematurely finalized and Done reaction can
fire before tools/content finish.

## Options Considered

### Option A: Patch only DingTalk `send()` and keep current progress queue

This would fix some premature finalize behavior, but it would not satisfy the
single-card requirement. Tool progress would still be sent through a separate
progress message/card.

Verdict: insufficient.

### Option B: Make progress worker edit the stream consumer's current card

This is tempting but fragile. The progress worker and stream consumer are
separate async tasks with separate state. Sharing `_message_id` directly would
introduce ordering races and make fallback behavior harder to reason about.

Verdict: risky.

### Option C: Unified turn-card coordinator fed by structured events

This treats assistant chunks and tool lifecycle events as one ordered
presentation stream and renders one full card state. It aligns with existing
`gateway/stream_events.py` design and with DingTalk's full-replace
`StreamingUpdateRequest`.

Verdict: recommended.

## Implementation Plan

### Phase 1: Contract tests for current event order

Add tests that simulate:

```text
assistant chunk
tool.started
tool.completed
assistant chunk
final stop
```

Assert current callbacks fire in the expected order and that the new
coordinator can consume them without touching real DingTalk network calls.

### Phase 2: Add tool call identity

Update serial and concurrent tool execution paths to include `tool_call_id` or
`index` on both `tool.started` and `tool.completed`.

Tests must cover:

- one tool,
- two different tools,
- two concurrent tools with the same name,
- failed tool completion.

### Phase 3: Add coordinator and renderer

Introduce a presentation-only coordinator that can render a deterministic
markdown snapshot from:

- assistant text segments,
- commentary,
- running tools,
- completed tools,
- failed tools,
- final state.

Tests should assert semantic content and status ordering.

### Phase 4: Wire coordinator behind adapter capability

In `gateway/run.py`, when adapter supports unified turn card:

- do not start separate `send_progress_messages()` for that turn,
- route tool progress callbacks into coordinator,
- route stream deltas into coordinator,
- route interim assistant messages into coordinator,
- finalize coordinator before checking normal final send suppression.

Non-unified platforms keep the existing stream consumer + progress queue path.

### Phase 5: DingTalk adapter support

DingTalk already has the low-level primitives:

- `send()` can create a card,
- `edit_message()` can update it,
- `edit_message(finalize=True)` can close it.

The main DingTalk-specific work is ensuring first card creation remains open
and returns the `out_track_id`, then every later update edits that same id.

### Phase 6: E2E-style tests

Use fake DingTalk SDK objects and a temp gateway runner/config to verify:

- exactly one `create_card` call for a multi-tool turn,
- multiple `streaming_update` calls target the same `out_track_id`,
- intermediate updates use `is_finalize=False`,
- final update uses `is_finalize=True`,
- completed tools remain visible in later updates,
- failed tools are marked failed,
- final normal `send()` is suppressed via `already_sent`.

## Key Risks

### Risk 1: Tool completion matching is ambiguous today

Without call identity, duplicate concurrent tools can mark the wrong row as
complete.

Mitigation: add `tool_call_id` or stable index before rendering check marks.

### Risk 2: Existing `GatewayStreamConsumer` assumes segment breaks create new messages

The current consumer intentionally resets message state at tool boundaries. A
single-card mode should not reuse that exact state machine unchanged.

Mitigation: use a separate coordinator for unified-card mode instead of forcing
the old consumer to behave both ways.

### Risk 3: Progress queue cancellation currently drains pending tool lines

The existing progress worker has cancellation/drain behavior. A coordinator must
also flush pending events and finalize the card on normal completion,
interrupt, error, and timeout.

Mitigation: explicit tests for cancellation/failure paths.

### Risk 4: Very long card content can exceed DingTalk limits

Keeping all completed tools plus assistant content in one card can grow large.

Mitigation: renderer should compact old completed tools when content approaches
`MAX_MESSAGE_LENGTH`, for example keep name/status/duration and truncate
preview/result summaries.

### Risk 5: Runtime footer/media are separate post-processing paths

Even with a unified text/tool card, media and runtime footer can still produce
separate sends.

Mitigation: define scope first. For initial implementation, treat the feature as
"single text/tool AI Card"; media remains native delivery.

## Test Matrix

Unit tests:

- coordinator render order,
- completed tool persists after assistant resumes,
- latest running tool updates in place,
- failed tool marks failed,
- duplicate same-name tools match by id/index,
- final render strips cursor and finalizes.

Gateway integration tests:

- DingTalk multi-stage turn creates one card only,
- no progress worker card is sent in unified mode,
- final send suppression still works,
- existing non-DingTalk progress tests remain unchanged,
- `expect_edits` keeps first stream card open.

Live/manual verification:

- one plain answer with no tools,
- answer with one tool,
- answer with multiple serial tools,
- answer with concurrent duplicate tools,
- answer with a failing tool,
- answer interrupted mid-tool,
- answer containing media tags.

## Recommendation

Recommended path:

1. Start with the shared foundation needed by every viable option: stable tool
   lifecycle identity and visible `tool.completed` rendering.
2. Implement a turn-card/status coordinator behind an adapter capability.
3. First enable it for DingTalk as either Option B or Option C, depending on the
   product answers above.
4. If the priority is "fewest cards", choose Option C.
5. If the priority is "lower risk but clear progress", choose Option B first.

Do not only patch DingTalk `send()`. That fixes premature finalization but does
not solve the requested "all phases update the first card" behavior.

Do not share raw `_message_id` between the existing progress worker and stream
consumer. That couples two independent async state machines and makes ordering
and failure handling harder.

## Runtime Finding: 2026-06-20 14:13-14:41 Card Spam

Observed profile: `xcx`.

The gateway was restarted with the new DingTalk card code, but the recent
runtime logs still showed repeated lines like:

- `AI Card created (streaming): ...`
- `AI Card sibling closed: ...`

There were no `turn status card created` log lines. That proves the repeated
cards were still produced by the legacy `GatewayStreamConsumer`/interim path,
not by the new `TurnStatusCardCoordinator`.

Root cause:

- `display.interim_assistant_messages` was enabled for the profile.
- `display.streaming` was disabled globally.
- The first implementation only enabled the turn status card when
  `tool_progress_enabled` was true.
- If runtime env/config disables tool progress, interim assistant messages can
  still create a stream consumer, so every model commentary segment becomes a
  separate DingTalk streaming card.
- DingTalk `send()` also used `reply_to is not None` as the card lifecycle
  boundary. That made ordinary sends without a reply anchor look like
  intermediate streaming cards, even when the caller did not intend to edit
  them later.

Fix:

- Enable `TurnStatusCardCoordinator` whenever the adapter supports turn status
  cards and the turn may emit tool progress, thinking progress, interim
  assistant messages, or token streaming.
- Wire `tool_progress_callback` when a turn status card exists, even if the
  legacy tool-progress chat setting is off, so the card can render real tool
  lifecycle state without re-enabling separate progress bubbles.
- Treat `metadata.expect_edits=True` as the explicit DingTalk lifecycle signal.
  Only those cards remain streaming. Ordinary `send()` calls create finalized
  one-shot cards; `reply_to` is used only for final-response behaviors such as
  @-sender and Done reactions.

Regression coverage:

- Gateway test for DingTalk interim assistant messages with
  `HERMES_TOOL_PROGRESS_MODE=off`.
- DingTalk adapter tests for finalized one-shot card sends versus
  `expect_edits=True` editable cards.

## Runtime Finding: 2026-06-20 15:15 Editable Fallback Boundary

After the 15:14 restart, the first live DingTalk turn did enter the new turn
status path, but DingTalk rejected the editable AI Card create request:

- `Forbidden.AccessDenied.IpNotInWhiteList`
- request IP: `207.174.6.105`
- appKey: configured DingTalk appKey redacted in repo docs

The adapter then fell back to the session webhook. That fallback is valid for a
one-shot message, but it is not editable via DingTalk AI Card
`streaming_update`. The turn status coordinator incorrectly accepted the
webhook `message_id` as if it were an AI Card `out_track_id`, producing repeated
`param.stream.outTrackId: card is not exist` edit failures.

Fix:

- When `metadata.expect_edits=True`, DingTalk AI Card creation failure now
  returns `SendResult(success=False)` instead of falling back to webhook.
- The turn status coordinator disables itself after a non-retryable initial
  send/edit failure, so a non-editable delivery path cannot generate edit
  failure storms.

The later 15:17 turn confirmed the intended path: one status card was created,
updated, finalized, and then one final answer card was sent.
