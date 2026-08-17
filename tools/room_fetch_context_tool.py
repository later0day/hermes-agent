"""room_fetch_context — a room member's on-demand context PULL tool.

Design ref: docs/design/agent-room/ax-alignment.md §3 改造 3 (agent inbox).

Raft《Is Having Agents in the Room Meant to Be Chaotic?》's "agent inbox"
insight is to invert push→pull: instead of the room deciding what to flood
into every agent's context (our historical full-history projection + hard
4000-char truncation), notifications/history become *queryable* items the
agent pulls when it has the bandwidth — "the agent decides what is worth
its context, instead of the room deciding for it."

Slice 1 (agent_room_projection.project_for_member_windowed) already
inverted the push side: a member now sees a compact "summary + recent N"
digest instead of the whole truncated history. This tool is the pull side:
it lets the member read any older row IN FULL — by sequence range or by
substring query — only when it actually needs to.

Room scope (which room / which member is asking) is NOT a tool argument —
it is bound by the gateway around the member's turn via
``bind_room_context(...)`` (a ContextVar), exactly like the observer's
route_to_member relies on the active-agent ContextVar. This keeps the LLM
from being able to read a DIFFERENT room's history by passing a forged
room_id.

The handler is defensive: if no room context is bound (e.g. the tool is
somehow reached outside a member turn) it returns a structured error
rather than raising, so a stray call can never crash the member's turn.
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Bound by the gateway around a member's turn. Holds everything the handler
# needs to serve reads scoped to THIS member in THIS room.
_ROOM_CONTEXT: contextvars.ContextVar[Optional["RoomContext"]] = (
    contextvars.ContextVar("agent_room_fetch_context", default=None)
)

# Safety caps so a single pull can't blow the context window back open —
# the whole point of the inbox is bounded context.
_MAX_RANGE_ROWS = 50
_MAX_QUERY_HITS = 20
_MAX_ROW_CHARS = 2000


@dataclass(frozen=True)
class RoomContext:
    """What room_fetch_context is scoped to for the current member turn."""
    messages_store: Any   # AgentRoomMessagesStore
    room_id: str
    member: str


class _BoundToken:
    """Returned by bind_room_context so the caller can reset() it."""

    __slots__ = ("_token",)

    def __init__(self, token: contextvars.Token) -> None:
        self._token = token

    def reset(self) -> None:
        try:
            _ROOM_CONTEXT.reset(self._token)
        except Exception:  # noqa: BLE001 — reset must never crash teardown
            pass


def bind_room_context(
    messages_store: Any, room_id: str, member: str
) -> _BoundToken:
    """Bind the room scope for the current member turn. Returns a token
    the caller MUST reset() in a finally block (like set_secret_scope)."""
    token = _ROOM_CONTEXT.set(
        RoomContext(messages_store=messages_store, room_id=room_id, member=member)
    )
    return _BoundToken(token)


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _render_rows(rows, member: str) -> list[dict[str, Any]]:
    """Render store rows for the member, verbatim (full text, capped per
    row) — this is the PULL path, so it deliberately shows more than the
    digest's short preview."""
    out: list[dict[str, Any]] = []
    for m in rows:
        body = m.content or ""
        if len(body) > _MAX_ROW_CHARS:
            body = body[: _MAX_ROW_CHARS - 15] + "...[truncated]"
        # Attribution: own rows marked "me", everyone else by name/kind.
        if m.sender_kind == "member" and m.sender_name == member:
            who = "me"
        elif m.sender_kind == "user":
            who = "user"
        elif m.sender_kind == "observer":
            who = "observer"
        elif m.sender_kind == "tool_result":
            who = f"tool:{m.sender_name}"
        else:
            who = m.sender_name or m.sender_kind
        out.append({"seq": m.sequence, "who": who, "content": body})
    return out


def room_fetch_context(
    query: Optional[str] = None,
    start_seq: Optional[int] = None,
    end_seq: Optional[int] = None,
) -> str:
    """Pull older room history that the digest only indexed.

    Provide EITHER a ``query`` (case-insensitive substring match over
    message content) OR a ``start_seq``/``end_seq`` range (inclusive,
    addressing the ``#<seq>`` markers shown in the room digest). Returns
    a JSON object ``{"ok": true, "rows": [{"seq","who","content"}, ...]}``.
    """
    ctx = _ROOM_CONTEXT.get()
    if ctx is None:
        return _err(
            "no room context bound — room_fetch_context is only usable "
            "inside a room member's turn"
        )
    store = ctx.messages_store
    try:
        all_rows = store.list_messages(ctx.room_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("room_fetch_context: list_messages failed: %s", exc)
        return _err(f"failed to read room history: {exc}")

    has_range = start_seq is not None or end_seq is not None
    has_query = bool(query and str(query).strip())

    if has_range and has_query:
        return _err("provide either query OR a seq range, not both")
    if not has_range and not has_query:
        return _err("provide a query or a start_seq/end_seq range")

    if has_range:
        lo = int(start_seq) if start_seq is not None else 0
        hi = int(end_seq) if end_seq is not None else 10**18
        if lo > hi:
            lo, hi = hi, lo
        hits = [m for m in all_rows if lo <= m.sequence <= hi]
        truncated = len(hits) > _MAX_RANGE_ROWS
        hits = hits[:_MAX_RANGE_ROWS]
        rows = _render_rows(hits, ctx.member)
        return json.dumps(
            {"ok": True, "mode": "range", "start_seq": lo, "end_seq": hi,
             "count": len(rows), "truncated": truncated, "rows": rows},
            ensure_ascii=False,
        )

    needle = str(query).strip().lower()
    hits = [m for m in all_rows if needle in (m.content or "").lower()]
    truncated = len(hits) > _MAX_QUERY_HITS
    hits = hits[:_MAX_QUERY_HITS]
    rows = _render_rows(hits, ctx.member)
    return json.dumps(
        {"ok": True, "mode": "query", "query": query,
         "count": len(rows), "truncated": truncated, "rows": rows},
        ensure_ascii=False,
    )


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

ROOM_FETCH_CONTEXT_SCHEMA = {
    "name": "room_fetch_context",
    "description": (
        "Pull older room history in full. Your turn starts with a compact "
        "room digest that only shows short previews of earlier messages "
        "(each tagged with a #<seq> index). Use this tool when you need the "
        "FULL text of one or more of those earlier messages — for example "
        "to read what another member said earlier, or to check an earlier "
        "user request. Provide EITHER `query` (find messages containing a "
        "phrase) OR `start_seq`/`end_seq` (read a range by the #<seq> "
        "indices from the digest). Only pull what you actually need — the "
        "digest already gives you the gist of everything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Case-insensitive substring to search for across all "
                    "room messages. Use to find earlier messages by content."
                ),
            },
            "start_seq": {
                "type": "integer",
                "description": (
                    "Inclusive lower #<seq> bound for a range read "
                    "(the numbers shown in the room digest)."
                ),
            },
            "end_seq": {
                "type": "integer",
                "description": "Inclusive upper #<seq> bound for a range read.",
            },
        },
        "required": [],
    },
}

from tools.registry import registry as _registry

_registry.register(
    name="room_fetch_context",
    toolset="room_member",
    schema=ROOM_FETCH_CONTEXT_SCHEMA,
    handler=room_fetch_context,
    check_fn=None,
    is_async=False,
    description="Pull older room history in full, by query or #seq range (member-only).",
    emoji="📥",
)
