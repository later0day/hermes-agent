"""DingTalk Stream liveness watchdog: half-open connection recovery.

The SDK's ``async for raw_message in websocket`` blocks forever on a half-open
socket (TCP still established but no business frames arriving); ``start()``
neither returns nor raises, so ``_run_stream``'s reconnect loop never regains
control. Production incident 2026-08-12: 4.5 days silent with zero exceptions.
The application-layer watchdog actively pings the live websocket and force-closes
it if the pong doesn't return within a timeout, which breaks the SDK's inner
loop and triggers a reconnect.
"""

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.dingtalk.adapter", None)
    return importlib.import_module("plugins.platforms.dingtalk.adapter")


def _fake_adapter(adapter_mod, websocket, *, running=True, ping_interval=0.01, ping_timeout=0.05):
    adapter = object.__new__(adapter_mod.DingTalkAdapter)
    from gateway.config import Platform
    adapter.platform = Platform.DINGTALK
    adapter._running = running
    adapter._ping_interval = ping_interval
    adapter._ping_timeout = ping_timeout
    adapter._stream_client = types.SimpleNamespace(websocket=websocket)
    # _quiet is used by the watchdog for the force-close path
    async def _quiet(coro, *_a, **_kw):
        try:
            await coro
        except Exception:
            pass
    adapter._quiet = _quiet
    return adapter


@pytest.mark.asyncio
async def test_watchdog_leaves_healthy_connection_alone(adapter_mod):
    """A healthy websocket returns its pong promptly, so watchdog must not close it."""
    pong = asyncio.get_running_loop().create_future()
    pong.set_result(None)  # pong resolves immediately
    websocket = MagicMock()
    websocket.ping = AsyncMock(return_value=pong)
    websocket.close = AsyncMock()
    adapter = _fake_adapter(adapter_mod, websocket)

    # Run watchdog briefly then stop
    task = asyncio.create_task(adapter._run_watchdog())
    await asyncio.sleep(0.05)
    adapter._running = False
    await asyncio.wait_for(task, timeout=1.0)

    assert websocket.close.await_count == 0
    assert websocket.ping.await_count >= 1


@pytest.mark.asyncio
async def test_watchdog_forces_close_on_pong_timeout(adapter_mod):
    """A half-open connection never delivers the pong; watchdog must force close."""
    never_resolves = asyncio.get_running_loop().create_future()  # pong never arrives
    websocket = MagicMock()
    websocket.ping = AsyncMock(return_value=never_resolves)
    websocket.close = AsyncMock()
    adapter = _fake_adapter(adapter_mod, websocket, ping_timeout=0.03)

    task = asyncio.create_task(adapter._run_watchdog())
    await asyncio.sleep(0.1)  # long enough for one ping + timeout
    adapter._running = False
    # cancel the never-resolving future so the awaiting task can unwind
    never_resolves.cancel()
    await asyncio.wait_for(task, timeout=1.0)

    assert websocket.close.await_count >= 1


@pytest.mark.asyncio
async def test_watchdog_skips_probe_when_websocket_absent(adapter_mod):
    """Mid-reconnect ``self._stream_client.websocket`` is None; watchdog must not crash."""
    adapter = _fake_adapter(adapter_mod, websocket=None)

    task = asyncio.create_task(adapter._run_watchdog())
    await asyncio.sleep(0.05)
    adapter._running = False
    await asyncio.wait_for(task, timeout=1.0)
    # No exception raised, task exited cleanly


@pytest.mark.asyncio
async def test_watchdog_exits_on_cancellation_without_force_close(adapter_mod):
    """CancelledError inside the ping await must return cleanly (no force-close).

    The watchdog's ``except asyncio.CancelledError: return`` clause treats cancellation
    as an orderly shutdown, not a half-open signal — otherwise ``disconnect()`` cancelling
    the watchdog would race a spurious ``websocket.close()`` into an already-tearing-down
    socket.
    """
    hung = asyncio.get_running_loop().create_future()
    websocket = MagicMock()
    websocket.ping = AsyncMock(return_value=hung)
    websocket.close = AsyncMock()
    adapter = _fake_adapter(adapter_mod, websocket, ping_timeout=10.0)

    task = asyncio.create_task(adapter._run_watchdog())
    await asyncio.sleep(0.02)  # let the ping enter its wait_for
    task.cancel()
    hung.cancel()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    # Force-close is NOT called on cancellation.
    assert websocket.close.await_count == 0


def test_env_int_uses_defaults_and_clamps_below_minimum(adapter_mod, monkeypatch):
    """Watchdog tunables must fall back to their constants on empty/invalid env."""
    monkeypatch.delenv("DINGTALK_STREAM_PING_INTERVAL", raising=False)
    monkeypatch.delenv("DINGTALK_STREAM_PING_TIMEOUT", raising=False)
    assert adapter_mod._env_int("DINGTALK_STREAM_PING_INTERVAL", adapter_mod.STREAM_PING_INTERVAL) == 60
    assert adapter_mod._env_int("DINGTALK_STREAM_PING_TIMEOUT", adapter_mod.STREAM_PING_TIMEOUT) == 20

    monkeypatch.setenv("DINGTALK_STREAM_PING_INTERVAL", "not-a-number")
    assert adapter_mod._env_int("DINGTALK_STREAM_PING_INTERVAL", 60) == 60

    monkeypatch.setenv("DINGTALK_STREAM_PING_INTERVAL", "0")
    assert adapter_mod._env_int("DINGTALK_STREAM_PING_INTERVAL", 60, minimum=1) == 60

    monkeypatch.setenv("DINGTALK_STREAM_PING_INTERVAL", "45")
    assert adapter_mod._env_int("DINGTALK_STREAM_PING_INTERVAL", 60) == 45


@pytest.mark.asyncio
async def test_disconnect_cancels_watchdog_before_websocket_close(adapter_mod):
    """Regression: watchdog cancellation must precede the shutdown websocket close so it
    can't observe the close and race in a spurious 'half-open' reconnect."""
    adapter = object.__new__(adapter_mod.DingTalkAdapter)
    from gateway.config import Platform
    adapter.platform = Platform.DINGTALK
    adapter._running = True
    adapter._streaming_cards = {}
    adapter._mention_patterns = []

    # Sequence recorder: watchdog cancel MUST happen before the websocket close.
    events: list[str] = []

    async def slow_watchdog():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            events.append("watchdog_cancelled")
            raise

    adapter._watchdog_task = asyncio.create_task(slow_watchdog())
    await asyncio.sleep(0)  # let the task actually start

    close_mock = AsyncMock(side_effect=lambda: events.append("websocket_closed"))
    websocket = MagicMock()
    websocket.close = close_mock
    adapter._stream_client = types.SimpleNamespace(websocket=websocket, close=lambda: None)
    adapter._stream_task = None
    adapter._http_client = None
    adapter._card_sdk = None
    adapter._session_webhooks = {}
    adapter._message_contexts = {}
    adapter._done_emoji_fired = set()
    adapter._dedup = types.SimpleNamespace(clear=lambda: None)
    adapter._bg_tasks = set()

    # No-ops for the disconnect helpers that touch other subsystems
    adapter._mark_disconnected = lambda: None
    async def _quiet(coro, *_a, **_kw):
        try:
            await coro
        except Exception:
            pass
    adapter._quiet = _quiet
    async def _close_streaming_siblings(_chat_id):
        return None
    adapter._close_streaming_siblings = _close_streaming_siblings

    await adapter.disconnect()

    assert "watchdog_cancelled" in events, "watchdog was never cancelled"
    assert "websocket_closed" in events, "websocket was never closed"
    assert events.index("watchdog_cancelled") < events.index("websocket_closed"), \
        f"watchdog must be cancelled before websocket close; got order {events}"
    assert adapter._watchdog_task is None
