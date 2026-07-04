from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import (
    GatewayRunner,
    _RECENT_IMAGE_RESEND_NOT_HANDLED,
    _find_latest_attached_image_path,
    _wants_recent_image_resend,
)
from gateway.session import SessionSource


class _FakeSessionDB:
    def __init__(self, messages):
        self.messages = messages
        self.appended = []

    def get_messages(self, session_id):
        return self.messages

    def append_message(self, session_id, role, content, **kwargs):
        self.appended.append((session_id, role, content, kwargs))


def _dingtalk_source():
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id="cid-test",
        chat_type="group",
        user_id="user-test",
        user_name="tester",
    )


def _dingtalk_event(text, *, media_urls=None):
    source = _dingtalk_source()
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-test",
        media_urls=media_urls or [],
    )


def _telegram_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="user-test",
        user_name="tester",
    )


def test_recent_image_resend_intent_detection():
    assert _wants_recent_image_resend("图片重新发送给我")
    assert _wants_recent_image_resend("刚才那个图片发给我")
    assert _wants_recent_image_resend("resend the last image")
    assert not _wants_recent_image_resend("这张图片里是什么")
    assert not _wants_recent_image_resend("重新解释一下这段话")


def test_find_latest_attached_image_path_uses_newest_existing_file(tmp_path):
    old_image = tmp_path / "old.jpg"
    new_image = tmp_path / "new.png"
    old_image.write_bytes(b"old")
    new_image.write_bytes(b"new")

    messages = [
        {"role": "user", "content": f"[Image attached at: {old_image}]"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "[Image attached at: /missing/image.png]"},
        {"role": "user", "content": f"看这个\n[Image attached at: {new_image}]"},
    ]

    assert _find_latest_attached_image_path(messages) == str(new_image)


@pytest.mark.asyncio
async def test_gateway_resends_recent_image_via_native_adapter(tmp_path):
    image_path = tmp_path / "latest.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session_db = _FakeSessionDB(
        [{"role": "user", "content": f"之前的图片\n[Image attached at: {image_path}]"}]
    )
    adapter = type(
        "FakeAdapter",
        (),
        {
            "send_image_file": AsyncMock(
                return_value=SendResult(success=True, message_id="native-img")
            )
        },
    )()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.DINGTALK: adapter}

    event = _dingtalk_event("图片重新发送给我")
    result = await runner._maybe_resend_recent_image(
        event,
        event.source,
        session_db,
        "session-id",
    )

    assert result is None
    adapter.send_image_file.assert_awaited_once_with(
        chat_id="cid-test",
        image_path=str(image_path),
        caption="最近一张图片重新发给你。",
        reply_to="msg-test",
        metadata=None,
    )
    assert session_db.appended == [
        (
            "session-id",
            "user",
            "图片重新发送给我",
            {"platform_message_id": "msg-test"},
        ),
        (
            "session-id",
            "assistant",
            f"[resent image attachment: {image_path}]",
            {},
        ),
    ]


@pytest.mark.asyncio
async def test_gateway_recent_image_resend_ignores_non_resend_text(tmp_path):
    image_path = tmp_path / "latest.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session_db = _FakeSessionDB(
        [{"role": "user", "content": f"[Image attached at: {image_path}]"}]
    )
    adapter = type(
        "FakeAdapter",
        (),
        {"send_image_file": AsyncMock()},
    )()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.DINGTALK: adapter}

    event = _dingtalk_event("这张图片里是什么")
    result = await runner._maybe_resend_recent_image(
        event,
        event.source,
        session_db,
        "session-id",
    )

    assert result is _RECENT_IMAGE_RESEND_NOT_HANDLED
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_recent_image_resend_is_dingtalk_only(tmp_path):
    image_path = tmp_path / "latest.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session_db = _FakeSessionDB(
        [{"role": "user", "content": f"[Image attached at: {image_path}]"}]
    )
    adapter = type(
        "FakeAdapter",
        (),
        {"send_image_file": AsyncMock()},
    )()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = _telegram_source()
    event = MessageEvent(
        text="图片重新发送给我",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-test",
    )

    result = await runner._maybe_resend_recent_image(
        event,
        source,
        session_db,
        "session-id",
    )

    assert result is _RECENT_IMAGE_RESEND_NOT_HANDLED
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_recent_image_resend_reports_missing_history():
    session_db = _FakeSessionDB([{"role": "assistant", "content": "no image"}])
    adapter = type(
        "FakeAdapter",
        (),
        {"send_image_file": AsyncMock()},
    )()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.DINGTALK: adapter}

    event = _dingtalk_event("刚才那个图片发给我")
    result = await runner._maybe_resend_recent_image(
        event,
        event.source,
        session_db,
        "session-id",
    )

    assert "没找到" in result
    adapter.send_image_file.assert_not_awaited()
