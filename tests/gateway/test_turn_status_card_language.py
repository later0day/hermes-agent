"""The turn status card speaks the profile's display language.

Its chrome was hardcoded Chinese, so every user of a status-card platform saw Chinese
whatever ``display.language`` said.
"""
import asyncio
import re
from types import SimpleNamespace

import pytest

from agent.i18n import reset_language_cache
from gateway.turn_status_card import TurnStatusCardConfig, TurnStatusCardCoordinator

CJK = re.compile(r"[一-鿿]")


class _Adapter:
    def __init__(self):
        self.contents = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.contents.append(content)
        return SimpleNamespace(success=True, message_id="status-1")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.contents.append(content)
        return SimpleNamespace(success=True, message_id=message_id)


async def _final_card() -> str:
    adapter = _Adapter()
    card = TurnStatusCardCoordinator(adapter=adapter, chat_id="c", metadata={},
                                     config=TurnStatusCardConfig(edit_interval=0.01))
    task = asyncio.create_task(card.run())
    card.on_tool_progress("tool.started", "read_file", "a.txt", {"path": "a.txt"}, tool_call_id="1", index=0)
    card.on_tool_progress("tool.completed", "read_file", None, None, duration=0.2, is_error=True,
                          tool_call_id="1", index=0)
    for _ in range(100):
        if adapter.contents:
            break
        await asyncio.sleep(0.01)
    card.finish()
    await task
    return adapter.contents[-1]


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_card_chrome_follows_display_language(lang, monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    reset_language_cache()
    try:
        final = asyncio.run(_final_card())
    finally:
        reset_language_cache()
    assert bool(CJK.search(final)) is (lang == "zh"), final
