"""M3.3 · Single-person-view projection.

Design ref: docs/design/agent-room/design.html §2.4.

Takes the shared room history (list of RoomMessage rows from
AgentRoomMessagesStore) and rewrites it into the perspective of ONE
target member so that member's LLM turn sees a coherent conversation
history.

Projection rules (§2.4):
  * user messages           → role="user",       prefix="[user]: "
  * observer messages       → role="user",       prefix="[observer]: "
  * OTHER member's messages → role="user",       prefix="[<other>]: "
  * TARGET member's messages → role="assistant"  (no prefix)
  * tool_result rows        → folded into the immediately preceding
                              user prefix (as descriptive text) since
                              other members' tool results are not
                              meaningful to this member's model.

Design tradeoffs:
  * Content truncation for attachments >4000 chars (M3-B7): the
    projection layer summarizes long content to keep prompt within
    context window budget.
  * Deterministic output: same input list → same output list. No
    hidden state, no timestamps in the rendered content (except where
    the caller asks for them).
  * O(n) — single pass over the input list, no repeated re-render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from gateway.agent_room_messages_store import RoomMessage

_MAX_CONTENT_CHARS = 4000  # M3-B7: cap per-message content length

# Raft AX 改造 3 (agent inbox): default window size for the "summary +
# recent N" projection. The article's core inbox idea is to invert
# push→pull: stop flooding the whole room history into every member's
# context (and hard-truncating it), and instead push only a compact
# digest of the older tail + the last N verbatim turns, leaving the
# member free to pull the rest on demand (room_fetch_context, slice 2).
_DEFAULT_RECENT_N = 12
# Per-row length inside the digest line — short, since the digest is a
# scannable index, not the full artifact.
_DIGEST_LINE_CHARS = 160


@dataclass(frozen=True)
class ProjectedMessage:
    """One message in the target member's rendered timeline."""
    role: str      # "user" or "assistant"
    content: str

    def to_openai(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def _truncate(text: str, limit: int = _MAX_CONTENT_CHARS) -> str:
    """M3-B7: cap message length to prevent context blowup."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[: limit - 20]
    return f"{head}...[truncated]"


def _render_content(msg: RoomMessage, target_member: str) -> str:
    """Format one row's content with the appropriate prefix for the
    target member's perspective."""
    kind = msg.sender_kind
    name = msg.sender_name
    body = _truncate(msg.content)

    if kind == "user":
        # The end user (human) — always prefixed [user]
        return f"[user]: {body}" if body else "[user]:"
    if kind == "observer":
        # Observer's routing decision — surface it as context. Both tool
        # forms carry empty content (the decision lives in tool_calls), so
        # without this the observer's own history would show a blank
        # [observer] row and lose track of what it decided (M4-B8: route
        # and decompose calls interleave in the SAME observer session).
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name")
                    args = fn.get("arguments") or "{}"
                    if fn_name == "route_to_member":
                        return f"[observer]: routed to member (args: {_truncate(args, 200)})"
                    if fn_name == "decompose_and_route":
                        return f"[observer]: decomposed into subtasks (args: {_truncate(args, 200)})"
        return f"[observer]: {body}" if body else "[observer]"
    if kind == "member":
        if name == target_member:
            # This is what "I" said in the past — assistant role, no prefix
            return body
        # Another member's turn — surface as if they spoke to me
        return f"[{name}]: {body}" if body else f"[{name}]"
    if kind == "tool_result":
        # Fold tool results into user-visible text; the target
        # member's model doesn't have that specific tool call in its
        # own tool_calls array, so leaving it as role=tool would
        # orphan the reference. Summarize instead.
        return f"[tool: {name}] {_truncate(body, 500)}"

    # Unknown kind — pass through defensively
    return body


def project_for_member(
    messages: list[RoomMessage],
    target_member: str,
) -> list[ProjectedMessage]:
    """Render `messages` from ``target_member``'s perspective.

    Parameters
    ----------
    messages : list[RoomMessage]
        Full room history in canonical (sequence) order. Empty list
        yields empty output.
    target_member : str
        The member whose viewpoint we're rendering. Their own past
        messages become role="assistant"; everyone else's become
        role="user" with a prefix identifying the speaker.

    Returns
    -------
    list[ProjectedMessage]
        One entry per input row (no filtering, no reordering). Callers
        can call .to_openai() on each to get the OpenAI wire format.

    Notes
    -----
    * Consecutive user-role messages are NOT merged — the LLM sees
      them as distinct turns from distinct speakers, which is exactly
      the multi-party conversation we want to convey.
    * Empty output is valid (empty room = no history to project).
    """
    if not messages:
        return []

    projected: list[ProjectedMessage] = []
    for msg in messages:
        content = _render_content(msg, target_member)
        if msg.sender_kind == "member" and msg.sender_name == target_member:
            role = "assistant"
        else:
            role = "user"
        projected.append(ProjectedMessage(role=role, content=content))

    return projected


def _digest_line(msg: RoomMessage, target_member: str) -> str:
    """One compact, scannable index line for an older (out-of-window) row.

    Uses the same speaker attribution as full projection so the member can
    tell who said what, but truncated hard to a short preview — the digest
    is an *index* of what happened, not the artifact itself. The member
    pulls the full text on demand (room_fetch_context, slice 2)."""
    kind = msg.sender_kind
    name = msg.sender_name
    body = _truncate(msg.content or "", _DIGEST_LINE_CHARS)
    if kind == "user":
        who = "user"
    elif kind == "observer":
        who = "observer"
    elif kind == "tool_result":
        who = f"tool:{name}"
    elif kind == "member" and name == target_member:
        who = "me"
    else:
        who = name or kind
    # #<seq> so a later room_fetch_context(range=...) can address it.
    return f"#{msg.sequence} [{who}] {body}".rstrip()


def project_for_member_windowed(
    messages: list[RoomMessage],
    target_member: str,
    *,
    recent_n: int = _DEFAULT_RECENT_N,
) -> list[ProjectedMessage]:
    """Raft AX 改造 3 · "summary + recent N" projection (agent inbox).

    Backward-compatible superset of :func:`project_for_member`:

      * When the room has ``<= recent_n`` rows (or ``recent_n <= 0``), this
        returns EXACTLY what ``project_for_member`` returns — no digest, no
        behavior change. Small rooms are unaffected.
      * When the room is larger, the older tail is collapsed into a SINGLE
        digest message (role="user") — one short attributed line per older
        row, addressable by ``#<seq>`` — followed by the last ``recent_n``
        rows projected verbatim via the normal per-member rules.

    This inverts the historical full-push + hard-4000-char-truncation
    (which the article criticizes as "the room deciding what's worth the
    agent's context") into a compact index the member can act on, while
    still pulling the full text on demand. Deterministic and O(n).
    """
    if not messages:
        return []
    if recent_n <= 0 or len(messages) <= recent_n:
        return project_for_member(messages, target_member)

    older = messages[:-recent_n]
    recent = messages[-recent_n:]

    digest_lines = [_digest_line(m, target_member) for m in older]
    digest_content = (
        f"[room digest — {len(older)} earlier message(s), oldest first; "
        f"use room_fetch_context to read any in full]\n"
        + "\n".join(digest_lines)
    )
    projected: list[ProjectedMessage] = [
        ProjectedMessage(role="user", content=digest_content)
    ]
    projected.extend(project_for_member(recent, target_member))
    return projected


def project_for_observer(
    messages: list[RoomMessage],
) -> list[ProjectedMessage]:
    """Observer sees the raw multi-party stream as user messages — it
    doesn't have "its own turns" in the same sense as a member.

    All messages become role="user" with speaker prefix, except the
    observer's own past synthesis replies (kind=="observer" with no
    tool_calls) which are surfaced as role="assistant".

    The observer's past *routing decisions* (kind=="observer" carrying a
    route_to_member / decompose_and_route tool_call) are DROPPED from its
    self-view. Those rows have empty content and render to the flattened
    prose ``[observer]: routed to member (args: ...)``; fed back as
    role="assistant" they act as few-shot examples that teach the model to
    emit its next decision as prose instead of a structured tool call,
    which the bridge cannot parse (empty route -> fallback member). The
    fresh per-turn classifier + last_routed already carry routing
    continuity, so the observer loses nothing actionable by not seeing its
    own past tool calls.
    """
    if not messages:
        return []
    projected: list[ProjectedMessage] = []
    for msg in messages:
        if msg.sender_kind == "observer" and msg.tool_calls:
            continue
        content = _render_content(msg, target_member="")  # no target
        role = "assistant" if msg.sender_kind == "observer" else "user"
        projected.append(ProjectedMessage(role=role, content=content))
    return projected
