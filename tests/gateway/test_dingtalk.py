"""Tests for DingTalk platform adapter."""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
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
    from gateway.platforms import dingtalk as dt

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
            "OrgGroupSendRequest",
            "OrgGroupSendHeaders",
            "PrivateChatSendRequest",
            "PrivateChatSendHeaders",
            "BatchSendOTORequest",
            "BatchSendOTOHeaders",
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


@pytest.fixture(autouse=True)
def _isolate_dingtalk_config_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


class TestDingTalkRequirements:

    def test_returns_false_when_sdk_missing(self, monkeypatch):
        with patch.dict("sys.modules", {"dingtalk_stream": None}), \
             patch("tools.lazy_deps.ensure", side_effect=ImportError("dingtalk_stream unavailable")):
            monkeypatch.setattr(
                "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", False
            )
            from gateway.platforms.dingtalk import check_dingtalk_requirements
            assert check_dingtalk_requirements() is False

    def test_returns_false_when_env_vars_missing(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", True
        )
        monkeypatch.setattr("gateway.platforms.dingtalk.HTTPX_AVAILABLE", True)
        monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
        from gateway.platforms.dingtalk import check_dingtalk_requirements
        assert check_dingtalk_requirements() is False

    def test_returns_true_when_all_available(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", True
        )
        monkeypatch.setattr("gateway.platforms.dingtalk.HTTPX_AVAILABLE", True)
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "test-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "test-secret")
        from gateway.platforms.dingtalk import check_dingtalk_requirements
        assert check_dingtalk_requirements() is True


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestDingTalkAdapterInit:

    def test_reads_config_from_extra(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        config = PlatformConfig(
            enabled=True,
            extra={"client_id": "cfg-id", "client_secret": "cfg-secret"},
        )
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "cfg-id"
        assert adapter._client_secret == "cfg-secret"
        assert adapter.name == "Dingtalk"  # base class uses .title()

    def test_reads_reserved_app_fields_from_extra(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        config = PlatformConfig(
            enabled=True,
            extra={
                "app_code": "app-123",
                "corp_id": "corp-123",
                "agent_id": "agent-123",
            },
        )
        adapter = DingTalkAdapter(config)
        assert adapter._app_code == "app-123"
        assert adapter._corp_id == "corp-123"
        assert adapter._agent_id == "agent-123"

    def test_blank_card_template_uses_default_ai_markdown_template(self):
        from gateway.platforms.dingtalk import (
            DEFAULT_AI_CARD_CONTENT_KEY,
            DEFAULT_AI_CARD_TEMPLATE_ID,
            DingTalkAdapter,
        )
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        assert adapter._card_template_id == DEFAULT_AI_CARD_TEMPLATE_ID
        assert adapter._card_content_key == DEFAULT_AI_CARD_CONTENT_KEY

    def test_custom_card_template_uses_default_content_key_when_config_empty(self):
        from gateway.platforms.dingtalk import DEFAULT_AI_CARD_CONTENT_KEY, DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"card_template_id": "custom-template"})
        )
        assert adapter._card_template_id == "custom-template"
        assert adapter._card_content_key == DEFAULT_AI_CARD_CONTENT_KEY

    def test_custom_card_template_can_read_dashboard_content_key(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        config_path = os.path.join(os.environ["HERMES_HOME"], "config.yaml")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("dingtalk:\n  card_content_key: content\n")

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"card_template_id": "custom-template"})
        )
        assert adapter._card_template_id == "custom-template"
        assert adapter._card_content_key == "content"

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env-secret")
        from gateway.platforms.dingtalk import DingTalkAdapter
        config = PlatformConfig(enabled=True)
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "env-id"
        assert adapter._client_secret == "env-secret"


# ---------------------------------------------------------------------------
# Message text extraction
# ---------------------------------------------------------------------------


class TestExtractText:

    def test_extracts_dict_text(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = {"content": "  hello world  "}
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == "hello world"

    def test_extracts_string_text(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = "plain text"
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == "plain text"

    def test_falls_back_to_rich_text(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = ""
        msg.rich_text = [{"text": "part1"}, {"text": "part2"}, {"image": "url"}]
        assert DingTalkAdapter._extract_text(msg) == "part1 part2"

    def test_returns_empty_for_no_content(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = ""
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == ""


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:

    def test_first_message_not_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        assert adapter._dedup.is_duplicate("msg-1") is False

    def test_second_same_message_is_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._dedup.is_duplicate("msg-1")
        assert adapter._dedup.is_duplicate("msg-1") is True

    def test_different_messages_not_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._dedup.is_duplicate("msg-1")
        assert adapter._dedup.is_duplicate("msg-2") is False

    def test_cache_cleanup_on_overflow(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        max_size = adapter._dedup._max_size
        # Fill beyond max
        for i in range(max_size + 10):
            adapter._dedup.is_duplicate(f"msg-{i}")
        # Cache should have been pruned
        assert len(adapter._dedup._seen) <= max_size + 10


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:

    @pytest.mark.asyncio
    async def test_send_posts_to_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
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
    async def test_send_fails_without_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._http_client = AsyncMock()

        result = await adapter.send("chat-123", "Hello!")
        assert result.success is False
        assert "session_webhook" in result.error

    @pytest.mark.asyncio
    async def test_send_uses_cached_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client
        adapter._session_webhooks["chat-123"] = ("https://cached.example/webhook", 9999999999999)

        result = await adapter.send("chat-123", "Hello!")
        assert result.success is True
        assert mock_client.post.call_args[0][0] == "https://cached.example/webhook"

    @pytest.mark.asyncio
    async def test_send_handles_http_error(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"session_webhook": "https://example/webhook"}
        )
        assert result.success is False
        assert "400" in result.error

    @pytest.mark.asyncio
    async def test_send_image_renders_markdown_image(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
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
    async def test_final_group_reply_can_at_sender(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"reply_at_sender": True})
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client
        adapter._message_contexts["chat-123"] = MagicMock(
            conversation_type="2",
            sender_staff_id="staff-1",
            sender_nick="Alice",
        )

        result = await adapter.send(
            "chat-123",
            "Hello!",
            reply_to="msg-1",
            metadata={"session_webhook": "https://example/webhook"},
        )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["at"]["atUserIds"] == ["staff-1"]
        assert payload["at"]["isAtAll"] is False
        assert payload["markdown"]["text"].startswith("@staff-1\n\nHello!")

    @pytest.mark.asyncio
    async def test_custom_emotion_tag_is_stripped_and_sent_as_reaction(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        fired = []
        adapter._fire_custom_reactions = lambda chat_id, names: fired.append((chat_id, names))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123",
            "Done [[dingtalk:emotion=🥳Done]]",
            metadata={"session_webhook": "https://example/webhook"},
        )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["markdown"]["text"] == "Done"
        assert fired == [("chat-123", ["🥳Done"])]

    @pytest.mark.asyncio
    async def test_send_document_with_public_url_renders_markdown_link(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send_document(
            "chat-123",
            "https://example.com/report.pdf",
            caption="Report",
            file_name="report.pdf",
            metadata={"session_webhook": "https://example/webhook"},
        )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["markdown"]["text"] == "Report\n\n[report.pdf](https://example.com/report.pdf)"

    @pytest.mark.asyncio
    async def test_send_image_file_uploads_and_sends_native_image(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )

        image_path = tmp_path / "demo.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        upload_response = MagicMock()
        upload_response.status_code = 200
        upload_response.json.return_value = {"errcode": 0, "media_id": "@media-image"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=upload_response)
        adapter._http_client = mock_client
        adapter._get_access_token = AsyncMock(return_value="access-token")
        adapter._robot_sdk = SimpleNamespace(
            org_group_send_with_options_async=AsyncMock(
                return_value=SimpleNamespace(body=SimpleNamespace(process_query_key="pq-1"))
            )
        )

        result = await adapter.send_image_file("chat-123", str(image_path))

        assert result.success is True
        assert result.message_id == "pq-1"
        upload_call = mock_client.post.call_args
        assert upload_call.args[0].endswith("/media/upload")
        assert upload_call.kwargs["params"] == {
            "access_token": "access-token",
            "type": "image",
        }
        native_request = adapter._robot_sdk.org_group_send_with_options_async.call_args.args[0]
        assert native_request.msg_key == "sampleImageMsg"
        assert json.loads(native_request.msg_param) == {"photoURL": "@media-image"}
        assert native_request.open_conversation_id == "chat-123"
        assert native_request.robot_code == "robot-1"

    @pytest.mark.asyncio
    async def test_native_robot_message_uses_oto_for_direct_robot_chat(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"robot_code": "robot-1"}))
        adapter._get_access_token = AsyncMock(return_value="access-token")
        adapter._message_contexts["dm-chat"] = SimpleNamespace(
            conversation_type="1",
            sender_staff_id="staff-1",
            robot_code="robot-from-callback",
        )
        batch_send = AsyncMock(
            return_value=SimpleNamespace(body=SimpleNamespace(process_query_key="pq-oto"))
        )
        adapter._robot_sdk = SimpleNamespace(batch_send_otowith_options_async=batch_send)

        result = await adapter._send_robot_native_message(
            chat_id="dm-chat",
            msg_key="sampleImageMsg",
            msg_param={"photoURL": "@media-image"},
        )

        assert result.success is True
        assert result.message_id == "pq-oto"
        request = batch_send.call_args.args[0]
        assert request.msg_key == "sampleImageMsg"
        assert json.loads(request.msg_param) == {"photoURL": "@media-image"}
        assert request.robot_code == "robot-from-callback"
        assert request.user_ids == ["staff-1"]

    @pytest.mark.asyncio
    async def test_send_image_file_falls_back_to_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )

        image_path = tmp_path / "demo.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        image_upload = MagicMock()
        image_upload.status_code = 200
        image_upload.json.return_value = {"errcode": 0, "media_id": "@media-image"}
        file_upload = MagicMock()
        file_upload.status_code = 200
        file_upload.json.return_value = {"errcode": 0, "media_id": "@media-file"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[image_upload, file_upload])
        adapter._http_client = mock_client
        adapter._get_access_token = AsyncMock(return_value="access-token")
        native_send = AsyncMock(
            side_effect=[
                Exception("Error: resource.not.found code: 400"),
                SimpleNamespace(body=SimpleNamespace(process_query_key="pq-file")),
            ]
        )
        adapter._robot_sdk = SimpleNamespace(
            org_group_send_with_options_async=native_send
        )

        result = await adapter.send_image_file("chat-123", str(image_path))

        assert result.success is True
        assert result.message_id == "pq-file"
        assert mock_client.post.call_args_list[0].kwargs["params"]["type"] == "image"
        assert mock_client.post.call_args_list[1].kwargs["params"]["type"] == "file"
        image_request = native_send.call_args_list[0].args[0]
        file_request = native_send.call_args_list[1].args[0]
        assert image_request.msg_key == "sampleImageMsg"
        assert file_request.msg_key == "sampleFile"
        assert json.loads(file_request.msg_param) == {
            "mediaId": "@media-file",
            "fileName": "demo.png",
            "fileType": "png",
        }

    @pytest.mark.asyncio
    async def test_send_document_uploads_and_sends_native_file(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )

        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"%PDF-1.7\n")
        upload_response = MagicMock()
        upload_response.status_code = 200
        upload_response.json.return_value = {"errcode": 0, "media_id": "@media-file"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=upload_response)
        adapter._http_client = mock_client
        adapter._get_access_token = AsyncMock(return_value="access-token")
        adapter._robot_sdk = SimpleNamespace(
            org_group_send_with_options_async=AsyncMock(
                return_value=SimpleNamespace(body=SimpleNamespace(process_query_key="pq-2"))
            )
        )

        result = await adapter.send_document("chat-123", str(file_path))

        assert result.success is True
        assert result.message_id == "pq-2"
        upload_call = mock_client.post.call_args
        assert upload_call.args[0].endswith("/media/upload")
        assert upload_call.kwargs["params"] == {
            "access_token": "access-token",
            "type": "file",
        }
        assert upload_call.kwargs["files"]["media"][2] == "application/octet-stream"
        native_request = adapter._robot_sdk.org_group_send_with_options_async.call_args.args[0]
        assert native_request.msg_key == "sampleFile"
        assert json.loads(native_request.msg_param) == {
            "mediaId": "@media-file",
            "fileName": "report.pdf",
            "fileType": "pdf",
        }

    @pytest.mark.asyncio
    async def test_send_document_uploads_html_as_opaque_file(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )
        file_path = tmp_path / "demo.html"
        file_path.write_text("<h1>demo</h1>", encoding="utf-8")
        upload_response = MagicMock()
        upload_response.status_code = 200
        upload_response.json.return_value = {"errcode": 0, "media_id": "@media-html"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=upload_response)
        adapter._http_client = mock_client
        adapter._get_access_token = AsyncMock(return_value="access-token")
        adapter._robot_sdk = SimpleNamespace(
            org_group_send_with_options_async=AsyncMock(
                return_value=SimpleNamespace(body=SimpleNamespace(process_query_key="pq-html"))
            )
        )

        result = await adapter.send_document("chat-123", str(file_path))

        assert result.success is True
        upload_call = mock_client.post.call_args
        assert upload_call.kwargs["files"]["media"][0] == "demo.html"
        assert upload_call.kwargs["files"]["media"][2] == "application/octet-stream"
        native_request = adapter._robot_sdk.org_group_send_with_options_async.call_args.args[0]
        assert json.loads(native_request.msg_param) == {
            "mediaId": "@media-html",
            "fileName": "demo.html",
            "fileType": "html",
        }

    @pytest.mark.asyncio
    async def test_send_document_returns_upload_error_for_unsupported_file_types(self, tmp_path):
        from gateway.platforms.base import SendResult
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )
        file_path = tmp_path / "demo.html"
        file_path.write_text("<h1>demo</h1>", encoding="utf-8")
        adapter._upload_robot_media = AsyncMock(
            return_value=SendResult(
                success=False,
                error="DingTalk media upload failed: 40005 unsupported file type",
            )
        )
        adapter._send_robot_native_message = AsyncMock()

        result = await adapter.send_document("chat-123", str(file_path))

        assert result.success is False
        assert result.error == "DingTalk media upload failed: 40005 unsupported file type"
        adapter._upload_robot_media.assert_awaited_once_with(str(file_path), media_type="file")
        adapter._send_robot_native_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_video_with_cover_sends_native_video_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"robot_code": "robot-1"})
        )
        video_path = tmp_path / "demo.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        cover_path = tmp_path / "cover.jpg"
        cover_path.write_bytes(b"jpg")
        adapter._upload_robot_media = AsyncMock(
            side_effect=[
                SimpleNamespace(success=True, message_id="@media-video"),
                SimpleNamespace(success=True, message_id="@media-cover"),
            ]
        )
        adapter._send_robot_native_message = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="native-video")
        )

        result = await adapter.send_video(
            "chat-123",
            str(video_path),
            metadata={"video_cover_path": str(cover_path), "duration_ms": 2500},
        )

        assert result.success is True
        assert result.message_id == "native-video"
        assert adapter._upload_robot_media.await_args_list[0].kwargs == {"media_type": "video"}
        assert adapter._upload_robot_media.await_args_list[1].kwargs == {"media_type": "image"}
        adapter._send_robot_native_message.assert_awaited_once_with(
            chat_id="chat-123",
            msg_key="sampleVideo",
            msg_param={
                "videoMediaId": "@media-video",
                "videoType": "mp4",
                "picMediaId": "@media-cover",
                "duration": "2500",
            },
            metadata={"video_cover_path": str(cover_path), "duration_ms": 2500},
        )

    @pytest.mark.asyncio
    async def test_send_video_without_cover_falls_back_to_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        video_path = tmp_path / "demo.mp4"
        video_path.write_bytes(b"not a real mp4")
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="file-video")
        )

        result = await adapter.send_video(
            "chat-123",
            str(video_path),
            caption="demo video",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

        assert result.success is True
        adapter.send_document.assert_awaited_once_with(
            chat_id="chat-123",
            file_path=str(video_path),
            caption="demo video",
            file_name="demo.mp4",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

    @pytest.mark.asyncio
    async def test_send_video_without_cover_uses_default_cover(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        video_path = tmp_path / "demo.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        adapter._generate_video_cover = lambda _path: None
        adapter._upload_robot_media = AsyncMock(
            side_effect=[
                SimpleNamespace(success=True, message_id="@media-video"),
                SimpleNamespace(success=True, message_id="@media-cover"),
            ]
        )
        adapter._send_robot_native_message = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="native-video")
        )

        result = await adapter.send_video(
            "chat-123",
            str(video_path),
            metadata={"duration_ms": 1000},
        )

        assert result.success is True
        cover_arg = adapter._upload_robot_media.await_args_list[1].args[0]
        assert Path(cover_arg).name.startswith("hermes_dingtalk_video_cover_")
        adapter._send_robot_native_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_voice_ogg_sends_native_audio_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        audio_path = tmp_path / "demo.ogg"
        audio_path.write_bytes(b"OggS" + b"\x00" * 16)
        adapter._upload_robot_media = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="@media-audio")
        )
        adapter._send_robot_native_message = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="native-audio")
        )

        result = await adapter.send_voice(
            "chat-123",
            str(audio_path),
            metadata={"duration_seconds": 3},
        )

        assert result.success is True
        assert result.message_id == "native-audio"
        adapter._upload_robot_media.assert_awaited_once_with(str(audio_path), media_type="voice")
        adapter._send_robot_native_message.assert_awaited_once_with(
            chat_id="chat-123",
            msg_key="sampleAudio",
            msg_param={"mediaId": "@media-audio", "duration": "3000"},
            metadata={"duration_seconds": 3},
        )

    @pytest.mark.asyncio
    async def test_send_voice_invalid_ogg_falls_back_to_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        audio_path = tmp_path / "demo.ogg"
        audio_path.write_bytes(b"not real ogg")
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="file-audio")
        )

        result = await adapter.send_voice(
            "chat-123",
            str(audio_path),
            caption="demo audio",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

        assert result.success is True
        adapter.send_document.assert_awaited_once_with(
            chat_id="chat-123",
            file_path=str(audio_path),
            caption="demo audio",
            file_name="demo.ogg",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

    @pytest.mark.asyncio
    async def test_send_voice_unsupported_audio_falls_back_to_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"mp3")
        adapter.send_document = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="file-audio")
        )

        result = await adapter.send_voice(
            "chat-123",
            str(audio_path),
            caption="demo audio",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

        assert result.success is True
        adapter.send_document.assert_awaited_once_with(
            chat_id="chat-123",
            file_path=str(audio_path),
            caption="demo audio",
            file_name="demo.mp3",
            reply_to="msg-1",
            metadata={"k": "v"},
        )

    @pytest.mark.asyncio
    async def test_send_video_with_public_url_renders_markdown_link(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send_video(
            "chat-123",
            "https://example.com/demo.mp4",
            caption="Video",
            metadata={"session_webhook": "https://example/webhook"},
        )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["markdown"]["text"] == "Video\n\n[demo.mp4](https://example.com/demo.mp4)"

    @pytest.mark.asyncio
    async def test_send_video_missing_local_file_reports_missing_file(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_video("chat-123", "/tmp/demo.mp4")

        assert result.success is False
        assert result.error and "Local file not found" in result.error

    @pytest.mark.asyncio
    async def test_send_voice_missing_local_file_reports_missing_file(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        result = await adapter.send_voice("chat-123", "/tmp/demo.ogg")

        assert result.success is False
        assert result.error and "Local file not found" in result.error


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:

    @pytest.mark.asyncio
    async def test_disconnect_closes_session_websocket(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        websocket = AsyncMock()
        blocker = asyncio.Event()

        async def _run_forever():
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                return

        adapter._stream_client = SimpleNamespace(websocket=websocket)
        adapter._stream_task = asyncio.create_task(_run_forever())
        adapter._running = True

        await adapter.disconnect()

        websocket.close.assert_awaited_once()
        assert adapter._stream_task is None

    @pytest.mark.asyncio
    async def test_connect_fails_without_sdk(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", False
        )
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_fails_without_credentials(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._client_id = ""
        adapter._client_secret = ""
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._session_webhooks["a"] = "http://x"
        adapter._dedup._seen["b"] = 1.0
        adapter._http_client = AsyncMock()
        adapter._stream_task = None

        await adapter.disconnect()
        assert len(adapter._session_webhooks) == 0
        assert len(adapter._dedup._seen) == 0
        assert adapter._http_client is None

    @pytest.mark.asyncio
    async def test_disconnect_finalizes_open_streaming_cards(self):
        """Streaming cards must be finalized before HTTP client closes."""
        from unittest.mock import AsyncMock, patch
        from gateway.platforms.dingtalk import DingTalkAdapter
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
        from gateway.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
        assert _DINGTALK_WEBHOOK_RE.match(
            "https://api.dingtalk.com/robot/send?access_token=x"
        )

    def test_oapi_domain_accepted(self):
        from gateway.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
        assert _DINGTALK_WEBHOOK_RE.match(
            "https://oapi.dingtalk.com/robot/send?access_token=x"
        )

    def test_http_rejected(self):
        from gateway.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
        assert not _DINGTALK_WEBHOOK_RE.match("http://api.dingtalk.com/robot/send")

    def test_suffix_attack_rejected(self):
        from gateway.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
        assert not _DINGTALK_WEBHOOK_RE.match(
            "https://api.dingtalk.com.evil.example/"
        )

    def test_unsanctioned_subdomain_rejected(self):
        from gateway.platforms.dingtalk import _DINGTALK_WEBHOOK_RE
        # Only api.* and oapi.* are allowed — e.g. eapi.dingtalk.com must not slip through
        assert not _DINGTALK_WEBHOOK_RE.match("https://eapi.dingtalk.com/robot/send")


class TestHandlerProcessIsAsync:
    """dingtalk-stream >= 0.20 requires ``process`` to be a coroutine."""

    def test_process_is_coroutine_function(self):
        from gateway.platforms.dingtalk import _IncomingHandler
        assert asyncio.iscoroutinefunction(_IncomingHandler.process)


class TestExtractText:
    """_extract_text must handle both legacy and current SDK payload shapes.

    Before SDK 0.20 ``message.text`` was a ``dict`` with a ``content`` key.
    From 0.20 onward it is a ``TextContent`` dataclass whose ``__str__``
    returns ``"TextContent(content=...)"`` — falling back to ``str(text)``
    leaks that repr into the agent's input.
    """

    def test_text_as_dict_legacy(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = {"content": "hello world"}
        msg.rich_text_content = None
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == "hello world"

    def test_text_as_textcontent_object(self):
        """SDK >= 0.20 shape: object with ``.content`` attribute."""
        from gateway.platforms.dingtalk import DingTalkAdapter

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

    def test_text_content_attr_with_empty_string(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        class FakeTextContent:
            content = ""

        msg = MagicMock()
        msg.text = FakeTextContent()
        msg.rich_text_content = None
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == ""

    def test_rich_text_content_new_shape(self):
        """SDK >= 0.20 exposes rich text as ``message.rich_text_content.rich_text_list``."""
        from gateway.platforms.dingtalk import DingTalkAdapter

        class FakeRichText:
            rich_text_list = [{"text": "hello "}, {"text": "world"}]

        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = FakeRichText()
        msg.rich_text = None
        result = DingTalkAdapter._extract_text(msg)
        assert "hello" in result and "world" in result

    def test_rich_text_legacy_shape(self):
        """Legacy ``message.rich_text`` list remains supported."""
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = None
        msg.rich_text = [{"text": "legacy "}, {"text": "rich"}]
        result = DingTalkAdapter._extract_text(msg)
        assert "legacy" in result and "rich" in result

    def test_empty_message(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = None
        msg.rich_text = None
        assert DingTalkAdapter._extract_text(msg) == ""


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
        from gateway.platforms.dingtalk import DingTalkAdapter
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

    def test_audio_rich_text_item_stays_audio(self):
        """Generic audio uploads (e.g. an mp3 the user attached) must NOT
        be auto-transcribed — they stay MessageType.AUDIO."""
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "audio", "downloadCode": "dl_audio_xyz", "fileName": "clip.mp3"}]
        )
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.AUDIO
        assert urls == ["dl_audio_xyz"]
        assert mtypes == ["audio/mpeg"]

    def test_file_rich_text_item_uses_filename_mime(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "file", "downloadCode": "dl_doc_abc", "fileName": "report.pdf"}]
        )
        msg.message_type = "richText"
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(DingTalkAdapter, msg)

        assert msg_type == MessageType.DOCUMENT
        assert urls == ["dl_doc_abc"]
        assert mtypes == ["application/pdf"]

    def test_cached_media_metadata_wins_over_filename_guess(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        msg = self._msg_with_rich_text(
            [
                {
                    "type": "file",
                    "downloadCode": "/tmp/hermes-doc",
                    "fileName": "unknown.bin",
                    "_hermes_media_type": "text/plain",
                }
            ]
        )
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(DingTalkAdapter, msg)

        assert msg_type == MessageType.DOCUMENT
        assert urls == ["/tmp/hermes-doc"]
        assert mtypes == ["text/plain"]


class TestDingTalkMediaResolution:
    @pytest.mark.asyncio
    async def test_resolve_media_codes_caches_image_content(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._fetch_download_url = AsyncMock()
        msg = SimpleNamespace(
            robot_code="robot",
            image_content=SimpleNamespace(download_code="img-code"),
            rich_text_content=None,
            rich_text=None,
        )

        await adapter._resolve_media_codes(msg)

        adapter._fetch_download_url.assert_awaited_once_with(
            "img-code",
            "robot",
            "token",
            msg.image_content,
            "download_code",
            mapped="image",
            filename=None,
        )

    @pytest.mark.asyncio
    async def test_resolve_media_codes_handles_rich_text_files(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value="token")
        adapter._fetch_download_url = AsyncMock()
        rich_item = {
            "type": "file",
            "downloadCode": "file-code",
            "fileName": "report.xlsx",
        }
        msg = SimpleNamespace(
            robot_code="robot",
            image_content=None,
            rich_text_content=SimpleNamespace(rich_text_list=[rich_item]),
            rich_text=None,
        )

        await adapter._resolve_media_codes(msg)

        adapter._fetch_download_url.assert_awaited_once_with(
            "file-code",
            "robot",
            "token",
            rich_item,
            "downloadCode",
            mapped="file",
            filename="report.xlsx",
        )

    @pytest.mark.asyncio
    async def test_resolve_media_codes_caches_direct_download_url_without_token(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value=None)
        adapter._cache_resolved_media_url = AsyncMock()
        rich_item = {
            "type": "file",
            "downloadUrl": "https://media.example/file",
            "fileName": "report.pdf",
        }
        msg = SimpleNamespace(
            robot_code="robot",
            image_content=None,
            rich_text_content=SimpleNamespace(rich_text_list=[rich_item]),
            rich_text=None,
        )

        await adapter._resolve_media_codes(msg)

        adapter._get_access_token.assert_not_awaited()
        adapter._cache_resolved_media_url.assert_awaited_once_with(
            "https://media.example/file",
            rich_item,
            "downloadUrl",
            mapped="file",
            filename="report.pdf",
        )

    @pytest.mark.asyncio
    async def test_fetch_download_url_mutates_item_to_cached_path(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._robot_sdk = SimpleNamespace(
            robot_message_file_download_with_options_async=AsyncMock(
                return_value=SimpleNamespace(
                    body=SimpleNamespace(download_url="https://media.example/file")
                )
            )
        )
        adapter._cache_media_url = AsyncMock(
            return_value=("/tmp/hermes-cache/doc.txt", "text/plain")
        )
        item = {"type": "file", "downloadCode": "file-code", "fileName": "doc.txt"}

        await adapter._fetch_download_url(
            "file-code",
            "robot",
            "token",
            item,
            "downloadCode",
            mapped="file",
            filename="doc.txt",
        )

        assert item["downloadCode"] == "/tmp/hermes-cache/doc.txt"
        assert item["_hermes_media_type"] == "text/plain"
        assert item["_hermes_file_name"] == "doc.txt"

    @pytest.mark.asyncio
    async def test_cache_resolved_media_url_records_error_without_routing_url(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._cache_media_url = AsyncMock(side_effect=RuntimeError("boom"))
        item = {"type": "file", "downloadUrl": "https://media.example/file"}

        await adapter._cache_resolved_media_url(
            "https://media.example/file",
            item,
            "downloadUrl",
            mapped="file",
            filename="report.pdf",
        )

        assert item["downloadUrl"] == "https://media.example/file"
        assert "DingTalk media download failed: boom" == item["_hermes_media_error"]

    @pytest.mark.asyncio
    async def test_cache_media_url_reuses_document_cache(self, monkeypatch):
        from gateway.platforms import dingtalk as dt
        from gateway.platforms.dingtalk import DingTalkAdapter

        captured = {}

        class FakeResponse:
            headers = {"content-type": "text/plain; charset=utf-8"}
            content = b"hello"

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers):
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr(dt.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(
            dt,
            "cache_document_from_bytes",
            lambda data, filename: f"/tmp/cached/{filename}",
        )
        monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: True)
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        path, media_type = await adapter._cache_media_url(
            "https://media.example/file",
            "file",
            "note.txt",
        )

        assert path == "/tmp/cached/note.txt"
        assert media_type == "text/plain"
        assert captured["url"] == "https://media.example/file"
        assert captured["headers"]["Accept"].startswith("application/octet-stream")
        assert captured["client_kwargs"]["follow_redirects"] is True
        assert captured["client_kwargs"]["trust_env"] is False


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
    from gateway.platforms.dingtalk import DingTalkAdapter
    return DingTalkAdapter(PlatformConfig(enabled=True, extra=extra or {}))


class TestAllowedUsersGate:

    def test_empty_allowlist_allows_everyone(self, monkeypatch):
        adapter = _make_gating_adapter(monkeypatch)
        assert adapter._is_user_allowed("anyone", "any-staff") is True

    def test_wildcard_allowlist_allows_everyone(self, monkeypatch):
        adapter = _make_gating_adapter(monkeypatch, extra={"allowed_users": ["*"]})
        assert adapter._is_user_allowed("anyone", "any-staff") is True

    def test_matches_sender_id_case_insensitive(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"allowed_users": ["SenderABC"]}
        )
        assert adapter._is_user_allowed("senderabc", "") is True

    def test_matches_staff_id(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"allowed_users": ["staff_1234"]}
        )
        assert adapter._is_user_allowed("", "staff_1234") is True

    def test_rejects_unknown_user(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"allowed_users": ["staff_1234"]}
        )
        assert adapter._is_user_allowed("other-sender", "other-staff") is False

    def test_env_var_csv_populates_allowlist(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, env={"DINGTALK_ALLOWED_USERS": "alice,bob,carol"}
        )
        assert adapter._is_user_allowed("alice", "") is True
        assert adapter._is_user_allowed("dave", "") is False


class TestMentionPatterns:

    def test_empty_patterns_list(self, monkeypatch):
        adapter = _make_gating_adapter(monkeypatch)
        assert adapter._mention_patterns == []
        assert adapter._message_matches_mention_patterns("anything") is False

    def test_pattern_matches_text(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"mention_patterns": ["^hermes"]}
        )
        assert adapter._message_matches_mention_patterns("hermes please help") is True
        assert adapter._message_matches_mention_patterns("please hermes help") is False

    def test_pattern_is_case_insensitive(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"mention_patterns": ["^hermes"]}
        )
        assert adapter._message_matches_mention_patterns("HERMES help") is True

    def test_invalid_regex_is_skipped_not_raised(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            extra={"mention_patterns": ["[unclosed", "^valid"]},
        )
        # Invalid pattern dropped, valid one kept
        assert len(adapter._mention_patterns) == 1
        assert adapter._message_matches_mention_patterns("valid trigger") is True

    def test_env_var_json_populates_patterns(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            env={"DINGTALK_MENTION_PATTERNS": '["^bot", "^assistant"]'},
        )
        assert len(adapter._mention_patterns) == 2
        assert adapter._message_matches_mention_patterns("bot ping") is True

    def test_env_var_newline_fallback_when_not_json(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            env={"DINGTALK_MENTION_PATTERNS": "^bot\n^assistant"},
        )
        assert len(adapter._mention_patterns) == 2


class TestShouldProcessMessage:

    def test_dm_always_accepted(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"require_mention": True}
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hi", is_group=False, chat_id="dm1") is True

    def test_group_rejected_when_require_mention_and_no_trigger(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"require_mention": True}
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hi", is_group=True, chat_id="grp1") is False

    def test_group_accepted_when_require_mention_disabled(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"require_mention": False}
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hi", is_group=True, chat_id="grp1") is True

    def test_group_accepted_when_bot_is_mentioned(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch, extra={"require_mention": True}
        )
        msg = MagicMock(is_in_at_list=True)
        assert adapter._should_process_message(msg, "hi", is_group=True, chat_id="grp1") is True

    def test_group_accepted_when_text_matches_wake_word(self, monkeypatch):
        adapter = _make_gating_adapter(
            monkeypatch,
            extra={"require_mention": True, "mention_patterns": ["^hermes"]},
        )
        msg = MagicMock(is_in_at_list=False)
        assert adapter._should_process_message(msg, "hermes help", is_group=True, chat_id="grp1") is True

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
    async def test_process_extracts_session_webhook(self):
        """session_webhook must be populated from callback data."""
        from gateway.platforms.dingtalk import _IncomingHandler, DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        callback.data = {
            "msgtype": "text",
            "text": {"content": "hello"},
            "senderId": "user1",
            "conversationId": "conv1",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
            "msgId": "msg-001",
        }

        result = await handler.process(callback)
        # Should return ACK immediately (STATUS_OK = 200)
        assert result[0] == 200

        # Let the background task run
        await asyncio.sleep(0.05)

        # _on_message should have been called with a ChatbotMessage
        adapter._on_message.assert_called_once()
        chatbot_msg = adapter._on_message.call_args[0][0]
        assert chatbot_msg.session_webhook == "https://oapi.dingtalk.com/robot/sendBySession?session=abc"

    @pytest.mark.asyncio
    async def test_process_fallback_session_webhook_when_from_dict_misses_it(self):
        """If ChatbotMessage.from_dict does not map sessionWebhook (e.g. SDK
        version mismatch), the handler should fall back to extracting it
        directly from the raw data dict."""
        from gateway.platforms.dingtalk import _IncomingHandler, DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        # Use a key that from_dict might not recognise in some SDK versions
        callback.data = {
            "msgtype": "text",
            "text": {"content": "hi"},
            "senderId": "user2",
            "conversationId": "conv2",
            "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=def",
            "msgId": "msg-002",
        }

        await handler.process(callback)
        await asyncio.sleep(0.05)

        adapter._on_message.assert_called_once()
        chatbot_msg = adapter._on_message.call_args[0][0]
        assert chatbot_msg.session_webhook == "https://oapi.dingtalk.com/robot/sendBySession?session=def"

    @pytest.mark.asyncio
    async def test_process_backfills_sender_staff_id_for_reply_at_sender(self, monkeypatch):
        """Raw senderStaffId must survive SDK field-mapping differences.

        ``reply_at_sender`` uses sender_staff_id as DingTalk's @ target.  If
        the SDK's ChatbotMessage.from_dict omits the field, the adapter must
        copy it from the raw callback payload.
        """
        from gateway.platforms import dingtalk as dt
        from gateway.platforms.dingtalk import _IncomingHandler, DingTalkAdapter

        class MinimalChatbotMessage(SimpleNamespace):
            @classmethod
            def from_dict(cls, data):
                return cls(
                    message_id=data.get("msgId") or "",
                    conversation_id=data.get("conversationId") or "",
                    text=data.get("text") or "",
                    session_webhook=data.get("sessionWebhook") or "",
                )

        monkeypatch.setattr(dt, "ChatbotMessage", MinimalChatbotMessage)

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()
        handler = _IncomingHandler(adapter, asyncio.get_running_loop())

        callback = MagicMock()
        callback.data = {
            "msgtype": "text",
            "text": {"content": "hi"},
            "msgId": "msg-003",
            "conversationId": "conv3",
            "conversationType": "2",
            "senderId": "sender-open-id",
            "senderStaffId": "staff-003",
            "senderNick": "Alice",
            "createAt": "1770000000000",
            "robotCode": "robot-from-callback",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=ghi",
        }

        result = await handler.process(callback)
        assert result[0] == 200
        await asyncio.sleep(0.05)

        adapter._on_message.assert_called_once()
        chatbot_msg = adapter._on_message.call_args[0][0]
        assert chatbot_msg.sender_staff_id == "staff-003"
        assert chatbot_msg.sender_id == "sender-open-id"
        assert chatbot_msg.sender_nick == "Alice"
        assert chatbot_msg.conversation_type == "2"
        assert chatbot_msg.create_at == "1770000000000"
        assert chatbot_msg.robot_code == "robot-from-callback"

    @pytest.mark.asyncio
    async def test_process_returns_ack_immediately(self):
        """process() must not block on _on_message — it should return
        the ACK tuple before the message is fully processed."""
        from gateway.platforms.dingtalk import _IncomingHandler, DingTalkAdapter

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
        from gateway.platforms.dingtalk import DingTalkAdapter
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

    def test_dingtalk_in_platform_enum(self):
        assert Platform.DINGTALK.value == "dingtalk"


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Concurrency — chat-scoped message context
# ---------------------------------------------------------------------------


class TestMessageContextIsolation:

    def test_contexts_keyed_by_chat_id(self):
        """Two concurrent chats must not clobber each other's context."""
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        msg_a = MagicMock(conversation_id="chat-A", sender_staff_id="user-A")
        msg_b = MagicMock(conversation_id="chat-B", sender_staff_id="user-B")
        adapter._message_contexts["chat-A"] = msg_a
        adapter._message_contexts["chat-B"] = msg_b

        assert adapter._message_contexts["chat-A"] is msg_a
        assert adapter._message_contexts["chat-B"] is msg_b






# ---------------------------------------------------------------------------
# Card lifecycle: finalize via metadata["streaming"]
# ---------------------------------------------------------------------------


class TestCardLifecycle:

    @pytest.fixture
    def adapter_with_card(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
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
    async def test_intermediate_send_stays_streaming(self, adapter_with_card):
        """send() without reply_to creates an OPEN card (tool progress /
        commentary / streaming first chunk).  No flicker closed→streaming
        when edit_message follows."""
        a = adapter_with_card
        result = await a.send("chat-1", "💻 terminal: ls")
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

        # Tool-progress / commentary path: no reply_to — no Done.
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
        """Tool-progress card left open (send without reply_to + edits) must
        be auto-closed when the final-reply send arrives."""
        a = adapter_with_card
        # First tool: intermediate send — card stays open.
        r1 = await a.send("chat-1", "💻 tool1")
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
                "card_content_key": "content",
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
        from gateway.platforms.dingtalk import DingTalkAdapter

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
        stream_request = mock_card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert stream_request.key == "content"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_card_content_key_reloads_dashboard_config(self, mock_stream_client):
        from gateway.platforms.dingtalk import DingTalkAdapter

        config_path = os.path.join(os.environ["HERMES_HOME"], "config.yaml")
        adapter = DingTalkAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "client_id": "test_id",
                    "client_secret": "test_secret",
                    "card_template_id": "test_card_template",
                },
            )
        )
        adapter._stream_client = mock_stream_client

        mock_card_sdk = MagicMock()
        mock_card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._card_sdk = mock_card_sdk

        with open(config_path, "w", encoding="utf-8") as f:
            f.write("dingtalk:\n  card_content_key: content\n")
        await adapter._stream_card_content("track-1", "token", "Hello")

        with open(config_path, "w", encoding="utf-8") as f:
            f.write("dingtalk:\n  card_content_key: ''\n")
        await adapter._stream_card_content("track-1", "token", "Hello")

        calls = mock_card_sdk.streaming_update_with_options_async.call_args_list
        assert calls[0].args[0].key == "content"
        assert calls[1].args[0].key == "msgContent"

    @pytest.mark.asyncio
    async def test_blank_template_uses_default_ai_card_shape(self, mock_stream_client, mock_http_client, mock_message):
        from gateway.platforms.dingtalk import (
            DEFAULT_AI_CARD_TEMPLATE_ID,
            DingTalkAdapter,
        )

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._stream_client = mock_stream_client
        adapter._http_client = mock_http_client
        adapter._message_contexts["test_conv_id"] = mock_message
        adapter._session_webhooks = {
            "test_conv_id": (
                "https://api.dingtalk.com/robot/sendBySession?session=test",
                9999999999999,
            )
        }

        mock_card_sdk = MagicMock()
        mock_card_sdk.create_card_with_options_async = AsyncMock()
        mock_card_sdk.deliver_card_with_options_async = AsyncMock()
        mock_card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._card_sdk = mock_card_sdk
        adapter._get_access_token = AsyncMock(return_value="test_token")

        result = await adapter.send("test_conv_id", "Hello World")

        assert result.success is True
        create_request = mock_card_sdk.create_card_with_options_async.call_args[0][0]
        assert create_request.card_template_id == DEFAULT_AI_CARD_TEMPLATE_ID
        assert "msgContent" in create_request.card_data.card_param_map
        assert "content" not in create_request.card_data.card_param_map
        stream_request = mock_card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert stream_request.key == "msgContent"

    @pytest.mark.asyncio
    async def test_ai_card_at_uses_structured_fields_without_content_prefix(
        self,
        config,
        mock_stream_client,
        mock_http_client,
    ):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(config)
        adapter._stream_client = mock_stream_client
        adapter._http_client = mock_http_client

        group_message = MagicMock(
            message_id="test_msg_id",
            conversation_id="group_conv_id",
            conversation_type="2",
            sender_staff_id="staff-1",
            sender_nick="Alice",
        )

        mock_card_sdk = MagicMock()
        mock_card_sdk.create_card_with_options_async = AsyncMock()
        mock_card_sdk.deliver_card_with_options_async = AsyncMock()
        mock_card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._card_sdk = mock_card_sdk
        adapter._get_access_token = AsyncMock(return_value="test_token")

        result = await adapter._create_and_stream_card(
            "group_conv_id",
            group_message,
            "Hello World",
            at_users={"staff-1": "Alice"},
        )

        assert result.success is True
        create_request = mock_card_sdk.create_card_with_options_async.call_args[0][0]
        assert create_request.card_at_user_ids == ["staff-1"]
        deliver_request = mock_card_sdk.deliver_card_with_options_async.call_args[0][0]
        assert deliver_request.im_group_open_deliver_model.at_user_ids == {
            "staff-1": "Alice",
        }
        stream_request = mock_card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert stream_request.content == "Hello World"

    @pytest.mark.asyncio
    async def test_group_reply_with_at_sender_uses_ai_card_user_mentions(
        self,
        mock_stream_client,
        mock_http_client,
    ):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "card_template_id": "test_card_template",
                    "reply_at_sender": True,
                },
            )
        )
        adapter._stream_client = mock_stream_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_http_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_http_client
        group_message = MagicMock(
            message_id="test_msg_id",
            conversation_id="group_conv_id",
            conversation_type="2",
            sender_staff_id="staff-1",
            sender_nick="Alice",
        )
        adapter._message_contexts["group_conv_id"] = group_message
        adapter._session_webhooks = {
            "group_conv_id": (
                "https://api.dingtalk.com/robot/sendBySession?session=test",
                9999999999999,
            )
        }

        mock_card_sdk = MagicMock()
        mock_card_sdk.create_card_with_options_async = AsyncMock()
        mock_card_sdk.deliver_card_with_options_async = AsyncMock()
        mock_card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._card_sdk = mock_card_sdk
        adapter._get_access_token = AsyncMock(return_value="test_token")

        result = await adapter.send("group_conv_id", "Hello World", reply_to="test_msg_id")

        assert result.success is True
        mock_card_sdk.create_card_with_options_async.assert_called_once()
        mock_card_sdk.deliver_card_with_options_async.assert_called_once()
        mock_http_client.post.assert_not_called()
        create_request = mock_card_sdk.create_card_with_options_async.call_args[0][0]
        assert create_request.card_at_user_ids == ["staff-1"]
        deliver_request = mock_card_sdk.deliver_card_with_options_async.call_args[0][0]
        assert deliver_request.im_group_open_deliver_model.at_user_ids == {
            "staff-1": "Alice",
        }
        stream_request = mock_card_sdk.streaming_update_with_options_async.call_args[0][0]
        assert stream_request.content == "Hello World"
