"""DingTalk outbound image/document delivery keeps the fork's fallbacks (0fd90da47b).

The adapter split (c71449e4a0) sent every image through the DM-only card_1_0 path with no fallback, so a
group image (or any card failure) was simply not delivered; send_document dropped captions, failed on a
URL, and omitted fileType.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from gateway.platforms.base import SendResult
from tests.gateway.test_dingtalk import _fake_dingtalk_optional_sdks, _make_gating_adapter  # noqa: F401


def _adapter(monkeypatch, *, card_ok=True, native_ok=True):
    adapter = _make_gating_adapter(monkeypatch)
    adapter._upload_robot_media = AsyncMock(return_value=SendResult(success=True, message_id="media-1"))
    adapter._send_robot_card_1_0_image = AsyncMock(return_value=SendResult(success=card_ok, error=None if card_ok else "card down"))
    adapter._send_robot_native_message = AsyncMock(return_value=SendResult(success=native_ok, error=None if native_ok else "native down"))
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    return adapter


def _image(tmp_path):
    path = tmp_path / "chart.png"
    path.write_bytes(b"png")
    return str(path)


def _native_keys(adapter):
    return [c.kwargs["msg_key"] for c in adapter._send_robot_native_message.await_args_list]


def test_group_image_skips_the_dm_card_and_sends_caption_plus_native_image(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    result = asyncio.run(adapter.send_image_file("cid-group", _image(tmp_path), caption="Q3",
                                                 metadata={"dingtalk_conversation_type": "2"}))
    assert result.success
    adapter._send_robot_card_1_0_image.assert_not_awaited()
    assert adapter.send.await_args.kwargs["content"] == "Q3"
    assert _native_keys(adapter) == ["sampleImageMsg"]


def test_dm_card_failure_falls_back_to_native_then_file(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, card_ok=False, native_ok=False)
    asyncio.run(adapter.send_image_file("dm-1", _image(tmp_path), metadata={"dingtalk_conversation_type": "1"}))
    adapter._send_robot_card_1_0_image.assert_awaited_once()
    assert _native_keys(adapter) == ["sampleImageMsg", "sampleFile"]


def test_document_url_is_sent_as_a_link_and_caption_precedes_a_file(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    asyncio.run(adapter.send_document("c1", "https://example.com/r/report.pdf?sig=1", caption="see"))
    assert adapter.send.await_args.kwargs["content"] == "see\n\n[report.pdf](https://example.com/r/report.pdf?sig=1)"
    adapter._upload_robot_media.assert_not_awaited()

    adapter = _adapter(monkeypatch)
    doc = tmp_path / "q3.xlsx"
    doc.write_bytes(b"x")
    asyncio.run(adapter.send_document("c1", str(doc), caption="numbers"))
    assert adapter.send.await_args.kwargs["content"] == "numbers"
    assert adapter._send_robot_native_message.await_args.kwargs["msg_param"]["fileType"] == "xlsx"
