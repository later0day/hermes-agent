"""Profile-scoped cache eviction for the gateway /undo command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_undo_evicts_resolved_named_profile_session_key():
    runner = GatewayRunner.__new__(GatewayRunner)
    entry = SimpleNamespace(
        session_id="session-worker",
        session_key="agent:worker:dingtalk:group:chat-1:user-1",
        last_prompt_tokens=123,
    )
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=entry),
        rewind_session=AsyncMock(
            return_value={
                "target_text": "previous request",
                "turns_undone": 1,
                "rewound_count": 2,
            }
        ),
    )
    runner._evict_cached_agent = MagicMock()
    event = MessageEvent(
        text="/undo",
        source=SessionSource(
            platform=Platform.DINGTALK,
            chat_id="chat-1",
            chat_type="group",
            user_id="user-1",
            profile="worker",
        ),
    )

    result = await runner._handle_undo_command(event)

    assert "previous request" in result
    assert entry.last_prompt_tokens == 0
    runner._evict_cached_agent.assert_called_once_with(entry.session_key)
