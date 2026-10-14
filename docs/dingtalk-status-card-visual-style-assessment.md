# DingTalk Status Card Visual Style Assessment

Date: 2026-06-20

Branch: `feature/dingtalk-single-card-streaming`

Scope:

- `gateway/turn_status_card.py`
- `gateway/platforms/dingtalk.py`

## Current Rendering Path

The turn status card is rendered as a single markdown-like text payload in
`gateway/turn_status_card.py`.

Current structure:

```text
**进度**
✅ 5 工具 · 1.1s · 答案见下方

**🛠 工具**
- ✓ 💻 `terminal` · preview · 0.1s
- ✓ 🌐 `terminal` · preview · 0.2s
```

The DingTalk adapter then writes the whole rendered string into one AI Card
streaming variable:

- default content key: `msgContent`
- update API: `StreamingUpdateRequest(is_full=True, is_finalize=...)`

The status card is therefore currently a text layout inside a card, not a
structured card made of typed rows, badges, status chips, or progress widgets.

## Observed UX Problems

1. The status column is noisy.

   DingTalk renders markdown list bullets, so a row becomes visually close to:

   ```text
   ● ✓ 💻 terminal ...
   ```

   That creates redundant leading symbols before the user reaches the actual
   tool name.

2. Color cannot be controlled from the current markdown payload.

   The current card path only streams a single content string. Inline color,
   status badges, and row background colors require template-level support.
   Changing emoji alone can improve recognition, but it will not create real
   color hierarchy.

3. Tool identity is still too implementation-heavy.

   Showing repeated `terminal` rows makes the card feel technical and samey.
   The command category is more useful to the user than the executor name:

   - `curl` / `wget` -> network request
   - `cat` / `grep` -> file inspection
   - `python` / `execute_code` -> local analysis

4. Wrapped previews reduce scanability.

   Long URLs and shell commands wrap aggressively on mobile. The current
   `preview_max_len` avoids unbounded growth, but it still often breaks in the
   middle of URLs or command paths.

5. DingTalk text-emotion is a separate channel.

   `gateway/platforms/dingtalk.py` uses DingTalk text emotion labels on the
   inbound user message, such as `💭 思考中`, `💻 命令执行中`, and `✅ 已完成`.
   These labels are not the AI Card content. The current code controls the
   label text and emoji, but not its rendered color.

## Option A: Text-Only Polish

Keep the current AI Card template and only change the markdown text renderer.

Potential adjustments:

- Remove markdown list bullets and render rows as plain lines:

  ```text
  ✅ 🌐 网络请求 · 0.2s
     curl https://...
  ```

- Prefer user-facing category labels over raw tool names:

  ```text
  ✅ 🌐 网络请求
  ✅ 📚 技能读取
  ✅ 🐍 本地分析
  ```

- Split running and completed tools:

  ```text
  当前
  ▶ 🌐 网络请求 · 获取接口响应

  已完成 4
  ✅ 📚 技能读取 · bree-api
  ✅ 🐍 本地分析 · 0.6s
  ```

- Use stronger status vocabulary:

  - running: `▶`
  - completed: `✅`
  - failed: `❌`
  - waiting for approval: `⏸`
  - degraded/no live card: `⚠️`

Pros:

- Low risk.
- No DingTalk template migration.
- Keeps the existing single-card streaming lifecycle.
- Easy to test in current unit tests.

Cons:

- Still mostly monochrome because markdown text rendering owns the visual
  result.
- Cannot provide true badges, colored rows, or compact progress bars.

## Option B: Structured Status Payload Inside Existing Card Template

Use more of the existing default card variables if the template supports them,
for example `msgTitle`, `msgTextList`, or status fields, while keeping the
same AI Card create/deliver/update lifecycle.

Possible direction:

- Stream the latest status into `msgContent`.
- Put compact completed-tool summaries into `msgTextList`.
- Use `flowStatus` or equivalent template variable for high-level state if the
  template actually maps it to color or state UI.

Pros:

- Better visual hierarchy if the current DingTalk template already supports
  those variables.
- Avoids a new template id if the current template can express it.

Cons:

- Needs validation against the actual DingTalk template schema, not just code.
- Streaming update currently updates one `key` at a time; multi-field live
  updates may require coordinated calls or a different template strategy.
- Test coverage must include create/update payload assertions for each key.

## Option C: Dedicated Progress AI Card Template

Create or configure a dedicated DingTalk AI Card template for Hermes progress.

Potential template-level UI:

- Header with state color: running / success / partial failure / failed.
- One highlighted current-action row.
- Completed tools rendered as compact checklist rows.
- Error rows with warning color.
- Optional collapsed "more tools" summary.

Pros:

- This is the only option that can properly solve real color hierarchy.
- Better mobile layout because rows can be designed as rows instead of markdown
  wrapping text.
- Can preserve the current lifecycle: one editable status card plus final
  answer card.

Cons:

- Requires DingTalk template work and config/schema decisions.
- More integration risk than text-only polish.
- Needs graceful fallback when the custom template is missing.

## Recommended Path

Recommended sequence:

1. Do Option A first.

   This addresses the obvious readability issues immediately: remove redundant
   bullets, reduce repeated raw tool names, improve labels, and split current
   vs completed work.

2. Validate whether the current template can support Option B.

   Inspect the actual configured AI Card template and test whether fields like
   `msgTextList` or `flowStatus` visibly change the rendered card. Do not infer
   this only from the variable names in code.

3. Move to Option C only if true color/status hierarchy is required.

   The user's complaint about color cannot be fully solved by markdown text.
   If color is a hard requirement, it belongs in the AI Card template layer,
   not in ad hoc renderer special cases.

## Non-Goals

- Do not add platform-specific one-off branching based on particular command
  strings beyond reusable tool/category classification.
- Do not change agent prompts, tool schemas, history, or cache behavior for
  presentation-only UI.
- Do not use extra progress messages/cards to compensate for styling limits;
  that would regress the original card-spam problem.
