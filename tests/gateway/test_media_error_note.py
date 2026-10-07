"""Attachments that failed before they became files still reach the agent as a note.

DingTalk records per-attachment download failures in ``MessageEvent.media_errors`` and lets an
event through with no text and no media; nothing downstream read them, so the agent got an empty
turn. ``_prepare_inbound_message_text`` serves both fresh and queued inbound messages.
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_a_failed_attachment_reaches_the_agent_as_a_note():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DINGTALK: PlatformConfig(enabled=True)})
    runner.adapters = {}
    runner._pending_native_image_paths_by_session = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    source = SessionSource(platform=Platform.DINGTALK, chat_id="c1", chat_type="dm", user_id="u1")
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source,
                         media_errors=["photo.png: download expired"])

    prepared = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert "[Media attachment unavailable: photo.png: download expired]" in prepared
