"""Tests for DingTalk platform adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig

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
            # Robot-native proactive message models (webhook fallback + media sends)
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

# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

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
        assert payload["markdown"]["text"] == "Hello!"

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
        from unittest.mock import AsyncMock
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

    def test_richtext_reset_does_not_clobber_voice(self):
        """A richText envelope containing a native voice item must stay
        VOICE — the ``msg_type_str == "richText"`` re-derivation used to
        reset it to TEXT, dropping the voice note from the STT path
        (#38211, #38219)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.event import MessageType

        msg = self._msg_with_rich_text(
            [{"type": "voice", "downloadCode": "dl_voice_rt"}]
        )
        msg.message_type = "richText"
        msg_type, urls, mtypes = DingTalkAdapter._extract_media(
            DingTalkAdapter, msg
        )
        assert msg_type == MessageType.VOICE
        assert urls == ["dl_voice_rt"]
        assert mtypes == ["audio"]

    def test_image_no_filename_still_photo(self):
        """msgtype='image' without fileName → still PHOTO (MIME heuristic)."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from gateway.platforms.event import MessageType

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
# Per-attachment media-error surfacing
# ---------------------------------------------------------------------------


class TestMediaErrorPropagation:
    """``_resolve_media_codes`` attaches per-attachment resolution failures via
    ``_set_media_error``; ``_on_message`` collects them via
    ``_extract_media_errors`` and surfaces them on the ``MessageEvent`` so the
    agent can tell the user a referenced image/audio could not be retrieved."""

    def test_on_message_preserves_media_errors(self, monkeypatch):
        from gateway.platforms.event import MessageType
        from unittest.mock import AsyncMock

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

        import asyncio
        asyncio.get_event_loop().run_until_complete(adapter._on_message(msg))

        event = adapter.handle_message.await_args.args[0]
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == []
        assert event.media_errors == [
            "DingTalk media download failed: robot SDK is unavailable."
        ]

    def test_set_media_error_on_dict_item(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        item = {"downloadCode": "dl1"}
        DingTalkAdapter._set_media_error(item, "boom")
        assert DingTalkAdapter._media_error_for_item(item) == "boom"

    def test_set_media_error_on_object_item(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        item = type("X", (), {})()
        DingTalkAdapter._set_media_error(item, "kaboom")
        assert DingTalkAdapter._media_error_for_item(item) == "kaboom"

    def test_media_get_dict_vs_attr(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._media_get({"k": "v"}, "k") == "v"
        assert DingTalkAdapter._media_get({"k": "v"}, "missing", "dflt") == "dflt"
        obj = type("X", (), {"k": "v"})()
        assert DingTalkAdapter._media_get(obj, "k") == "v"

    def test_first_media_ref_finds_code_then_url(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        ref, key, is_code = DingTalkAdapter._first_media_ref(
            {"downloadCode": "dl1", "downloadUrl": "http://x/y.jpg"}
        )
        assert ref == "dl1" and is_code is True
        ref, key, is_code = DingTalkAdapter._first_media_ref(
            {"downloadUrl": "http://x/y.jpg"}
        )
        assert ref == "http://x/y.jpg" and is_code is False
        assert DingTalkAdapter._first_media_ref({}) == (None, None, False)

    def test_default_media_type_and_extension(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._default_media_type("image") == "image/jpeg"
        assert DingTalkAdapter._default_media_type("audio") == "audio/ogg"
        assert DingTalkAdapter._default_media_type("video") == "video/mp4"
        assert DingTalkAdapter._default_media_type("file") == "application/octet-stream"
        assert DingTalkAdapter._default_media_type("image", "photo.png") == "image/png"
        assert DingTalkAdapter._extension_for_media("image") == ".jpg"
        assert DingTalkAdapter._extension_for_media("audio") == ".ogg"
        assert DingTalkAdapter._extension_for_media("video") == ".mp4"
        assert DingTalkAdapter._extension_for_media("image", "image/jpeg") == ".jpg"
        assert DingTalkAdapter._extension_for_media("image", None, "pic.PNG") == ".PNG"

    def test_extract_media_errors_collects_from_image_and_rich(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = _make_gating_adapter.__wrapped__ if hasattr(_make_gating_adapter, "__wrapped__") else _make_gating_adapter
        # Build a minimal adapter-like object: _extract_media_errors is an instance method
        # but only uses self._media_error_for_item (classmethod) and _rich_list (module-level),
        # so a bare instance works for this test.
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter as DTA
        inst = DTA.__new__(DTA)
        msg = type("M", (), {})()
        msg.image_content = {"_hermes_media_error": "img-fail"}
        msg.rich_text = [
            {"downloadCode": "dl1", "_hermes_media_error": "rich-fail-1"},
            {"downloadCode": "dl2"},
            {"_hermes_media_error": "rich-fail-2"},
        ]
        msg.rich_text_content = None
        errors = inst._extract_media_errors(msg)
        assert errors == ["img-fail", "rich-fail-1", "rich-fail-2"]


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


class TestRichMediaAndReactions:
    """Tests for the fork-ported rich-media send / emotion / status-card methods."""

    def test_extract_emotion_tags_strips_and_collects(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        content, tags = DingTalkAdapter._extract_emotion_tags(
            "Hello [[emotion:thumbsup]] world [[dingtalk:emotion=fire]]"
        )
        assert "thumbsup" in tags
        assert "fire" in tags
        assert "[[emotion:" not in content
        assert "[[dingtalk:" not in content

    def test_extract_emotion_tags_empty_content(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._extract_emotion_tags("") == ("", [])

    def test_prepend_mention_tokens_no_payload(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._prepend_mention_tokens("hello", None) == "hello"

    def test_prepend_mention_tokens_with_at_all(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        result = DingTalkAdapter._prepend_mention_tokens("hello", {"isAtAll": True})
        assert "@所有人" in result
        assert "hello" in result

    def test_prepend_mention_tokens_with_mobiles_and_users(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        result = DingTalkAdapter._prepend_mention_tokens(
            "hello", {"atMobiles": ["13800138000"], "atUserIds": ["staff-1"]}
        )
        assert "@13800138000" in result
        assert "@staff-1" in result
        assert "hello" in result

    def test_prepend_mention_tokens_dedupes(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        result = DingTalkAdapter._prepend_mention_tokens(
            "hello", {"atMobiles": ["123", "123"]}
        )
        assert result.count("@123") == 1

    def test_build_webhook_at_payload_empty(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter._build_webhook_at_payload({}, {}) is None

    def test_build_webhook_at_payload_with_at_all(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        payload = adapter._build_webhook_at_payload(
            {"dingtalk_at_all": "true"}, {}
        )
        assert payload is not None
        assert payload["isAtAll"] is True

    def test_build_webhook_at_payload_with_mobiles(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        payload = adapter._build_webhook_at_payload(
            {"dingtalk_at_mobiles": "13800138000,13900139000"}, {}
        )
        assert payload is not None
        assert "13800138000" in payload["atMobiles"]
        assert "13900139000" in payload["atMobiles"]

    def test_stage_label_for_tool_terminal_with_git(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        label = DingTalkAdapter._stage_label_for_tool("terminal", "git commit -m hello")
        assert label == "🌳 提交代码中"

    def test_stage_label_for_tool_unknown_returns_none(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._stage_label_for_tool("nonexistent_tool") is None

    def test_stage_label_for_tool_none_name(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._stage_label_for_tool(None) is None

    def test_stage_label_for_tool_read_file(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        label = DingTalkAdapter._stage_label_for_tool("read_file")
        assert label == "👀 看文件中"

    def test_read_bool_setting_env_fallback(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        monkeypatch.setenv("TEST_BOOL_SETTING", "yes")
        assert DingTalkAdapter._read_bool_setting(
            None, env_name="TEST_BOOL_SETTING", default=False
        ) is True

    def test_read_bool_setting_default(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        monkeypatch.delenv("TEST_BOOL_DEFAULT", raising=False)
        assert DingTalkAdapter._read_bool_setting(
            None, env_name="TEST_BOOL_DEFAULT", default=True
        ) is True

    def test_metadata_values_list_and_csv(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        values = DingTalkAdapter._metadata_values(
            {"a": ["x", "y"], "b": "z,w"}, "a", "b"
        )
        assert "x" in values
        assert "y" in values
        assert "z" in values
        assert "w" in values

    def test_metadata_bool_false(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._metadata_bool({"x": "false"}, "x") is False
        assert DingTalkAdapter._metadata_bool({"x": "true"}, "x") is True
        assert DingTalkAdapter._metadata_bool({}, "x") is False

    def test_image_card_param_map(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        m = DingTalkAdapter._image_card_param_map("media123", "caption")
        assert m["msgTitle"] == "Hermes"
        assert m["msgContent"] == "caption"
        assert m["staticMsgContent"] == "caption"
        import json
        sys_obj = json.loads(m["sys_full_json_obj"])
        assert "media123" in sys_obj["msgImages"]

    def test_card_initial_param_map_custom_template(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"card_template_id": "custom-tmpl"})
        )
        m = adapter._card_initial_param_map()
        # Custom template: just the content key with empty string
        content_key = adapter._current_card_content_key()
        assert m == {content_key: ""}

    def test_card_initial_param_map_default_template(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        m = adapter._card_initial_param_map()
        assert "msgContent" in m
        assert "staticMsgContent" in m
        assert "flowStatus" in m
        assert m["flowStatus"] == "1"

    def test_current_card_content_key_default(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter._current_card_content_key() == "msgContent"

    def test_current_card_content_key_override(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(
            PlatformConfig(enabled=True, extra={"card_content_key": "customKey"})
        )
        assert adapter._current_card_content_key() == "customKey"

    def test_looks_like_mp4_valid(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        p = tmp_path / "test.mp4"
        p.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 20)
        assert DingTalkAdapter._looks_like_mp4(p) is True

    def test_looks_like_mp4_invalid(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        p = tmp_path / "test.txt"
        p.write_bytes(b"hello world")
        assert DingTalkAdapter._looks_like_mp4(p) is False

    def test_looks_like_native_audio_ogg(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        p = tmp_path / "test.ogg"
        p.write_bytes(b"OggS" + b"\x00" * 12)
        assert DingTalkAdapter._looks_like_native_audio(p, "ogg") is True

    def test_looks_like_native_audio_amr(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        p = tmp_path / "test.amr"
        p.write_bytes(b"#!AMR\n")
        assert DingTalkAdapter._looks_like_native_audio(p, "amr") is True

    def test_looks_like_native_audio_wrong(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        p = tmp_path / "test.ogg"
        p.write_bytes(b"NOT OGG")
        assert DingTalkAdapter._looks_like_native_audio(p, "ogg") is False

    def test_duration_ms_from_metadata_ms(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._duration_ms_from_metadata({"duration_ms": 5000}) == 5000

    def test_duration_ms_from_metadata_seconds(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._duration_ms_from_metadata({"duration": 2}) == 2000

    def test_duration_ms_from_metadata_none(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        assert DingTalkAdapter._duration_ms_from_metadata({}) is None

    def test_set_pending_reply_state_default_to_success(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter.set_pending_reply_state("chat-1", "unknown")
        assert adapter._pending_reply_state["chat-1"] == "success"

    def test_set_pending_reply_state_error(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter.set_pending_reply_state("chat-1", "error")
        assert adapter._pending_reply_state["chat-1"] == "error"

    def test_set_pending_reply_state_empty_chat_ignored(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter.set_pending_reply_state("", "error")
        assert "" not in adapter._pending_reply_state


class TestSendExecApproval:
    """Test the dangerous-command approval card."""

    @pytest.mark.asyncio
    async def test_send_exec_approval_calls_send(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._http_client = AsyncMock()
        adapter._get_valid_webhook = lambda chat_id: ("https://api.dingtalk.com/x", 9999999999999)
        adapter._http_client.post = AsyncMock(
            return_value=SimpleNamespace(status_code=200, text="ok")
        )
        result = await adapter.send_exec_approval(
            "chat-1", "rm -rf /", "session-1", "test"
        )
        assert result.success
        # The command should appear in the sent payload
        call_args = adapter._http_client.post.call_args
        payload = call_args[1]["json"]
        assert "rm -rf /" in payload["markdown"]["text"]


class TestSendVideoVoiceFallback:
    """send_video / send_voice fall back to send_document when local files don't exist or aren't native format."""

    @pytest.mark.asyncio
    async def test_send_video_missing_file_returns_error(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter.send_video("chat-1", "/nonexistent/video.mp4")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_voice_missing_file_returns_error(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter.send_voice("chat-1", "/nonexistent/audio.ogg")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_video_remote_url_delegates_to_send(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._http_client = AsyncMock()
        adapter._get_valid_webhook = lambda chat_id: ("https://api.dingtalk.com/x", 9999999999999)
        adapter._http_client.post = AsyncMock(
            return_value=SimpleNamespace(status_code=200, text="ok")
        )
        result = await adapter.send_video(
            "chat-1", "https://example.com/video.mp4", caption="look"
        )
        assert result.success


class TestRobotNativeMessage:
    """Test _send_robot_native_message routing logic."""

    @pytest.mark.asyncio
    async def test_robot_native_group_send(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        import plugins.platforms.dingtalk.adapter as dt
        from types import SimpleNamespace
        # Inline mock of robot SDK models so the test is hermetic.
        _FakeModel = type("_FakeModel", (), {"__init__": lambda self, **kw: None})
        monkeypatch.setattr(dt, "dingtalk_robot_models", SimpleNamespace(
            OrgGroupSendRequest=_FakeModel,
            OrgGroupSendHeaders=_FakeModel,
        ), raising=False)
        monkeypatch.setattr(dt, "tea_util_models", SimpleNamespace(RuntimeOptions=_FakeModel), raising=False)
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._robot_sdk = MagicMock()
        adapter._robot_sdk.org_group_send_with_options_async = AsyncMock(
            return_value=SimpleNamespace(body=SimpleNamespace(process_query_key="qkey-1"))
        )
        adapter._get_access_token = AsyncMock(return_value="token")
        msg = SimpleNamespace(
            conversation_id="cid-1",
            conversation_type="2",
            robot_code="robot-1",
            sender_staff_id="",
        )
        adapter._message_contexts["chat-1"] = msg
        result = await adapter._send_robot_native_message(
            "chat-1", "sampleText", {"content": "hello"}
        )
        assert result.success
        assert result.message_id == "qkey-1"
        adapter._robot_sdk.org_group_send_with_options_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_robot_native_no_robot_sdk(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._robot_sdk = None
        result = await adapter._send_robot_native_message(
            "chat-1", "sampleText", {"content": "hello"}
        )
        assert not result.success
        assert "unavailable" in result.error.lower()


class TestProactiveMarkdownFallback:
    """Test _send_markdown_proactive: AI Card first, then robot-native."""

    @pytest.mark.asyncio
    async def test_proactive_card_success(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        import plugins.platforms.dingtalk.adapter as dt
        from types import SimpleNamespace
        _FakeModel = type("_FakeModel", (), {"__init__": lambda self, **kw: None})
        monkeypatch.setattr(dt, "dingtalk_card_models", SimpleNamespace(
            CreateCardRequest=_FakeModel,
            CreateCardRequestCardData=_FakeModel,
            CreateCardRequestImGroupOpenSpaceModel=_FakeModel,
            CreateCardRequestImRobotOpenSpaceModel=_FakeModel,
            CreateCardHeaders=_FakeModel,
            DeliverCardRequest=_FakeModel,
            DeliverCardRequestImGroupOpenDeliverModel=_FakeModel,
            DeliverCardRequestImRobotOpenDeliverModel=_FakeModel,
            DeliverCardHeaders=_FakeModel,
            StreamingUpdateRequest=_FakeModel,
            StreamingUpdateHeaders=_FakeModel,
        ), raising=False)
        monkeypatch.setattr(dt, "tea_util_models", SimpleNamespace(RuntimeOptions=_FakeModel), raising=False)
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"card_template_id": "tmpl-1"}))
        adapter._card_sdk = MagicMock()
        adapter._card_sdk.create_card_with_options_async = AsyncMock()
        adapter._card_sdk.deliver_card_with_options_async = AsyncMock()
        adapter._card_sdk.streaming_update_with_options_async = AsyncMock()
        adapter._get_access_token = AsyncMock(return_value="token")
        result = await adapter._send_markdown_proactive("chat-1", "hello world")
        assert result.success
        adapter._card_sdk.create_card_with_options_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proactive_empty_content(self, monkeypatch):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter._send_markdown_proactive("chat-1", "   ")
        assert not result.success
        assert "empty" in result.error.lower()


class TestUploadRobotMedia:
    """Test _upload_robot_media file validation."""

    @pytest.mark.asyncio
    async def test_upload_missing_file(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter._upload_robot_media("/nonexistent/file.png", "image")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_upload_unsupported_type(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        p = tmp_path / "file.xyz"
        p.write_bytes(b"data")
        result = await adapter._upload_robot_media(str(p), "document")
        assert not result.success
        assert "unsupported" in result.error.lower()

    @pytest.mark.asyncio
    async def test_upload_no_http_client(self, tmp_path):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        p = tmp_path / "file.png"
        p.write_bytes(b"data")
        result = await adapter._upload_robot_media(str(p), "image")
        assert not result.success


class TestSendImageFileUpload:
    """Test send_image_file remote URL delegation and local file upload path."""

    @pytest.mark.asyncio
    async def test_send_image_file_remote_delegates_to_send_image(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._http_client = AsyncMock()
        adapter._get_valid_webhook = lambda chat_id: ("https://api.dingtalk.com/x", 9999999999999)
        adapter._http_client.post = AsyncMock(
            return_value=SimpleNamespace(status_code=200, text="ok")
        )
        result = await adapter.send_image_file(
            "chat-1", "https://example.com/image.png", caption="look"
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_send_image_file_missing_file(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter.send_image_file("chat-1", "/nonexistent/img.png")
        assert not result.success


class TestSendDocumentUpload:
    """Test send_document now uses robot media upload."""

    @pytest.mark.asyncio
    async def test_send_document_missing_file(self):
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
        result = await adapter.send_document("chat-1", "/nonexistent/doc.pdf")
        assert not result.success
        assert "not found" in result.error.lower()


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

# ---------------------------------------------------------------------------
# Card lifecycle: finalize via metadata["streaming"]
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
    async def test_intermediate_send_stays_streaming(self, adapter_with_card):
        """send(metadata={"expect_edits": True}) creates an OPEN card (tool progress /
        commentary / streaming first chunk).  No flicker closed→streaming
        when edit_message follows."""
        a = adapter_with_card
        result = await a.send("chat-1", "💻 terminal: ls", metadata={"expect_edits": True})
        assert result.success
        call = a._card_sdk.streaming_update_with_options_async.call_args
        assert call[0][0].is_finalize is False
        # Tracked for sibling cleanup.
        assert result.message_id in a._streaming_cards.get("chat-1", {})

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
    @pytest.mark.parametrize("missing", ["template", "sdk"])
    async def test_edit_message_rejects_when_ai_cards_are_unavailable(self, missing):
        """Edits must not access the card SDK on webhook-only installations."""
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        extra = {} if missing == "template" else {"card_template_id": "tmpl-1"}
        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra=extra))
        # With DEFAULT_AI_CARD_TEMPLATE_ID, a missing template still enables cards;
        # the hard gate is now _card_sdk being None (SDK not initialized on connect()).
        card_sdk = None if missing in ("sdk", "template") else MagicMock()
        adapter._card_sdk = card_sdk
        adapter._get_access_token = AsyncMock(return_value="token")

        result = await adapter.edit_message("chat-1", "track-1", "heartbeat")

        assert not result.success
        adapter._get_access_token.assert_not_awaited()
        if card_sdk is not None:
            assert card_sdk.mock_calls == []

    @pytest.mark.asyncio
    async def test_edit_message_streams_when_ai_cards_are_configured(self, adapter_with_card):
        """Configured cards retain the streaming edit path."""
        result = await adapter_with_card.edit_message("chat-1", "track-1", "heartbeat")

        assert result.success
        assert result.message_id == "track-1"
        adapter_with_card._card_sdk.streaming_update_with_options_async.assert_awaited_once()

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


class TestImageResend:
    """Test the recent-image resend shortcut."""

    def test_wants_recent_image_resend_chinese(self):
        from gateway.run_turn import _wants_recent_image_resend
        assert _wants_recent_image_resend("把刚才的图片重新发一下") is True
        assert _wants_recent_image_resend("重新发图片") is True

    def test_wants_recent_image_resend_english(self):
        from gateway.run_turn import _wants_recent_image_resend
        assert _wants_recent_image_resend("resend the last image") is True
        assert _wants_recent_image_resend("send the latest image") is True

    def test_wants_recent_image_resend_no_match(self):
        from gateway.run_turn import _wants_recent_image_resend
        assert _wants_recent_image_resend("what is the weather") is False
        assert _wants_recent_image_resend("") is False
        assert _wants_recent_image_resend(None) is False

    def test_find_latest_attached_image_path(self, tmp_path):
        from gateway.run_turn import _find_latest_attached_image_path
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG")
        messages = [
            {"role": "assistant", "content": "Here is the image [Image attached at: /nonexistent/old.png]"},
            {"role": "assistant", "content": f"Latest [Image attached at: {img}]"},
        ]
        path = _find_latest_attached_image_path(messages)
        assert path == str(img)

    def test_find_latest_attached_image_path_none_when_no_image(self):
        from gateway.run_turn import _find_latest_attached_image_path
        messages = [{"role": "assistant", "content": "No images here"}]
        assert _find_latest_attached_image_path(messages) is None
        assert _find_latest_attached_image_path([]) is None
