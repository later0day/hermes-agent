"""DingTalk inbound media reaches vision / STT end to end.

Drives the REAL ``_on_message`` -> ``_resolve_media_codes`` -> ``_fetch_download_url`` -> cache ->
``_extract_media`` chain (only the DingTalk SDK call and the HTTP download are faked) and then the
gateway's real ``_classify_inbound_media``. Before this, the resolver only swapped the download code
for a signed remote URL and typed images as a bare ``"image"``: vision needs an ``image/*`` LOCAL file,
so every picture reached the model as "the user sent a document", and voice notes failed STT.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageType
from gateway.run_inbound import GatewayInboundMixin
from tests.gateway.test_dingtalk import _FakeChatbotMessage, _fake_dingtalk_optional_sdks, _make_gating_adapter  # noqa: F401

_CONTENT = {"img": ("image/png", b"\x89PNG\r\n\x1a\nfake"), "voice": ("audio/ogg", b"OggS-fake"),
            "doc": ("application/pdf", b"%PDF-1.4 fake")}


class _Response:
    def __init__(self, url):
        kind = url.rsplit("/", 1)[-1]
        self.headers = {"content-type": _CONTENT[kind][0]}
        self.content = _CONTENT[kind][1]

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _Response(url)


def _adapter(monkeypatch, download_urls):
    import plugins.platforms.dingtalk.adapter as dt

    monkeypatch.setattr(dt.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: True)
    adapter = _make_gating_adapter(monkeypatch, extra={"require_mention": False})
    adapter.handle_message = AsyncMock()
    adapter._get_access_token = AsyncMock(return_value="token")

    async def _download(request, headers, runtime):
        url = download_urls.get(request.download_code)
        return SimpleNamespace(body=SimpleNamespace(download_url=url) if url else None)

    adapter._robot_sdk = SimpleNamespace(robot_message_file_download_with_options_async=_download)
    return adapter


def _deliver(adapter, msg):
    asyncio.run(adapter._on_message(msg))
    return adapter.handle_message.await_args.args[0]


def _msg(**fields):
    msg = _FakeChatbotMessage.from_dict({"msgId": "m-1", "conversationId": "c-1", "conversationType": "1",
                                         "senderId": "s-1", "senderNick": "Alice", "text": ""})
    msg.image_content = None
    msg.extensions = {}
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def test_picture_is_cached_locally_and_routed_to_vision(monkeypatch):
    adapter = _adapter(monkeypatch, {"dl-img": "https://down.dingtalk.com/f/img"})
    event = _deliver(adapter, _msg(image_content={"downloadCode": "dl-img"}, message_type="picture"))

    assert event.message_type == MessageType.PHOTO
    assert event.media_types == ["image/png"] and Path(event.media_urls[0]).is_file()
    images, stt, _audio, _video = GatewayInboundMixin._classify_inbound_media(event, False)
    assert images == event.media_urls and stt == []


def test_rich_text_voice_note_goes_to_stt_as_a_local_file(monkeypatch):
    adapter = _adapter(monkeypatch, {"dl-voice": "https://down.dingtalk.com/f/voice"})
    msg = _msg(message_type="richText",
               rich_text_content=SimpleNamespace(rich_text_list=[{"type": "voice", "downloadCode": "dl-voice"}]))
    event = _deliver(adapter, msg)

    assert event.message_type == MessageType.VOICE and Path(event.media_urls[0]).is_file()
    _images, stt, _audio, _video = GatewayInboundMixin._classify_inbound_media(event, False)
    assert stt == event.media_urls


def test_file_message_is_cached_with_its_real_type(monkeypatch):
    adapter = _adapter(monkeypatch, {"dl-doc": "https://down.dingtalk.com/f/doc"})
    event = _deliver(adapter, _msg(message_type="file",
                                   extensions={"content": {"downloadCode": "dl-doc", "fileName": "q3.pdf"}}))

    assert event.message_type == MessageType.DOCUMENT
    assert event.media_types == ["application/pdf"] and Path(event.media_urls[0]).name.endswith("q3.pdf")


def test_a_failed_download_tells_the_agent_instead_of_passing_a_code(monkeypatch):
    adapter = _adapter(monkeypatch, {})  # the SDK answers with no download URL
    event = _deliver(adapter, _msg(image_content={"downloadCode": "dl-gone"}, message_type="picture"))

    assert event.media_urls == []
    assert "[Media attachment unavailable: DingTalk media download failed" in event.text
