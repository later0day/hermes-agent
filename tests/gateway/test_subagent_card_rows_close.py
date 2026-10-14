"""A delegated child's tool rows on the turn status card must close when the child's tool finishes.

The relay dropped the child's tool.completed (``TASK_TOOL_COMPLETED: None``), so every subagent row
kept spinning with a growing timer until the card closed. Rows are paired by the child's tool_call_id
so two parallel same-name calls close the right row.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.turn_status_card import (
    _SUBAGENT_TOOL_EVENTS, TurnStatusCardConfig, TurnStatusCardCoordinator,
)
from tools.delegate_tool import _build_child_progress_callback


class _Adapter:
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SimpleNamespace(success=True, message_id="status-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        return SimpleNamespace(success=True, message_id=message_id)


def _relay_into(card):
    parent = MagicMock()
    parent._delegate_spinner = None

    def parent_cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
        if event_type in _SUBAGENT_TOOL_EVENTS:  # what route_progress_event does with a live card
            card.on_tool_progress(_SUBAGENT_TOOL_EVENTS[event_type], tool_name, preview, args, **kwargs)

    parent.tool_progress_callback = parent_cb
    return _build_child_progress_callback(0, "goal", parent, subagent_id="sa-1")


@pytest.mark.asyncio
async def test_subagent_tool_rows_close_by_call_id():
    card = TurnStatusCardCoordinator(adapter=_Adapter(), chat_id="c", metadata={},
                                     config=TurnStatusCardConfig(edit_interval=0.01, preview_max_len=24))
    task = asyncio.create_task(card.run())
    child_cb = _relay_into(card)

    child_cb("tool.started", "terminal", "sleep 5", {}, tool_call_id="call-a")
    child_cb("tool.started", "terminal", "ls", {}, tool_call_id="call-b")
    child_cb("tool.completed", "terminal", None, None, tool_call_id="call-b", duration=0.2, is_error=False)

    async def _settled(pred):
        for _ in range(200):
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError({k: e.status for k, e in card._tools.items()})

    await _settled(lambda: len(card._tools) == 2 and any(e.status == "completed" for e in card._tools.values()))
    by_preview = {e.preview: e.status for e in card._tools.values()}
    assert by_preview == {"sleep 5": "running", "ls": "completed"}, by_preview

    child_cb("tool.completed", "terminal", None, None, tool_call_id="call-a", duration=5.0, is_error=True,
             result='{"success": false, "error": "boom"}')
    await _settled(lambda: all(e.status != "running" for e in card._tools.values()))
    assert {e.preview: e.status for e in card._tools.values()} == {"sleep 5": "failed", "ls": "completed"}
    card.finish()
    await task
