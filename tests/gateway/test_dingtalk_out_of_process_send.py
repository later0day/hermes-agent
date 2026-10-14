"""DingTalk delivery that doesn't go through a cached inbound message.

- ``send_message`` to DingTalk used the text-only static-webhook sender even inside the gateway, so an
  agent could not send media or reach any chat without DINGTALK_WEBHOOK_URL (unset on this install):
  it now goes through the live adapter like other plugin platforms (cards, media, DM routing).
- The out-of-process sender (cron workers, dashboard, a live adapter whose SDK failed) required that
  webhook; 2026-10-14's lost cron deliveries ended in "DingTalk not configured". It now falls back to an
  OpenAPI group send with the app's own credentials.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gateway.config import Platform, PlatformConfig

CID = "cidGROUPxxxxxxxxxxxxxxxx=="


def _mock_openapi(monkeypatch, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.headers), json.loads(request.content or b"{}")))
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "tok-1", "expireIn": 7200})
        if request.url.path == "/v1.0/robot/groupMessages/send":
            return httpx.Response(200, json={"processQueryKey": "pqk-1"})
        return httpx.Response(404, json={"code": "notfound"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_out_of_process_send_uses_the_app_credentials_without_a_webhook(monkeypatch):
    from plugins.platforms.dingtalk.adapter import _standalone_send

    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    calls: list = []
    _mock_openapi(monkeypatch, calls)
    pconfig = PlatformConfig(enabled=True, extra={"client_id": "app-key", "client_secret": "app-secret"})

    result = asyncio.run(_standalone_send(pconfig, CID, "**daily report**"))

    assert result == {"success": True, "platform": "dingtalk", "chat_id": CID, "message_id": "pqk-1"}
    (token_path, _, token_body), (send_path, send_headers, send_body) = calls
    assert token_path == "/v1.0/oauth2/accessToken" and token_body == {"appKey": "app-key", "appSecret": "app-secret"}
    assert send_path == "/v1.0/robot/groupMessages/send"
    assert send_headers["x-acs-dingtalk-access-token"] == "tok-1"
    assert send_body["openConversationId"] == CID and send_body["robotCode"] == "app-key"
    assert json.loads(send_body["msgParam"])["text"] == "**daily report**"


def test_out_of_process_send_without_any_credentials_says_what_to_set(monkeypatch):
    from plugins.platforms.dingtalk.adapter import _standalone_send

    for var in ("DINGTALK_WEBHOOK_URL", "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    result = asyncio.run(_standalone_send(PlatformConfig(enabled=True, extra={}), CID, "x"))
    assert "DINGTALK_CLIENT_ID" in result["error"]


class _LiveDingTalk:
    def __init__(self):
        self.calls: list = []

    async def send(self, chat_id, content, metadata=None, **_kw):
        self.calls.append(("send", chat_id, content))
        return SimpleNamespace(success=True, message_id="m1", error=None)

    async def send_image_file(self, chat_id, image_path, caption=None, **_kw):
        self.calls.append(("send_image_file", chat_id, image_path))
        return SimpleNamespace(success=True, message_id="m2", error=None)


def test_send_message_to_dingtalk_goes_through_the_live_adapter_with_media(monkeypatch, tmp_path):
    import tools.send_message_tool as smt

    adapter = _LiveDingTalk()
    monkeypatch.setattr(smt, "_live_adapter", lambda platform, **_kw: (object(), adapter))

    async def _inline(_runner, make_coro, _msg):
        return await make_coro()
    monkeypatch.setattr(smt, "_dispatch_on_gateway_loop", _inline)
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    result = asyncio.run(smt._send_to_platform(
        Platform.DINGTALK, PlatformConfig(enabled=True, extra={}), CID, "see chart", media_files=[(str(image), False)]))

    assert not result.get("error"), result
    assert ("send_image_file", CID, str(image)) in adapter.calls  # native media, not "MEDIA omitted"
