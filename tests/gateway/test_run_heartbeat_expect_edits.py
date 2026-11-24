"""Behavioral contract for the long-running heartbeat's editable card lifecycle.

The first heartbeat is an interim send marked for later edits. Later intervals
edit the returned message id, falling back to a fresh send only while the turn
still owns the session. The adapter-side property is covered by
``TestCardLifecycle::test_expect_edits_send_then_edit_never_reopens_card``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import gateway.run_turn as run_turn_module
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


_THREAD_METADATA = {"thread_id": "thread-1", "message_id": "parent-1"}


class _ControlledIntervals:
    """Release heartbeat intervals explicitly instead of waiting wall-clock time."""

    def __init__(self):
        self._releases = asyncio.Queue()

    async def sleep(self, _seconds):
        release = asyncio.Event()
        await self._releases.put(release)
        await release.wait()

    async def tick(self):
        release = await asyncio.wait_for(self._releases.get(), timeout=5)
        release.set()


class _HeartbeatAdapter:
    def __init__(self, *, edit_succeeds=True):
        self.edit_succeeds = edit_succeeds
        self.on_edit = None
        self.calls = []
        self.observations = asyncio.Queue()
        self._send_count = 0

    async def send(self, chat_id, text, metadata=None):
        self._send_count += 1
        call = SimpleNamespace(
            kind="send",
            chat_id=chat_id,
            text=text,
            metadata=metadata,
            message_id=f"heartbeat-{self._send_count}",
        )
        self.calls.append(call)
        await self.observations.put(call)
        return SimpleNamespace(success=True, message_id=call.message_id)

    async def edit_message(self, chat_id, message_id, text):
        call = SimpleNamespace(
            kind="edit", chat_id=chat_id, text=text, message_id=message_id
        )
        self.calls.append(call)
        if self.on_edit is not None:
            self.on_edit()
        await self.observations.put(call)
        return SimpleNamespace(success=self.edit_succeeds, message_id=message_id)


async def _next_call(adapter):
    return await asyncio.wait_for(adapter.observations.get(), timeout=5)


def _start_heartbeat(monkeypatch, adapter):
    intervals = _ControlledIntervals()
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "1")
    monkeypatch.setattr(
        run_turn_module, "asyncio", SimpleNamespace(sleep=intervals.sleep)
    )

    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._agent_activity_summary = lambda _agent: None
    ownership = {"current": True}
    runner._should_emit_long_running_notification = (
        lambda *_args: ownership["current"]
    )

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        thread_id="thread-1",
    )
    turn_ctx = TurnContext(
        source=source,
        session_key="session-key",
        _status_thread_metadata=dict(_THREAD_METADATA),
    )
    turn_ctx.agent_holder[0] = object()
    disp = SimpleNamespace(
        user_config={},
        platform_key=Platform.DISCORD.value,
        _display_surface_mode=lambda *_args, **_kwargs: "on",
        resolve_display_setting=lambda *_args: False,
    )
    task = asyncio.create_task(
        runner._run_agent_notify_long_running(disp, turn_ctx, [None])
    )
    return task, intervals, ownership


async def _stop_heartbeat(task, intervals, ownership):
    if task.done():
        await task
        return
    ownership["current"] = False
    await intervals.tick()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_heartbeat_send_declares_expect_edits_and_interim_send(monkeypatch):
    """The metadata handed to the first send declares both lifecycle contracts."""
    adapter = _HeartbeatAdapter()
    task, intervals, ownership = _start_heartbeat(monkeypatch, adapter)
    try:
        await intervals.tick()
        sent = await _next_call(adapter)

        assert sent.kind == "send"
        assert sent.metadata["expect_edits"] is True
        assert sent.metadata["_interim_send"] is True
    finally:
        await _stop_heartbeat(task, intervals, ownership)


@pytest.mark.asyncio
async def test_heartbeat_metadata_preserves_thread_routing(monkeypatch):
    """Lifecycle markers augment the Discord thread and status metadata."""
    adapter = _HeartbeatAdapter()
    task, intervals, ownership = _start_heartbeat(monkeypatch, adapter)
    try:
        await intervals.tick()
        sent = await _next_call(adapter)

        assert sent.metadata["thread_id"] == _THREAD_METADATA["thread_id"]
        assert sent.metadata["message_id"] == _THREAD_METADATA["message_id"]
        assert sent.metadata["non_conversational"] is True
    finally:
        await _stop_heartbeat(task, intervals, ownership)


@pytest.mark.asyncio
async def test_heartbeat_edits_returned_message_before_sending_new(monkeypatch):
    """The second interval edits the id returned by the first send."""
    adapter = _HeartbeatAdapter()
    task, intervals, ownership = _start_heartbeat(monkeypatch, adapter)
    try:
        await intervals.tick()
        sent = await _next_call(adapter)
        await intervals.tick()
        edited = await _next_call(adapter)

        assert edited.kind == "edit"
        assert edited.message_id == sent.message_id
        assert [call.kind for call in adapter.calls] == ["send", "edit"]
    finally:
        await _stop_heartbeat(task, intervals, ownership)


@pytest.mark.asyncio
@pytest.mark.parametrize("remains_owned", [True, False])
async def test_heartbeat_failed_edit_falls_back_only_while_owned(
    monkeypatch, remains_owned
):
    """A failed edit sends a replacement only if ownership survives the await."""
    adapter = _HeartbeatAdapter(edit_succeeds=False)
    task, intervals, ownership = _start_heartbeat(monkeypatch, adapter)
    adapter.on_edit = lambda: ownership.update(current=remains_owned)
    try:
        await intervals.tick()
        await _next_call(adapter)
        await intervals.tick()
        edited = await _next_call(adapter)
        assert edited.kind == "edit"

        if remains_owned:
            fallback = await _next_call(adapter)
            assert fallback.kind == "send"
            assert fallback.message_id != edited.message_id
            expected_kinds = ["send", "edit", "send"]
        else:
            await asyncio.wait_for(task, timeout=5)
            expected_kinds = ["send", "edit"]
        assert [call.kind for call in adapter.calls] == expected_kinds
    finally:
        await _stop_heartbeat(task, intervals, ownership)
