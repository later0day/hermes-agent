"""The long-running heartbeat must declare its editable card lifecycle.

``_notify_long_running`` in gateway/run.py sends one "⏳ Working — N min"
message and then *edits that same message* on every later interval, so the
first send has to be marked editable.

Upstream's DingTalk adapter derived the card lifecycle from ``reply_to``
(absent ⇒ leave the card open), which made this producer correct by accident.
The fork replaced that with an explicit ``metadata["expect_edits"]`` contract
— better for genuine one-shot sends (background notices, queued follow-ups),
but it silently regressed every producer that edits its own message without
declaring it.  The heartbeat was the one live path that did, so the card was
finalized on create and the next interval's edit reopened it: the
closed→streaming flicker upstream's contract avoided.

The adapter-side property is covered by
``TestCardLifecycle::test_expect_edits_send_then_edit_never_reopens_card``.
"""

from __future__ import annotations

import inspect
import re

import gateway.run as run_module


def _heartbeat_source() -> str:
    """Source of the ``_notify_long_running`` closure inside process_message."""
    src = inspect.getsource(run_module)
    start = src.index("async def _notify_long_running()")
    # The closure ends where the task that runs it is scheduled.
    end = src.index("_notify_task = asyncio.create_task(_notify_long_running())", start)
    return src[start:end]


def test_heartbeat_send_declares_expect_edits():
    """The heartbeat's send() carries metadata["expect_edits"] = True."""
    body = _heartbeat_source()
    assert "_notify_adapter.send(" in body, "heartbeat send() call moved or was renamed"
    assert re.search(
        r'_heartbeat_metadata\["expect_edits"\]\s*=\s*True', body
    ), "heartbeat send() no longer declares expect_edits — editable cards will flicker"


def test_heartbeat_send_passes_the_expect_edits_metadata():
    """The dict carrying expect_edits is the one actually handed to send()."""
    body = _heartbeat_source()
    send_call = body[body.index("_notify_adapter.send(") :]
    send_call = send_call[: send_call.index(")\n")]
    assert "metadata=_heartbeat_metadata" in send_call, (
        "heartbeat builds expect_edits metadata but sends a different dict"
    )


def test_heartbeat_metadata_preserves_thread_routing():
    """expect_edits is added to — not substituted for — the thread metadata."""
    body = _heartbeat_source()
    assert "_non_conversational_metadata(" in body, (
        "heartbeat dropped _non_conversational_metadata; Discord lifecycle "
        "marking and thread routing would be lost"
    )
    assert "_status_thread_metadata" in body, (
        "heartbeat dropped _status_thread_metadata; heartbeats would leave the thread"
    )


def test_heartbeat_edits_before_sending_new():
    """Edit-in-place is still attempted first, so send() is the fallback."""
    body = _heartbeat_source()
    assert body.index("_notify_adapter.edit_message(") < body.index(
        "_notify_adapter.send("
    ), "heartbeat must try edit_message before falling back to a fresh send"
