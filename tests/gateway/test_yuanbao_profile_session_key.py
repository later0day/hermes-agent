"""Yuanbao dispatch state must use the routed profile namespace."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.yuanbao import DispatchMiddleware
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_dispatch_queue_key_uses_source_profile(monkeypatch):
    captured = {}

    def _build_session_key(source, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return "agent:worker:yuanbao:group:chat-1"

    monkeypatch.setattr(
        "gateway.platforms.yuanbao.build_session_key", _build_session_key
    )
    queue = asyncio.Queue()
    adapter = SimpleNamespace(
        name="yuanbao",
        config=SimpleNamespace(extra={}),
        _session_key_profile=lambda source: source.profile,
        _group_queues={"agent:worker:yuanbao:group:chat-1": queue},
    )
    source = SessionSource(
        platform=Platform.YUANBAO,
        chat_id="chat-1",
        chat_type="group",
        profile="worker",
    )
    ctx = SimpleNamespace(
        adapter=adapter,
        source=source,
        chat_type="group",
    )
    next_fn = AsyncNoop()

    await DispatchMiddleware().handle(ctx, next_fn)

    assert captured["profile"] == "worker"
    assert queue.qsize() == 1
    assert next_fn.called is True


class AsyncNoop:
    def __init__(self):
        self.called = False

    async def __call__(self):
        self.called = True
