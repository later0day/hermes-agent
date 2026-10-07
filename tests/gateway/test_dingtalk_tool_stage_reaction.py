"""DingTalk stage reactions fire when a tool starts on the agent's worker thread.

``notify_tool_started`` is called from the progress callback, which runs in the agent's executor
thread, where there is no running event loop: a bare ``create_task`` raised there, the error was
swallowed, and the stage emoji never changed.
"""
from __future__ import annotations

import asyncio
import threading
import types

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.dingtalk.adapter import DingTalkAdapter


@pytest.mark.asyncio
async def test_tool_start_on_a_worker_thread_swaps_the_stage_reaction():
    adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "x", "client_secret": "y"}))
    adapter._loop = asyncio.get_running_loop()  # what connect() records
    sent = []

    async def fake_emotion(msg_id, conversation_id, label, *, recall):
        sent.append((label, recall))

    adapter._send_emotion = fake_emotion
    adapter._message_contexts["c1"] = types.SimpleNamespace(message_id="m", conversation_id="cv")

    worker = threading.Thread(target=adapter.notify_tool_started, args=("c1", "terminal"), kwargs={"preview": "ls"})
    worker.start()
    worker.join()
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.02)

    assert sent, "the stage reaction never reached DingTalk"
    assert any(not recall for _, recall in sent)
