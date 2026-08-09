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
        # Observer's route_to_member decision — surface reason as context
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    if fn.get("name") == "route_to_member":
                        args = fn.get("arguments") or "{}"
                        return f"[observer]: routed to member (args: {_truncate(args, 200)})"
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


def project_for_observer(
    messages: list[RoomMessage],
) -> list[ProjectedMessage]:
    """Observer sees the raw multi-party stream as user messages — it
    doesn't have "its own turns" in the same sense as a member.

    All messages become role="user" with speaker prefix, except the
    observer's own past routes (kind=="observer") which are surfaced as
    role="assistant" so the observer can see its own routing history.
    """
    if not messages:
        return []
    projected: list[ProjectedMessage] = []
    for msg in messages:
        content = _render_content(msg, target_member="")  # no target
        role = "assistant" if msg.sender_kind == "observer" else "user"
        projected.append(ProjectedMessage(role=role, content=content))
    return projected
