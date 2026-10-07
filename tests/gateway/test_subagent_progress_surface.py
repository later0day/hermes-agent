"""Subagent progress shows on the turn status card only.

The card port also pushed subagent tool/lifecycle events into the plain progress queue, so every
platform with tool progress on started getting subagent bubbles it never had; and the card
prefixed 🔀 onto lines delegate_tool_progress had already prefixed.
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


def test_without_a_card_subagent_events_do_not_become_progress_bubbles():
    turn, ctx = _turn(None)
    turn.progress_callback("subagent.progress", preview="🔀 [1] read_file")
    turn.progress_callback("subagent.tool", tool_name="terminal", preview="ls")
    assert ctx.progress_queue.empty()


def test_the_card_shows_subagent_progress_once_prefixed():
    card = _Card()
    turn, _ = _turn(card)
    turn.progress_callback("subagent.progress", preview="🔀 [1] read_file")
    turn.progress_callback("subagent.start", preview="scan the repo")
    assert card.commentary == ["🔀 [1] read_file", "🔀 scan the repo"]
