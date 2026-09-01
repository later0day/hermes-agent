"""Tests for DingTalk platform adapter."""
import asyncio
import concurrent.futures
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


class _FakeDingTalkModel:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeChatbotMessage(SimpleNamespace):
    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(
            message_id=data.get("msgId") or data.get("messageId") or data.get("message_id") or "",
            conversation_id=data.get("conversationId") or data.get("conversation_id") or "",
            conversation_type=str(data.get("conversationType") or data.get("conversation_type") or "1"),
            sender_id=data.get("senderId") or data.get("sender_id") or "",
            sender_staff_id=data.get("senderStaffId") or data.get("sender_staff_id") or data.get("senderId") or "",
            sender_nick=data.get("senderNick") or data.get("sender_nick") or "",
            text=data.get("text") or "",
            rich_text=data.get("richText") or data.get("rich_text"),
            rich_text_content=data.get("richTextContent") or data.get("rich_text_content"),
            session_webhook=data.get("sessionWebhook") or data.get("session_webhook") or "",
            session_webhook_expired_time=data.get("sessionWebhookExpiredTime") or data.get("session_webhook_expired_time") or 0,
            create_at=data.get("createAt") or data.get("create_at") or 0,
            at_users=data.get("atUsers") or data.get("at_users") or [],
            is_in_at_list=bool(data.get("isInAtList") or data.get("is_in_at_list")),
        )


@pytest.fixture(autouse=True)
def _fake_dingtalk_optional_sdks(monkeypatch):
    """Keep DingTalk adapter tests hermetic when optional SDKs are absent."""
    import plugins.platforms.dingtalk.adapter as dt

    card_models = SimpleNamespace(**{
        name: _FakeDingTalkModel
        for name in (
            "CreateCardRequest",
            "CreateCardRequestCardData",
            "CreateCardRequestImGroupOpenSpaceModel",
            "CreateCardRequestImRobotOpenSpaceModel",
            "CreateCardHeaders",
            "DeliverCardRequest",
            "DeliverCardRequestImGroupOpenDeliverModel",
            "DeliverCardRequestImRobotOpenDeliverModel",
            "DeliverCardHeaders",
            "StreamingUpdateRequest",
            "StreamingUpdateHeaders",
        )
    })
    robot_models = SimpleNamespace(**{
        name: _FakeDingTalkModel
        for name in (
            "RobotReplyEmotionRequestTextEmotion",
            "RobotReplyEmotionRequest",
            "RobotReplyEmotionHeaders",
            "RobotRecallEmotionRequestTextEmotion",
            "RobotRecallEmotionRequest",
            "RobotRecallEmotionHeaders",
            "RobotMessageFileDownloadRequest",
            "RobotMessageFileDownloadHeaders",
            "BatchSendOTORequest",
            "BatchSendOTOHeaders",
            "BatchSendOTOResponse",
            "BatchSendOTOResponseBody",
            "PrivateChatSendRequest",
            "PrivateChatSendHeaders",
            "OrgGroupSendRequest",
            "OrgGroupSendHeaders",
        )
    })

    monkeypatch.setattr(dt, "ChatbotMessage", _FakeChatbotMessage, raising=False)
    monkeypatch.setattr(
        dt,
        "AckMessage",
        SimpleNamespace(STATUS_OK=200, STATUS_SYSTEM_EXCEPTION=500),
        raising=False,
    )
    monkeypatch.setattr(dt, "tea_util_models", SimpleNamespace(RuntimeOptions=_FakeDingTalkModel), raising=False)
    monkeypatch.setattr(dt, "dingtalk_card_models", card_models, raising=False)
    monkeypatch.setattr(dt, "dingtalk_robot_models", robot_models, raising=False)


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


class TestDingTalkRequirements:


    def test_returns_false_when_env_vars_missing(self, monkeypatch):
        monkeypatch.setattr(
            "plugins.platforms.dingtalk.adapter.DINGTALK_STREAM_AVAILABLE", True
        )
        monkeypatch.setattr("plugins.platforms.dingtalk.adapter.HTTPX_AVAILABLE", True)
        monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
        from plugins.platforms.dingtalk.adapter import check_dingtalk_requirements
        assert check_dingtalk_requirements() is False


class TestDingTalkHttpClient:
    def test_http_client_kwargs_default_uses_limits(self, monkeypatch):
        from plugins.platforms.dingtalk import adapter as dt

        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            "gateway.platforms._http_client_limits.platform_httpx_limits",
            lambda: "limits",
        )

        kwargs = dt._dingtalk_http_client_kwargs(timeout=30.0)

        assert kwargs == {"timeout": 30.0, "limits": "limits"}

    def test_http_client_kwargs_force_ipv4_uses_ipv4_transport(self, monkeypatch):
        from plugins.platforms.dingtalk import adapter as dt

        def fake_getaddrinfo(*args, **kwargs):
            return []

        fake_getaddrinfo._hermes_ipv4_patched = True
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(
            "gateway.platforms._http_client_limits.platform_httpx_limits",
            lambda: "limits",
        )

        transport_calls = []

        class FakeTransport:
            def __init__(self, **kwargs):
                transport_calls.append(kwargs)

        monkeypatch.setattr(dt.httpx, "AsyncHTTPTransport", FakeTransport)

        kwargs = dt._dingtalk_http_client_kwargs(timeout=30.0)

        assert kwargs["timeout"] == 30.0
        assert "limits" not in kwargs
        assert isinstance(kwargs["transport"], FakeTransport)
        assert transport_calls == [{"limits": "limits", "local_address": "0.0.0.0"}]


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestDingTalkAdapterInit:

    def test_reads_config_from_extra(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        config = PlatformConfig(
            enabled=True,
            extra={"client_id": "cfg-id", "client_secret": "cfg-secret"},
        )
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "cfg-id"
        assert adapter._client_secret == "cfg-secret"
        assert adapter.name == "Dingtalk"  # base class uses .title()

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env-secret")
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        config = PlatformConfig(enabled=True)
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "env-id"
        assert adapter._client_secret == "env-secret"

    def test_reads_robot_code_from_env(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("DINGTALK_ROBOT_CODE", "env-robot")
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        config = PlatformConfig(enabled=True)
        adapter = DingTalkAdapter(config)
        assert adapter._robot_code == "env-robot"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:

    def test_first_message_not_duplicate(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        assert adapter._dedup.is_duplicate("msg-1") is False


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:

    @pytest.mark.asyncio
    async def test_send_posts_to_webhook(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"session_webhook": "https://dingtalk.example/webhook"}
        )
        assert result.success is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://dingtalk.example/webhook"
        payload = call_args[1]["json"]
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["title"] == "Hermes"
        assert payload["markdown"]["text"] == "Hello!"


    @pytest.mark.asyncio
    async def test_send_no_webhook_falls_back_to_proactive_card(self):
        """Defect #2: with no valid session_webhook, send() must NOT drop
        the reply. When an AI Card template is configured it delivers via
        the webhook-independent AI Card path (the SDK-proven transport),
        synthesizing a group-targeted message when the inbound context was
        lost on restart."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import SendResult
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._card_template_id = "tpl-1"
        adapter._card_sdk = object()
        adapter._get_access_token = AsyncMock(return_value="tok")
        adapter._create_and_stream_card = AsyncMock(
            return_value=SendResult(success=True, message_id="card-1")
        )
        adapter._send_robot_native_message = AsyncMock()

        result = await adapter.send("cidABCDEF", "delayed reply")

        assert result.success is True
        assert result.message_id == "card-1"
        adapter._create_and_stream_card.assert_awaited_once()
        # Robot-native (OrgGroupSend) NOT used when the card path succeeds.
        adapter._send_robot_native_message.assert_not_called()
        # Synthesized message targets the conversation by chat_id.
        synth_msg = adapter._create_and_stream_card.await_args.args[1]
        assert getattr(synth_msg, "conversation_id") == "cidABCDEF"

    @pytest.mark.asyncio
    async def test_send_no_webhook_no_card_uses_robot_native(self):
        """With no webhook AND no AI Card template, send() falls back to the
        robot-native sampleMarkdown transport (last resort)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import SendResult
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        # No _card_template_id → card path skipped.
        adapter._send_robot_native_message = AsyncMock(
            return_value=SendResult(success=True, message_id="native-1")
        )

        result = await adapter.send("chat-xyz", "delayed reply")

        assert result.success is True
        assert result.message_id == "native-1"
        call = adapter._send_robot_native_message.await_args
        assert call.kwargs["msg_key"] == "sampleMarkdown"
        assert call.kwargs["msg_param"]["text"] == "delayed reply"

    @pytest.mark.asyncio
    async def test_send_webhook_4xx_falls_back_to_proactive(self):
        """A webhook that DingTalk rejects with 4xx (expired mid-flight,
        robot removed, etc.) is dead — send() retries via the proactive
        path rather than losing the reply."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import SendResult
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "expired session webhook"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client
        adapter._send_robot_native_message = AsyncMock(
            return_value=SendResult(success=True, message_id="proactive-2")
        )

        result = await adapter.send(
            "chat-xyz", "hi",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        assert result.message_id == "proactive-2"
        adapter._send_robot_native_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_webhook_5xx_does_not_fall_back(self):
        """A 5xx is a transient DingTalk server error — retrying the SAME
        dead webhook is pointless and the proactive path likely hits the
        same outage. send() returns the failure without falling back."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client
        adapter._send_robot_native_message = AsyncMock()

        result = await adapter.send(
            "chat-xyz", "hi",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is False
        assert "500" in (result.error or "")
        adapter._send_robot_native_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_proactive_fallback_failure_is_reported(self):
        """When the proactive fallback itself fails (e.g. no robot_code /
        token, and no card template), send() surfaces that failure rather
        than a false success."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import SendResult
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._send_robot_native_message = AsyncMock(
            return_value=SendResult(success=False, error="DingTalk robotCode is unavailable")
        )

        result = await adapter.send("chat-xyz", "reply")

        assert result.success is False
        assert "robotCode" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_image_renders_markdown_image(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send_image(
            "chat-123",
            "https://example.com/demo.png",
            caption="Screenshot",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["text"] == "Screenshot\n\n![image](https://example.com/demo.png)"

    @pytest.mark.asyncio
    async def test_send_image_file_missing_local_file_returns_not_found(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_image_file("chat-123", "/tmp/demo.png")

        assert result.success is False
        assert result.error and "Local file not found" in result.error

    @pytest.mark.asyncio
    async def test_send_image_file_returns_explicit_unsupported_error(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_image_file("chat-123", "/tmp/demo.png")

        assert result.success is False
        assert result.error and "Local file not found" in result.error

    @pytest.mark.asyncio
    async def test_send_document_missing_local_file_returns_not_found(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_document("chat-123", "/tmp/demo.pdf")

        assert result.success is False
        assert result.error and "Local file not found" in result.error

    @pytest.mark.asyncio
    async def test_send_document_returns_explicit_unsupported_error(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_document("chat-123", "/tmp/demo.pdf")

        assert result.success is False
        assert result.error and "Local file not found" in result.error

    @pytest.mark.asyncio
    async def test_send_uses_ai_card_without_session_webhook(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_id="chat-123",
            conversation_type="1",
            sender_staff_id="staff-123",
        )
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(),
            deliver_card_with_options_async=AsyncMock(),
            streaming_update_with_options_async=AsyncMock(),
        )

        result = await adapter.send(
            "chat-123",
            "### Markdown probe\n\n- **ok**",
            reply_to="msg-123",
        )

        assert result.success is True
        adapter._card_sdk.create_card_with_options_async.assert_awaited_once()
        adapter._card_sdk.deliver_card_with_options_async.assert_awaited_once()
        adapter._card_sdk.streaming_update_with_options_async.assert_awaited_once()
        stream_request = adapter._card_sdk.streaming_update_with_options_async.await_args.args[0]
        assert stream_request.is_finalize is True
        deliver_request = adapter._card_sdk.deliver_card_with_options_async.await_args.args[0]
        assert deliver_request.open_space_id == "dtv1.card//IM_ROBOT.staff-123"

    @pytest.mark.asyncio
    async def test_ai_card_send_without_reply_to_finalizes_unless_expect_edits(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._fire_done_reaction = MagicMock()
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_id="chat-123",
            conversation_type="1",
            sender_staff_id="staff-123",
        )
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(),
            deliver_card_with_options_async=AsyncMock(),
            streaming_update_with_options_async=AsyncMock(),
        )

        result = await adapter.send("chat-123", "Background notice")

        assert result.success is True
        stream_request = adapter._card_sdk.streaming_update_with_options_async.await_args.args[0]
        assert stream_request.is_finalize is True
        assert "chat-123" not in adapter._streaming_cards
        adapter._fire_done_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_ai_card_expect_edits_keeps_card_streaming_even_with_reply_to(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._fire_done_reaction = MagicMock()
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_id="chat-123",
            conversation_type="1",
            sender_staff_id="staff-123",
        )
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(),
            deliver_card_with_options_async=AsyncMock(),
            streaming_update_with_options_async=AsyncMock(),
        )

        result = await adapter.send(
            "chat-123",
            "Working...",
            reply_to="msg-123",
            metadata={"expect_edits": True},
        )

        assert result.success is True
        stream_request = adapter._card_sdk.streaming_update_with_options_async.await_args.args[0]
        assert stream_request.is_finalize is False
        assert adapter._streaming_cards["chat-123"][result.message_id] == "Working..."
        adapter._fire_done_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_ai_card_expect_edits_failure_sends_degraded_notice(self):
        """Editable AI Card path failure must still notify the user.

        Previous behavior was complete silence (success=False, no
        webhook call) to avoid an edit-storm against a webhook
        message_id. The current contract is: still return success=False
        so the turn-status coordinator disables itself, BUT send a
        one-shot plain-text notice via webhook so the user sees that
        live progress is unavailable rather than staring at silence.
        """
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_id="chat-123",
            conversation_type="1",
            sender_staff_id="staff-123",
        )
        adapter._session_webhooks["chat-123"] = ("https://dingtalk.example/webhook", 9999999999999)
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock(
            return_value=SimpleNamespace(status_code=200, text="{}"),
        )
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(side_effect=RuntimeError("blocked")),
            deliver_card_with_options_async=AsyncMock(),
            streaming_update_with_options_async=AsyncMock(),
        )

        result = await adapter.send(
            "chat-123",
            "Working...",
            metadata={"expect_edits": True},
        )

        # Outer send still reports failure so the coordinator disables
        # itself and no edits target a webhook id.
        assert result.success is False
        assert "webhook fallback cannot be edited" in result.error
        assert "chat-123" not in adapter._streaming_cards

        # But the user got a one-shot degraded notice via webhook.
        adapter._http_client.post.assert_awaited_once()
        sent_payload = adapter._http_client.post.await_args.kwargs["json"]
        assert sent_payload["msgtype"] == "markdown"
        assert adapter._DEGRADED_PROGRESS_NOTICE in sent_payload["markdown"]["text"]
        # Original "Working..." content (which the agent would have
        # edited) MUST NOT be sent as the notice body — that's what
        # caused the edit-storm in the first place.
        assert "Working..." not in sent_payload["markdown"]["text"]

    @pytest.mark.asyncio
    async def test_ai_card_expect_edits_failure_without_webhook_stays_silent(self):
        """Degraded notice is best-effort; missing webhook means silence.

        We never want a webhook-lookup failure to mask the original AI
        Card failure or to raise back to the caller.
        """
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._message_contexts["chat-456"] = SimpleNamespace(
            conversation_id="chat-456",
            conversation_type="1",
            sender_staff_id="staff-456",
        )
        # No cached session_webhook for this chat — the degraded notice
        # path must skip cleanly without HTTP calls.
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock()
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(side_effect=RuntimeError("blocked")),
            deliver_card_with_options_async=AsyncMock(),
            streaming_update_with_options_async=AsyncMock(),
        )

        result = await adapter.send(
            "chat-456",
            "Working...",
            metadata={"expect_edits": True},
        )

        assert result.success is False
        assert "webhook fallback cannot be edited" in result.error
        adapter._http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_image_file_sends_card_1_0_with_uploaded_media(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import SendResult

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "cfg-robot"})
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._upload_robot_media = AsyncMock(
            return_value=SendResult(success=True, message_id="@media-123")
        )
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_type="1",
            sender_staff_id="staff-123",
            robot_code="callback-robot",
        )
        adapter._robot_sdk = SimpleNamespace(
            private_chat_send_with_options_async=AsyncMock(),
            batch_send_otowith_options_async=AsyncMock(),
        )
        adapter._card_sdk = SimpleNamespace(
            create_card_with_options_async=AsyncMock(),
            deliver_card_with_options_async=AsyncMock(),
        )

        result = await adapter.send_image_file(
            "chat-123",
            "/tmp/demo.png",
            caption="Screenshot",
        )

        assert result.success is True
        adapter._robot_sdk.private_chat_send_with_options_async.assert_not_called()
        adapter._robot_sdk.batch_send_otowith_options_async.assert_not_called()
        create_request = adapter._card_sdk.create_card_with_options_async.await_args.args[0]
        assert create_request.card_template_id
        param_map = create_request.card_data.card_param_map
        assert param_map["msgContent"] == "Screenshot"
        assert json.loads(param_map["sys_full_json_obj"])["msgImages"] == ["@media-123"]
        deliver_request = adapter._card_sdk.deliver_card_with_options_async.await_args.args[0]
        assert deliver_request.open_space_id == "dtv1.card//IM_ROBOT.staff-123"

    @pytest.mark.asyncio
    async def test_native_dm_send_defaults_to_batch_oto_with_config_robot_code(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(
                enabled=True,
                extra={"robot_code": "cfg-robot", "app_code": "cool-app"},
            )
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_type="1",
            sender_staff_id="staff-123",
            chatbot_user_id="chatbot-user-123",
            robot_code="callback-robot",
        )
        adapter._robot_sdk = SimpleNamespace(
            private_chat_send_with_options_async=AsyncMock(
                return_value=SimpleNamespace(
                    body=SimpleNamespace(process_query_key="process-123")
                )
            ),
            batch_send_otowith_options_async=AsyncMock(
                return_value=SimpleNamespace(
                    body=SimpleNamespace(process_query_key="batch-process")
                )
            )
        )

        result = await adapter._send_robot_native_message(
            chat_id="chat-123",
            msg_key="sampleImageMsg",
            msg_param={"photoURL": "media-123"},
        )

        assert result.success is True
        adapter._robot_sdk.private_chat_send_with_options_async.assert_not_called()
        request = adapter._robot_sdk.batch_send_otowith_options_async.await_args.args[0]
        assert request.robot_code == "cfg-robot"
        assert request.user_ids == ["staff-123"]

    @pytest.mark.asyncio
    async def test_native_dm_send_uses_private_chat_route_when_requested(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(
                enabled=True,
                extra={"robot_code": "cfg-robot", "app_code": "cool-app"},
            )
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._message_contexts["chat-123"] = SimpleNamespace(
            conversation_type="1",
            sender_staff_id="staff-123",
            robot_code="callback-robot",
        )
        adapter._robot_sdk = SimpleNamespace(
            private_chat_send_with_options_async=AsyncMock(
                return_value=SimpleNamespace(
                    body=SimpleNamespace(process_query_key="private-process")
                )
            ),
            batch_send_otowith_options_async=AsyncMock(
                return_value=SimpleNamespace(
                    body=SimpleNamespace(process_query_key="batch-process")
                )
            ),
        )

        result = await adapter._send_robot_native_message(
            chat_id="chat-123",
            msg_key="sampleImageMsg",
            msg_param={"photoURL": "media-123"},
            metadata={"dingtalk_send_route": "private_chat_send"},
        )

        assert result.success is True
        adapter._robot_sdk.batch_send_otowith_options_async.assert_not_called()
        request = adapter._robot_sdk.private_chat_send_with_options_async.await_args.args[0]
        assert request.cool_app_code == "cool-app"
        assert request.robot_code == "cfg-robot"
        assert request.open_conversation_id == "chat-123"


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:


    @pytest.mark.asyncio
    async def test_connect_fails_without_sdk(self, monkeypatch):
        monkeypatch.setattr(
            "plugins.platforms.dingtalk.adapter.DINGTALK_STREAM_AVAILABLE", False
        )
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        result = await adapter.connect()
        assert result is False


    @pytest.mark.asyncio
    async def test_disconnect_finalizes_open_streaming_cards(self):
        """Streaming cards must be finalized before HTTP client closes."""
        from unittest.mock import AsyncMock, patch
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._http_client = AsyncMock()
        adapter._stream_task = None
        adapter._streaming_cards = {
            "chat-1": {"track-a": "last content"},
            "chat-2": {"track-b": "other"},
        }

        close_calls = []

        async def fake_close_siblings(chat_id):
            # HTTP client must still be alive at call time.
            assert adapter._http_client is not None, (
                "HTTP client was already closed before card finalization"
            )
            close_calls.append(chat_id)
            adapter._streaming_cards.pop(chat_id, None)

        with patch.object(adapter, "_close_streaming_siblings", side_effect=fake_close_siblings):
            await adapter.disconnect()

        assert set(close_calls) == {"chat-1", "chat-2"}
        assert adapter._streaming_cards == {}
        assert adapter._http_client is None


# ---------------------------------------------------------------------------
# Platform enum
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SDK compatibility regression tests (dingtalk-stream >= 0.20 / 0.24)
# ---------------------------------------------------------------------------


class TestWebhookDomainAllowlist:
    """Guard the webhook origin allowlist against regression.

    The SDK started returning reply webhooks on ``oapi.dingtalk.com`` in
    addition to ``api.dingtalk.com``. Both must be accepted, and hostile
    lookalikes must still be rejected (SSRF defence-in-depth).
    """

    def test_api_domain_accepted(self):
        from plugins.platforms.dingtalk.adapter import _DINGTALK_WEBHOOK_RE
        assert _DINGTALK_WEBHOOK_RE.match(
            "https://api.dingtalk.com/robot/send?access_token=x"
        )

    def test_oapi_domain_accepted(self):
        from plugins.platforms.dingtalk.adapter import _DINGTALK_WEBHOOK_RE
        assert _DINGTALK_WEBHOOK_RE.match(
            "https://oapi.dingtalk.com/robot/send?access_token=x"
        )


class TestHandlerProcessIsAsync:
    """dingtalk-stream >= 0.20 requires ``process`` to be a coroutine."""

    def test_process_is_coroutine_function(self):
        from plugins.platforms.dingtalk.adapter import _IncomingHandler
        assert asyncio.iscoroutinefunction(_IncomingHandler.process)


class TestExtractText:
    """_extract_text must handle both legacy and current SDK payload shapes.

    Before SDK 0.20 ``message.text`` was a ``dict`` with a ``content`` key.
    From 0.20 onward it is a ``TextContent`` dataclass whose ``__str__``
    returns ``"TextContent(content=...)"`` — falling back to ``str(text)``
    leaks that repr into the agent's input.
    """


    def test_text_as_textcontent_object(self):
        """SDK >= 0.20 shape: object with ``.content`` attribute."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        class FakeTextContent:
            content = "hello from new sdk"

            def __str__(self):  # mimic real SDK repr
                return f"TextContent(content={self.content})"

        msg = MagicMock()
        msg.text = FakeTextContent()
        msg.rich_text_content = None
        msg.rich_text = None
        result = DingTalkAdapter._extract_text(msg)
        assert result == "hello from new sdk"
        assert "TextContent(" not in result


    def test_rich_text_content_new_shape(self):
        """SDK >= 0.20 exposes rich text as ``message.rich_text_content.rich_text_list``."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        class FakeRichText:
            rich_text_list = [{"text": "hello "}, {"text": "world"}]

        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = FakeRichText()
        msg.rich_text = None
        result = DingTalkAdapter._extract_text(msg)
        assert "hello" in result and "world" in result


    def test_empty_message(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = None
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == ""

    # --- Card / interactiveCard message handling (文档分享卡片) ---

    def test_card_with_dict_content_and_url(self):
        """card msgtype with extensions.card.content as dict with url key."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text = None
        msg.message_type = "card"
        msg.extensions = {
            "card": {
                "title": "Q3经营分析报告",
                "content": {"url": "https://dingtalk.com/doc/abc123"},
            }
        }
        assert DingTalkAdapter._extract_text(msg) == "[文档] Q3经营分析报告 https://dingtalk.com/doc/abc123"

    def test_card_with_dict_content_docurl(self):
        """card msgtype with extensions.card.content as dict with docUrl key."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text = None
        msg.message_type = "card"
        msg.extensions = {
            "card": {
                "title": "周报模板",
                "content": {"docUrl": "https://docs.dingtalk.com/xyz"},
            }
        }
        assert DingTalkAdapter._extract_text(msg) == "[文档] 周报模板 https://docs.dingtalk.com/xyz"


    def test_interactive_card_with_title_and_url(self):
        """interactiveCard msgtype with both title and biz_custom_action_url."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text = None
        msg.message_type = "interactiveCard"
        msg.extensions = {
            "content": {
                "title": "项目看板",
                "biz_custom_action_url": "https://dingtalk.com/doc/kanban",
            }
        }
        assert DingTalkAdapter._extract_text(msg) == "[文档卡片] 项目看板 https://dingtalk.com/doc/kanban"


class TestExtractMedia:
    """_extract_media must split native voice rich-text items (auto-STT)
    from generic audio file uploads (kept as attachments, no STT)."""

    def _msg_with_rich_text(self, items):
        msg = MagicMock()
        msg.text = None
        msg.image_content = None
        msg.rich_text_content = None
        msg.rich_text = items
        return msg

    def test_voice_rich_text_item_classified_as_voice(self):
        """Native DingTalk voice notes (type=voice) must enter the auto-STT
        path via MessageType.VOICE — the gateway skips STT for AUDIO."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "voice", "downloadCode": "dl_voice_abc"}]
        )
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.VOICE
        assert urls == ["dl_voice_abc"]
        assert mtypes == ["audio/ogg"]

    def test_richtext_reset_does_not_clobber_voice(self):
        """A richText envelope containing a native voice item must stay
        VOICE — the ``msg_type_str == "richText"`` re-derivation used to
        reset it to TEXT, dropping the voice note from the STT path
        (#38211, #38219)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "voice", "downloadCode": "dl_voice_rt"}]
        )
        msg.message_type = "richText"
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.VOICE
        assert urls == ["dl_voice_rt"]
        assert mtypes == ["audio/ogg"]

    def test_richtext_with_image_still_photo(self):
        """richText with only an embedded image keeps the PHOTO promotion."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "picture", "downloadCode": "dl_img_rt"}]
        )
        msg.message_type = "richText"
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.PHOTO
        assert urls == ["dl_img_rt"]

    def test_audio_rich_text_item_stays_audio(self):
        """Generic audio uploads (e.g. an mp3 the user attached) must NOT
        be auto-transcribed — they stay MessageType.AUDIO."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter, DINGTALK_TYPE_MAPPING
        from gateway.platforms.base import MessageType

        # Simulate a future/non-voice audio rich-text item by extending the
        # mapping so item_type != "voice" but still routes through the
        # ``mapped == "audio"`` branch.
        DINGTALK_TYPE_MAPPING["audio"] = "audio"
        try:
            msg = self._msg_with_rich_text(
                [{"type": "audio", "downloadCode": "dl_audio_xyz"}]
            )
            msg_type, urls, mtypes = DingTalkAdapter._extract_media(
                DingTalkAdapter, msg
            )
            assert msg_type == MessageType.AUDIO
            assert urls == ["dl_audio_xyz"]
            assert mtypes == ["audio/ogg"]
        finally:
            del DINGTALK_TYPE_MAPPING["audio"]

    @pytest.mark.asyncio
    async def test_on_message_preserves_media_errors(self, monkeypatch):
        from gateway.platforms.base import MessageType

        adapter = _make_gating_adapter(monkeypatch, extra={"require_mention": False})
        adapter.handle_message = AsyncMock()

        async def _fail_media_resolution(message):
            adapter._set_media_error(
                message.image_content,
                "DingTalk media download failed: robot SDK is unavailable.",
            )

        adapter._resolve_media_codes = AsyncMock(side_effect=_fail_media_resolution)

        msg = _FakeChatbotMessage.from_dict({
            "msgId": "msg-media-error",
            "conversationId": "conv-1",
            "conversationType": "1",
            "senderId": "sender-1",
            "senderNick": "Alice",
            "text": "",
        })
        msg.image_content = {"downloadCode": "dl_image_abc"}
        msg.message_type = "picture"

        await adapter._on_message(msg)

        event = adapter.handle_message.await_args.args[0]
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == []
        assert event.media_errors == [
            "DingTalk media download failed: robot SDK is unavailable."
        ]
    def test_file_extensions_content_downloadcode_resolved(self):
        """msgtype='file' with extensions.content.downloadCode → DOCUMENT."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = MagicMock()
        msg.text = None
        msg.image_content = None
        msg.rich_text_content = None
        msg.rich_text = None
        msg.message_type = "file"
        msg.extensions = {"content": {"downloadCode": "dl_file_123", "fileName": "report.pdf"}}
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.DOCUMENT
        assert urls == ["dl_file_123"]
        assert mtypes == ["application/pdf"]

    def test_image_extensions_content_classified_as_photo(self):
        """msgtype='image' with extensions.content → PHOTO (not DOCUMENT)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = MagicMock()
        msg.text = None
        msg.image_content = None
        msg.rich_text_content = None
        msg.rich_text = None
        msg.message_type = "image"
        msg.extensions = {"content": {"downloadCode": "dl_img_abc", "fileName": "photo.png"}}
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.PHOTO
        assert urls == ["dl_img_abc"]
        assert mtypes == ["image/png"]

    def test_image_no_filename_still_photo(self):
        """msgtype='image' without fileName → still PHOTO (MIME heuristic)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = MagicMock()
        msg.text = None
        msg.image_content = None
        msg.rich_text_content = None
        msg.rich_text = None
        msg.message_type = "image"
        msg.extensions = {"content": {"downloadCode": "dl_img_noext"}}
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.PHOTO
        assert urls == ["dl_img_noext"]
        # Without fileName, mime defaults to octet-stream but msg_type_str=="image" still wins
        assert mtypes == ["application/octet-stream"]


# ---------------------------------------------------------------------------
# Group gating — require_mention + allowed_users (parity with other platforms)
# ---------------------------------------------------------------------------


def _make_gating_adapter(monkeypatch, *, extra=None, env=None):
    """Build a DingTalkAdapter with only the gating fields populated.

    Clears every DINGTALK_* gating env var before applying the caller's
    overrides so individual tests stay isolated.
    """
    for key in (
        "DINGTALK_REQUIRE_MENTION",
        "DINGTALK_MENTION_PATTERNS",
        "DINGTALK_FREE_RESPONSE_CHATS",
        "DINGTALK_ALLOWED_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter
    return DingTalkAdapter(PlatformConfig(enabled=True, extra=extra or {}))


class TestAllowedUsersGate:

    def test_empty_allowlist_allows_everyone(self, monkeypatch):
        adapter = _make_gating_adapter(monkeypatch)
        assert adapter._is_user_allowed("anyone", "any-staff") is True


    def test_matches_sender_id_case_insensitive(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"allowed_users": ["SenderABC"]}
        )
        assert adapter._is_user_allowed("senderabc", "") is True


class TestMentionPatterns:


    def test_pattern_matches_text(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"mention_patterns": ["^hermes"]}
        )
        assert adapter._message_matches_mention_patterns("hermes please help") is True
        assert adapter._message_matches_mention_patterns("please hermes help") is False


    def test_env_var_json_populates_patterns(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            env={"DINGTALK_MENTION_PATTERNS": '["^bot", "^assistant"]'},
        )
        assert len(adapter._mention_patterns) == 2
        assert adapter._message_matches_mention_patterns("bot ping") is True


class TestShouldProcessMessage:

    def test_dm_always_accepted(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"require_mention": True}
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hi", is_group=False, chat_id="dm1") is True


    def test_group_accepted_when_chat_in_free_response_list(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            extra={"require_mention": True, "free_response_chats": ["grp1"]},
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hi", is_group=True, chat_id="grp1") is True
        # Different group still blocked
        assert adapter._should_process_message(msg, "hi", is_group=True, chat_id="grp2") is False


# ---------------------------------------------------------------------------
# _IncomingHandler.process — session_webhook extraction & fire-and-forget
# ---------------------------------------------------------------------------


class TestIncomingHandlerProcess:
    """Verify that _IncomingHandler.process correctly converts callback data
    and dispatches message processing as a background task (fire-and-forget)
    so the SDK ACK is returned immediately."""


    @pytest.mark.asyncio
    async def test_process_preserves_robot_code_and_chatbot_user_id_separately(self):
        """robotCode is the OpenAPI robot identifier; chatbotUserId is the
        robot user's DingTalk account. They must not be collapsed."""
        from plugins.platforms.dingtalk.adapter import _IncomingHandler, DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        callback.data = {
            "msgtype": "text",
            "text": {"content": "hi"},
            "senderId": "user2",
            "conversationId": "conv2",
            "robotCode": "robot-code-1",
            "chatbotUserId": "chatbot-user-1",
            "msgId": "msg-robot",
        }

        await handler.process(callback)
        await asyncio.sleep(0.05)

        chatbot_msg = adapter._on_message.call_args[0][0]
        assert chatbot_msg.robot_code == "robot-code-1"
        assert chatbot_msg.chatbot_user_id == "chatbot-user-1"

    @pytest.mark.asyncio
    async def test_process_does_not_use_chatbot_user_id_as_robot_code(self):
        """Older/newer SDK mappings may miss robotCode, but chatbotUserId is
        still not a valid robotCode fallback."""
        from plugins.platforms.dingtalk.adapter import _IncomingHandler, DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        callback.data = {
            "msgtype": "text",
            "text": {"content": "hi"},
            "senderId": "user2",
            "conversationId": "conv2",
            "chatbotUserId": "chatbot-user-1",
            "msgId": "msg-chatbot-user",
        }

        await handler.process(callback)
        await asyncio.sleep(0.05)

        chatbot_msg = adapter._on_message.call_args[0][0]
        assert getattr(chatbot_msg, "robot_code", None) in (None, "")
        assert chatbot_msg.chatbot_user_id == "chatbot-user-1"

    @pytest.mark.asyncio
    async def test_process_returns_ack_immediately(self):
        """process() must not block on _on_message — it should return
        the ACK tuple before the message is fully processed."""
        from plugins.platforms.dingtalk.adapter import _IncomingHandler, DingTalkAdapter

        processing_started = asyncio.Event()
        processing_gate = asyncio.Event()

        async def slow_on_message(msg):
            processing_started.set()
            await processing_gate.wait()  # Block until we release

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = slow_on_message
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        callback.data = {
            "msgtype": "text",
            "text": {"content": "test"},
            "senderId": "u",
            "conversationId": "c",
            "sessionWebhook": "https://oapi.dingtalk.com/x",
            "msgId": "m",
        }

        # process() should return immediately even though _on_message blocks
        result = await handler.process(callback)
        assert result[0] == 200

        # Clean up: release the gate so the background task finishes
        processing_gate.set()
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Text extraction — mention preservation + platform sanity
# ---------------------------------------------------------------------------

class TestExtractTextMentions:

    def test_preserves_at_mentions_in_text(self):
        """@mentions are routing signals (via isInAtList), not text to strip.

        Stripping all @handles collateral-damages emails, SSH URLs, and
        literal references the user wrote.
        """
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        cases = [
            ("@bot hello", "@bot hello"),
            ("contact alice@example.com", "contact alice@example.com"),
            ("git@github.com:foo/bar.git", "git@github.com:foo/bar.git"),
            ("what does @openai think", "what does @openai think"),
            ("@机器人 转发给 @老王", "@机器人 转发给 @老王"),
        ]
        for text, expected in cases:
            msg = MagicMock()
            msg.text = text
            msg.rich_text = None
            msg.rich_text_content = None
            assert DingTalkAdapter._extract_text(msg) == expected, (
                f"mangled: {text!r} -> {DingTalkAdapter._extract_text(msg)!r}"
            )


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Concurrency — chat-scoped message context
# ---------------------------------------------------------------------------


class TestMessageContextIsolation:

    def test_contexts_keyed_by_chat_id(self):
        """Two concurrent chats must not clobber each other's context."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        msg_a = MagicMock(conversation_id="chat-A", sender_staff_id="user-A")
        msg_b = MagicMock(conversation_id="chat-B", sender_staff_id="user-B")
        adapter._message_contexts["chat-A"] = msg_a
        adapter._message_contexts["chat-B"] = msg_b

        assert adapter._message_contexts["chat-A"] is msg_a
        assert adapter._message_contexts["chat-B"] is msg_b


# ---------------------------------------------------------------------------
# Card lifecycle: editable cards use metadata["expect_edits"]
# ---------------------------------------------------------------------------


class TestCardLifecycle:

    @pytest.fixture
    def adapter_with_card(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        a = DingTalkAdapter(PlatformConfig(
            enabled=True,
            extra={"card_template_id": "tmpl-1"},
        ))
        a._card_sdk = MagicMock()
        a._card_sdk.create_card_with_options_async = AsyncMock()
        a._card_sdk.deliver_card_with_options_async = AsyncMock()
        a._card_sdk.streaming_update_with_options_async = AsyncMock()
        a._http_client = AsyncMock()
        a._get_access_token = AsyncMock(return_value="token")
        # Minimal message context
        msg = MagicMock(
            conversation_id="chat-1",
            conversation_type="1",
            sender_staff_id="staff-1",
            message_id="user-msg-1",
        )
        a._message_contexts["chat-1"] = msg
        a._session_webhooks["chat-1"] = (
            "https://api.dingtalk.com/x", 9999999999999,
        )
        return a

    @pytest.mark.asyncio
    async def test_final_reply_finalizes_card(self, adapter_with_card):
        """send(reply_to=...) creates a closed card (final response path)."""
        a = adapter_with_card
        result = await a.send("chat-1", "Hello", reply_to="user-msg-1")
        assert result.success
        call = a._card_sdk.streaming_update_with_options_async.call_args
        assert call[0][0].is_finalize is True
        # Not tracked as streaming — it's already closed.
        assert "chat-1" not in a._streaming_cards

    @pytest.mark.asyncio
    async def test_expect_edits_send_stays_streaming(self, adapter_with_card):
        """metadata.expect_edits creates an OPEN card for editable status /
        commentary / streaming first chunk.  No flicker closed→streaming when
        edit_message follows."""
        a = adapter_with_card
        result = await a.send(
            "chat-1",
            "💻 terminal: ls",
            metadata={"expect_edits": True},
        )
        assert result.success
        call = a._card_sdk.streaming_update_with_options_async.call_args
        assert call[0][0].is_finalize is False
        # Tracked for sibling cleanup.
        assert result.message_id in a._streaming_cards.get("chat-1", {})

    @pytest.mark.asyncio
    async def test_done_fires_only_when_reply_to_is_set(self, adapter_with_card):
        """reply_to distinguishes final response (base.py) from tool-progress
        sends (run.py).  Done must only fire for the former."""
        a = adapter_with_card
        fired: list[str] = []
        a._fire_done_reaction = lambda cid: fired.append(cid)

        # Non-final notice path: no reply_to — no Done.
        await a.send("chat-1", "tool line")
        assert fired == []

        # Final response path: reply_to set — Done fires.
        await a.send("chat-1", "final", reply_to="user-msg-1")
        assert fired == ["chat-1"]

    @pytest.mark.asyncio
    async def test_edit_message_finalize_fires_done(self, adapter_with_card):
        """Stream consumer's final edit_message(finalize=True) fires Done."""
        a = adapter_with_card
        fired: list[str] = []
        a._fire_done_reaction = lambda cid: fired.append(cid)

        await a.send("chat-1", "initial")
        # Reopen via edit_message(finalize=False) then close.
        await a.edit_message(
            chat_id="chat-1", message_id="track-X",
            content="streaming...", finalize=False,
        )
        await a.edit_message(
            chat_id="chat-1", message_id="track-X",
            content="final", finalize=True,
        )
        assert "chat-1" in fired

    @pytest.mark.asyncio
    async def test_edit_message_finalize_false_tracks_sibling(self, adapter_with_card):
        """After edit_message(finalize=False), card is tracked as open."""
        a = adapter_with_card
        await a.edit_message(
            chat_id="chat-1", message_id="track-1",
            content="partial", finalize=False,
        )
        assert "chat-1" in a._streaming_cards
        assert a._streaming_cards["chat-1"].get("track-1") == "partial"

    @pytest.mark.asyncio
    async def test_next_send_auto_closes_sibling_streaming_cards(
        self, adapter_with_card,
    ):
        """Tool-progress card left open (expect_edits + edits) must
        be auto-closed when the final-reply send arrives."""
        a = adapter_with_card
        # First tool: editable status send — card stays open.
        r1 = await a.send("chat-1", "💻 tool1", metadata={"expect_edits": True})
        # Second tool: edit_message(finalize=False) — keeps streaming.
        await a.edit_message(
            chat_id="chat-1", message_id=r1.message_id,
            content="💻 tool1\n💻 tool2", finalize=False,
        )
        assert r1.message_id in a._streaming_cards.get("chat-1", {})
        a._card_sdk.streaming_update_with_options_async.reset_mock()

        # Final response send auto-closes the sibling.
        await a.send("chat-1", "final answer", reply_to="user-msg")

        calls = a._card_sdk.streaming_update_with_options_async.call_args_list
        assert len(calls) >= 2
        # First call was the sibling close with last-seen tool-progress content.
        first_req = calls[0][0][0]
        assert first_req.out_track_id == r1.message_id
        assert first_req.is_finalize is True
        assert "tool1" in first_req.content
        # Streaming tracking is cleared after close.
        assert "chat-1" not in a._streaming_cards

    @pytest.mark.asyncio
    async def test_expect_edits_send_then_edit_never_reopens_card(
        self, adapter_with_card,
    ):
        """An editable card must stay open across create → edit → finalize.

        Guards the upstream property that ``test_intermediate_send_stays
        _streaming`` protected: no closed→streaming flicker for a card that
        gets edited later.  The fork moved the signal from ``reply_to`` to
        ``metadata["expect_edits"]``, so every producer that edits its own
        message must declare it (see the gateway heartbeat in
        ``test_run_heartbeat_expect_edits.py``).
        """
        a = adapter_with_card
        r = await a.send(
            "chat-1", "⏳ Working — 3 min", metadata={"expect_edits": True},
        )
        assert r.success
        create_req = a._card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert create_req.is_finalize is False
        assert r.message_id in a._streaming_cards.get("chat-1", {})

        # Interval 2 edits in place — still open, so no reopen happened.
        await a.edit_message("chat-1", r.message_id, "⏳ Working — 6 min")
        edit_req = a._card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert edit_req.is_finalize is False
        assert r.message_id in a._streaming_cards.get("chat-1", {})

    @pytest.mark.asyncio
    async def test_edit_message_requires_message_id(self, adapter_with_card):
        a = adapter_with_card
        result = await a.edit_message(
            chat_id="chat-1", message_id="", content="x", finalize=True,
        )
        assert result.success is False
        a._card_sdk.streaming_update_with_options_async.assert_not_called()

    def test_fire_done_reaction_is_idempotent(self, adapter_with_card):
        a = adapter_with_card
        captured = []
        def _capture(coro):
            captured.append(coro)
        a._spawn_bg = _capture

        a._fire_done_reaction("chat-1")
        a._fire_done_reaction("chat-1")
        assert len(captured) == 1
        captured[0].close()

    def test_fire_done_reaction_uses_done_label_by_default(self, adapter_with_card):
        """No reply state set → use the success completion label.

        Backwards-compatible with adapters / paths that don't wire the
        new hook: previous behaviour was to always fire Done.
        """
        a = adapter_with_card
        emotion_calls: list[tuple[str, bool]] = []

        async def _capture_emotion(msg_id, conv_id, label, *, recall=False):
            emotion_calls.append((label, recall))

        a._send_emotion = _capture_emotion
        loop = asyncio.new_event_loop()
        try:
            a._spawn_bg = lambda coro: loop.run_until_complete(coro)
            a._fire_done_reaction("chat-1")
        finally:
            loop.close()

        assert emotion_calls == [
            (a.REACTION_THINKING, True),
            (a.REACTION_DONE, False),
        ]

    def test_fire_done_reaction_picks_error_label_when_state_is_error(
        self, adapter_with_card,
    ):
        a = adapter_with_card
        emotion_calls: list[tuple[str, bool]] = []

        async def _capture_emotion(msg_id, conv_id, label, *, recall=False):
            emotion_calls.append((label, recall))

        a._send_emotion = _capture_emotion
        a.set_pending_reply_state("chat-1", "error")

        loop = asyncio.new_event_loop()
        try:
            a._spawn_bg = lambda coro: loop.run_until_complete(coro)
            a._fire_done_reaction("chat-1")
        finally:
            loop.close()

        assert emotion_calls == [
            (a.REACTION_THINKING, True),
            (a.REACTION_ERROR, False),
        ]
        # State is consumed on use, not sticky across turns.
        assert "chat-1" not in a._pending_reply_state

    def test_fire_done_reaction_picks_interrupted_label_when_state_is_interrupted(
        self, adapter_with_card,
    ):
        a = adapter_with_card
        emotion_calls: list[tuple[str, bool]] = []

        async def _capture_emotion(msg_id, conv_id, label, *, recall=False):
            emotion_calls.append((label, recall))

        a._send_emotion = _capture_emotion
        a.set_pending_reply_state("chat-1", "interrupted")

        loop = asyncio.new_event_loop()
        try:
            a._spawn_bg = lambda coro: loop.run_until_complete(coro)
            a._fire_done_reaction("chat-1")
        finally:
            loop.close()

        assert emotion_calls == [
            (a.REACTION_THINKING, True),
            (a.REACTION_INTERRUPTED, False),
        ]

    def test_set_pending_reply_state_rejects_unknown_states(self, adapter_with_card):
        """Unknown states fall back to success — never accidentally
        suppress the completion reaction with an unrecognized label.
        """
        a = adapter_with_card
        a.set_pending_reply_state("chat-1", "garbage")
        assert a._pending_reply_state["chat-1"] == "success"
        # Empty chat_id is a no-op.
        a.set_pending_reply_state("", "error")
        assert "" not in a._pending_reply_state

    # ------------------------------------------------------------------
    # Stage-aware reaction lifecycle (notify_tool_started)
    # ------------------------------------------------------------------

    @staticmethod
    def _capture_stage_emotions(a):
        """Wire up the adapter so all _send_emotion calls are captured
        and _spawn_bg runs the coroutine synchronously on a private
        loop. Returns the list that emotion calls append to."""
        emotion_calls: list[tuple[str, bool]] = []

        async def _capture_emotion(msg_id, conv_id, label, *, recall=False):
            emotion_calls.append((label, recall))

        a._send_emotion = _capture_emotion
        loop = asyncio.new_event_loop()
        a._spawn_bg = lambda coro: loop.run_until_complete(coro)
        a._test_stage_loop = loop  # so the caller can close it
        return emotion_calls

    def test_notify_tool_started_swaps_thinking_for_stage_label(
        self, adapter_with_card,
    ):
        """First terminal call in a turn swaps the Thinking reaction
        for the terminal stage label."""
        a = adapter_with_card
        terminal_label = a._TOOL_STAGE_LABELS["terminal"]
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal")
        finally:
            a._test_stage_loop.close()

        assert calls == [
            (a.REACTION_THINKING, True),
            (terminal_label, False),
        ]
        # State now reflects the new label so future swaps know what
        # to recall.
        assert a._current_stage_label["chat-1"] == terminal_label

    def test_notify_tool_started_schedules_on_adapter_loop_from_worker_thread(
        self, adapter_with_card, monkeypatch,
    ):
        """Agent tool callbacks run off the gateway loop; stage swaps must hop
        back to the adapter loop instead of calling create_task in that worker
        thread."""
        a = adapter_with_card

        class FakeLoop:
            def is_running(self):
                return True

        fake_loop = FakeLoop()
        a._loop = fake_loop
        scheduled = []

        def fake_run_coroutine_threadsafe(coro, loop):
            scheduled.append((coro, loop))
            coro.close()
            fut = concurrent.futures.Future()
            fut.set_result(None)
            return fut

        monkeypatch.setattr(
            asyncio,
            "run_coroutine_threadsafe",
            fake_run_coroutine_threadsafe,
        )

        a.notify_tool_started("chat-1", "terminal")

        assert len(scheduled) == 1
        assert scheduled[0][1] is fake_loop

    def test_notify_tool_started_is_noop_when_category_unchanged(
        self, adapter_with_card,
    ):
        """Back-to-back terminal calls do not flicker — only the first
        call swaps the label, subsequent ones short-circuit."""
        a = adapter_with_card
        terminal_label = a._TOOL_STAGE_LABELS["terminal"]
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal")
            a.notify_tool_started("chat-1", "terminal")
            a.notify_tool_started("chat-1", "terminal")
        finally:
            a._test_stage_loop.close()

        # One swap pair, not three.
        assert calls == [
            (a.REACTION_THINKING, True),
            (terminal_label, False),
        ]

    def test_notify_tool_started_uses_terminal_preview_specific_label(
        self, adapter_with_card,
    ):
        """Terminal commands can refine the coarse terminal stage label."""
        a = adapter_with_card
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal", preview="pytest tests/gateway")
        finally:
            a._test_stage_loop.close()

        assert calls == [
            (a.REACTION_THINKING, True),
            ("🧪 跑测试中", False),
        ]
        assert a._current_stage_label["chat-1"] == "🧪 跑测试中"

    def test_notify_tool_started_swaps_again_on_category_change(
        self, adapter_with_card,
    ):
        """terminal → read_file → web_search: each category transition
        fires one recall+reply pair; the next pair recalls the *current*
        label (not Thinking) so the swap matches what's rendered."""
        a = adapter_with_card
        terminal_label = a._TOOL_STAGE_LABELS["terminal"]
        read_file_label = a._TOOL_STAGE_LABELS["read_file"]
        web_search_label = a._TOOL_STAGE_LABELS["web_search"]
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal")
            a.notify_tool_started("chat-1", "read_file")
            a.notify_tool_started("chat-1", "web_search")
        finally:
            a._test_stage_loop.close()

        assert calls == [
            (a.REACTION_THINKING, True), (terminal_label, False),
            (terminal_label, True),      (read_file_label, False),
            (read_file_label, True),     (web_search_label, False),
        ]
        assert a._current_stage_label["chat-1"] == web_search_label

    def test_notify_tool_started_noop_for_uncategorized_tool(
        self, adapter_with_card,
    ):
        """Tools without a stage label leave the current label intact
        — we don't have a sensible fallback so we just skip the swap."""
        a = adapter_with_card
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "totally_unknown_plugin_tool")
        finally:
            a._test_stage_loop.close()

        assert calls == []
        assert "chat-1" not in a._current_stage_label

    def test_notify_tool_started_noop_for_missing_message_context(
        self, adapter_with_card,
    ):
        """Without the inbound message context we cannot fire the
        emotion API — swap is silently skipped."""
        a = adapter_with_card
        a._message_contexts.clear()
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal")
        finally:
            a._test_stage_loop.close()

        assert calls == []

    def test_fire_done_reaction_recalls_current_stage_label(
        self, adapter_with_card,
    ):
        """After a stage swap, Done recalls the stage label — not
        Thinking. Otherwise the stage label would orphan on the
        message side. Also clears the per-chat state."""
        a = adapter_with_card
        terminal_label = a._TOOL_STAGE_LABELS["terminal"]
        calls = self._capture_stage_emotions(a)
        try:
            a.notify_tool_started("chat-1", "terminal")
            calls.clear()  # focus on what Done does
            a._fire_done_reaction("chat-1")
        finally:
            a._test_stage_loop.close()

        assert calls == [
            (terminal_label, True),
            (a.REACTION_DONE, False),
        ]
        assert "chat-1" not in a._current_stage_label

    def test_stage_label_for_tool_covers_high_volume_tools(
        self, adapter_with_card,
    ):
        """Sanity check that the stage table covers the tools the
        agent calls most often. If a tool drops from the catalog
        we want this to break loudly, not silently fall through to
        the "no swap" path.
        """
        a = adapter_with_card
        for name in (
            "terminal", "read_file", "write_file", "patch",
            "search_files", "web_search", "browser_navigate",
            "delegate_task", "memory",
        ):
            assert a._stage_label_for_tool(name), name
        assert a._stage_label_for_tool("totally_unknown_tool") is None
        assert a._stage_label_for_tool(None) is None
        assert a._stage_label_for_tool("") is None



# ---------------------------------------------------------------------------
# AI Card Tests
# ---------------------------------------------------------------------------

class TestDingTalkAdapterAICards:
    @pytest.fixture
    def config(self):
        return PlatformConfig(
            enabled=True,
            extra={
                "client_id": "test_id",
                "client_secret": "test_secret",
                "card_template_id": "test_card_template",
            },
        )

    @pytest.fixture
    def mock_stream_client(self):
        client = MagicMock()
        client.get_access_token = MagicMock(return_value="test_token")
        return client

    @pytest.fixture
    def mock_http_client(self):
        return AsyncMock()

    @pytest.fixture
    def mock_message(self):
        msg = MagicMock()
        msg.message_id = "test_msg_id"
        msg.conversation_id = "test_conv_id"
        msg.conversation_type = "1"
        msg.sender_id = "sender1"
        msg.sender_nick = "Test User"
        msg.sender_staff_id = "staff1"
        msg.text = MagicMock(content="Hello")
        msg.session_webhook = "https://api.dingtalk.com/robot/sendBySession?session=test"
        msg.session_webhook_expired_time = 999999999999
        msg.create_at = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        msg.at_users = []
        return msg

    @pytest.mark.asyncio
    async def test_send_uses_ai_card_if_configured(self, config, mock_stream_client, mock_http_client, mock_message):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(config)
        adapter._stream_client = mock_stream_client
        adapter._http_client = mock_http_client
        adapter._message_contexts["test_conv_id"] = mock_message
        adapter._session_webhooks = {"test_conv_id": ("https://api.dingtalk.com/robot/sendBySession?session=test", 9999999999999)}
        adapter._card_template_id = "test_card_template"

        # Mock the card SDK with proper async methods
        mock_card_sdk = MagicMock()
        mock_card_sdk.create_card_with_options_async = AsyncMock()
        mock_card_sdk.deliver_card_with_options_async = AsyncMock()
        mock_card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._card_sdk = mock_card_sdk

        # Mock access token
        adapter._get_access_token = AsyncMock(return_value="test_token")

        result = await adapter.send("test_conv_id", "Hello World")

        mock_card_sdk.create_card_with_options_async.assert_called_once()
        mock_card_sdk.deliver_card_with_options_async.assert_called_once()
        mock_card_sdk.streaming_update_with_options_async.assert_called_once()
        assert result.success is True

