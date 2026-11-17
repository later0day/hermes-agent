"""Subagent progress surfaces: in-card when the turn has a status card, else 🔀 progress bubbles.

The fork rendered a delegated child's tool calls and lifecycle text as 🔀 bubbles on platforms without
a card (origin/main gateway/run.py progress_callback); the 2026-10-08 port wrongly dropped them, so a
long delegation was silent there. The card must not double delegate_tool_progress's 🔀 prefix.
"""
import queue
from unittest.mock import MagicMock

from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


class _Runner:
    def _delivery_adapter_for(self, source):
        return None


class _Card:
    def __init__(self):
        self.commentary = []

    def on_commentary(self, text):
        self.commentary.append(text)

    def on_tool_progress(self, *args, **kwargs):
        pass


def _turn(card):
    ctx = TurnContext(source=MagicMock(), _run_still_current=lambda: True, progress_queue=queue.Queue(),
                      tool_progress_enabled=True, _loop_for_step=None)
    ctx.turn_status_card_holder[0] = card
    return TurnRunner(_Runner(), ctx), ctx


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_without_a_card_subagent_progress_becomes_bubbles():
    turn, ctx = _turn(None)
    turn.progress_callback("subagent.start", preview="scan the repo")
    turn.progress_callback("subagent.tool", tool_name="terminal", preview="ls")
    turn.progress_callback("subagent.progress", preview="🔀 [1] read_file")
    turn.progress_callback("subagent.tool.completed", tool_name="terminal")
    assert _drain(ctx.progress_queue) == ["🔀 scan the repo", "🔀 terminal — ls", "🔀 [1] read_file"]


def test_without_a_card_tool_progress_off_keeps_subagents_quiet():
    turn, ctx = _turn(None)
    ctx.tool_progress_enabled = False
    turn.progress_callback("subagent.start", preview="scan the repo")
    turn.progress_callback("subagent.tool", tool_name="terminal", preview="ls")
    assert ctx.progress_queue.empty()


def test_the_card_shows_subagent_progress_once_prefixed():
    card = _Card()
    turn, _ = _turn(card)
    turn.progress_callback("subagent.progress", preview="🔀 [1] read_file")
    turn.progress_callback("subagent.start", preview="scan the repo")
    assert card.commentary == ["🔀 [1] read_file", "🔀 scan the repo"]
