"""Deterministic @mention routing for Agent Rooms.

Ported from hermes-studio's group-chat/mention-routing.ts. The room's
observer LLM decides the FIRST hop (which member answers a fresh user
message); once a member is replying, member-to-member handoff is driven
by explicit ``@membername`` tokens in the reply — parsed here with zero
LLM involvement, exactly like the reference implementation.

Why deterministic: the observer's structured tool-call routing is the
fragile part of the pipeline (model must reliably emit a parseable
tool_call). Member handoff does not need that fragility — a member simply
writes ``@finance`` and this pure function resolves the target. 100%
predictable, no tool_choice, no projection few-shot concerns.

Rules (mirroring mention-routing.ts):
  * ``@all`` (case-insensitive) → every member except the sender.
  * ``@<member>`` → that member, if the token has clean boundaries.
  * The sender is always excluded (a member can't hand off to itself).
  * Tokens inside ``<quoted_message>...</quoted_message>`` blocks are
    masked out so forwarding someone else's words can't misfire.
  * Boundary check keeps ASCII identifiers / emails from matching:
    the char before ``@`` must be a non-``[A-Za-z0-9_]`` (or start),
    and the char after the name must be whitespace / punctuation /
    CJK / end-of-string.
"""

from __future__ import annotations

import re

ALL_AGENTS_MENTION = "all"

# Punctuation that legitimately terminates a mention token.
_AFTER_BOUNDARY = set(".,!?;:，。！？；：)]}>")

_QUOTED_MESSAGE_BLOCK_RE = re.compile(
    r"<quoted_message(?:\s[^>]*)?>.*?</quoted_message>",
    re.IGNORECASE | re.DOTALL,
)

_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_]")
_WS_RE = re.compile(r"\s")


def _mask_quoted_blocks(content: str) -> str:
    """Replace every non-newline char inside <quoted_message> blocks with a
    space so mentions quoted from another turn don't route."""
    def _blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    return _QUOTED_MESSAGE_BLOCK_RE.sub(_blank, content)


def _is_before_boundary(char: str | None) -> bool:
    return char is None or not _IDENT_CHAR_RE.match(char)


def _is_after_boundary(char: str | None) -> bool:
    return char is None or bool(_WS_RE.match(char)) or char in _AFTER_BOUNDARY


def _find_mention_ranges(content: str, mention_name: str) -> list[tuple[int, int]]:
    if not content or not mention_name:
        return []
    routable = _mask_quoted_blocks(content)
    content_lower = routable.lower()
    mention_lower = mention_name.lower()
    needle = f"@{mention_lower}"
    ranges: list[tuple[int, int]] = []
    from_index = 0
    while from_index < len(content):
        at_index = content_lower.find(needle, from_index)
        if at_index == -1:
            break
        start = at_index
        end = at_index + len(mention_name) + 1
        before = routable[start - 1] if start - 1 >= 0 else None
        after = routable[end] if end < len(routable) else None
        if _is_before_boundary(before) and _is_after_boundary(after):
            ranges.append((start, end))
        from_index = at_index + 1
    return ranges


def is_agent_mentioned(content: str, agent_name: str) -> bool:
    return len(_find_mention_ranges(content, agent_name)) > 0


def is_all_agents_mentioned(content: str) -> bool:
    return is_agent_mentioned(content, ALL_AGENTS_MENTION)


def resolve_mention_targets(
    members: tuple[str, ...] | list[str],
    content: str,
    sender: str,
) -> list[str]:
    """Return the members explicitly @mentioned in ``content``.

    Parameters
    ----------
    members : roster of member profile names.
    content : the message text to scan for ``@name`` / ``@all`` tokens.
    sender : the member that authored ``content`` — always excluded so a
        member cannot hand off to itself (mirrors ``isSenderAgent``).

    Returns an ordered, de-duplicated list of matched member names.
    Empty list means no handoff (the chain ends).
    """
    candidates = [m for m in members if m != sender]
    if is_all_agents_mentioned(content):
        return list(candidates)
    seen: set[str] = set()
    out: list[str] = []
    for m in candidates:
        if m in seen:
            continue
        if is_agent_mentioned(content, m):
            seen.add(m)
            out.append(m)
    return out


def strip_mention_tokens(content: str, own_name: str) -> str:
    """Remove ``@all`` / ``@own_name`` routing tokens from a member's own
    inbound text (mirrors ``stripMentionRoutingTokens``) so the member's
    model doesn't see the plumbing token addressed to it."""
    ranges_by_key: dict[str, tuple[int, int]] = {}
    for rng in [
        *_find_mention_ranges(content, ALL_AGENTS_MENTION),
        *_find_mention_ranges(content, own_name),
    ]:
        ranges_by_key[f"{rng[0]}:{rng[1]}"] = rng
    ranges = sorted(ranges_by_key.values(), key=lambda r: r[0], reverse=True)
    result = content
    for start, end in ranges:
        result = result[:start] + result[end:]
    result = re.sub(r"^[\s,，:：;；.!?。！？]+", "", result)
    result = re.sub(r"[\s,，:：;；]+$", "", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    return result.strip()
