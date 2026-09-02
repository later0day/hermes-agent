"""
DingTalk platform adapter using Stream Mode.

Uses dingtalk-stream SDK (>=0.20) for real-time message reception without webhooks.
Responses are sent via DingTalk AI Cards when the card SDK is available,
otherwise via DingTalk's session webhook (markdown format).
Supports: text, images, audio, video, rich text, files, and group @mentions.

Requires:
    pip install "dingtalk-stream>=0.20" httpx
    DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET env vars

Configuration in config.yaml:
    platforms:
      dingtalk:
        enabled: true
        # Optional group-chat gating (mirrors Slack/Telegram/Discord):
        require_mention: true            # or DINGTALK_REQUIRE_MENTION env var
        # free_response_chats:           # conversations that skip require_mention
        #   - cidABC==
        # mention_patterns:              # regex wake-words (e.g. Chinese bot names)
        #   - "^小马"
        # allowed_users:                 # staff_id or sender_id list; "*" = any
        #   - "manager1234"
        # reply_at_sender: true          # @ sender on final group replies
        extra:
          client_id: "your-app-key"      # or DINGTALK_CLIENT_ID env var
          client_secret: "your-secret"   # or DINGTALK_CLIENT_SECRET env var
"""

import asyncio
import concurrent.futures
import json
import logging
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import tempfile
import traceback
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import CallbackMessage, AckMessage

    DINGTALK_STREAM_AVAILABLE = True
except Exception:  # noqa: BLE001 — broad: optional SDK's transitive deps (cryptography) may raise non-ImportError; degrade gracefully (#41112)
    DINGTALK_STREAM_AVAILABLE = False
    dingtalk_stream = None  # type: ignore[assignment]
    ChatbotMessage = None  # type: ignore[assignment]
    CallbackMessage = None  # type: ignore[assignment]
    AckMessage = type(
        "AckMessage",
        (),
        {
            "STATUS_OK": 200,
            "STATUS_SYSTEM_EXCEPTION": 500,
        },
    )  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

# Card SDK for AI Cards (following QwenPaw pattern).
# Catch broad Exception, not just ImportError: the alibabacloud_dingtalk SDK
# transitively imports cryptography and can raise AttributeError (not
# ImportError) when the installed cryptography version skews from what the SDK
# expects (e.g. `cryptography.utils.DeprecatedIn46` missing on older
# cryptography). An optional SDK with a broken dependency chain must degrade
# gracefully — same as a missing one — rather than crash the whole adapter
# (and therefore the whole plugin) import. #41112.
try:
    from alibabacloud_dingtalk.card_1_0 import (
        client as dingtalk_card_client,
        models as dingtalk_card_models,
    )
    from alibabacloud_dingtalk.robot_1_0 import (
        client as dingtalk_robot_client,
        models as dingtalk_robot_models,
    )
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    CARD_SDK_AVAILABLE = True
except Exception:
    CARD_SDK_AVAILABLE = False
    dingtalk_card_client = None
    dingtalk_card_models = None
    dingtalk_robot_client = None
    dingtalk_robot_models = None
    open_api_models = None
    tea_util_models = None

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator, compile_mention_patterns
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _ssrf_redirect_guard,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    safe_url_for_log,
)
from hermes_cli.config import load_config_readonly

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# Stream liveness watchdog. DingTalk Stream Mode connections can silently go
# "half-open": the TCP socket stays established and the SDK's ``async for
# raw_message in websocket`` blocks forever waiting for business frames that
# will never arrive, while ``start()`` neither returns nor raises — so the
# adapter's own reconnect loop (``_run_stream``) never gets control. Observed
# in production 2026-08-12: a connection went silent for 4.5 days with zero
# exceptions until a manual restart. The fix is an application-layer watchdog
# that actively pings the live websocket and force-closes it if the pong does
# not return within a timeout, which breaks the SDK's inner loop and triggers
# its (and our) reconnect path with a fresh ticket. A quiet-but-healthy
# connection returns the pong promptly, so this never churns a live socket.
STREAM_PING_INTERVAL = 60  # seconds between liveness pings
STREAM_PING_TIMEOUT = 20  # seconds to await the pong before declaring half-open


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive int from env, falling back to ``default`` on any error."""
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


_SESSION_WEBHOOKS_MAX = 500
_DINGTALK_WEBHOOK_RE = re.compile(r'^https://(?:api|oapi)\.dingtalk\.com/')
_DINGTALK_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
_DINGTALK_NATIVE_AUDIO_EXTS = {"ogg", "amr"}
_DINGTALK_NATIVE_VIDEO_EXTS = {"mp4"}
DEFAULT_AI_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"
DEFAULT_AI_CARD_CONTENT_KEY = "msgContent"
_DINGTALK_EMOTION_TAG_RE = re.compile(
    r"\[\[(?:dingtalk[:_-])?emotion\s*[:=]\s*([^\]]+?)\s*\]\]",
    re.IGNORECASE,
)

# DingTalk message type → runtime content type
DINGTALK_TYPE_MAPPING = {
    "audio": "audio",
    "document": "file",
    "file": "file",
    "image": "image",
    "picture": "image",
    "video": "video",
    "voice": "audio",
}

# File extension → MIME type mapping for DingTalk file/image messages.
# Image MIME types (image/*) are used below in _extract_media to classify
# incoming msgtype='image' payloads as MessageType.PHOTO (not DOCUMENT).
EXT_MAP = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "zip": "application/zip",
    "mp4": "video/mp4",
}


def _dingtalk_ipv4_preference_enabled() -> bool:
    """Return True when Hermes' global IPv4 preference patch is active."""
    try:
        import socket

        return bool(getattr(socket.getaddrinfo, "_hermes_ipv4_patched", False))
    except Exception:
        return False


def _dingtalk_http_client_kwargs(timeout: float) -> Dict[str, Any]:
    from gateway.platforms._http_client_limits import platform_httpx_limits

    limits = platform_httpx_limits()
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if _dingtalk_ipv4_preference_enabled() and httpx is not None:
        kwargs["transport"] = httpx.AsyncHTTPTransport(
            limits=limits,
            local_address="0.0.0.0",
        )
    else:
        kwargs["limits"] = limits
    return kwargs


def dingtalk_deps_present() -> bool:
    """PASSIVE probe: are dingtalk-stream/httpx importable right now?

    Registry ``check_fn`` — called from status displays and config loading,
    so it must never install anything.  The ACTIVE lazy-installer
    (``check_dingtalk_requirements``) is registered as ``ensure_deps_fn``
    and runs from ``create_adapter()`` when this returns False (#79812).
    Credentials are gated separately via ``is_connected``/``validate_config``.
    """
    return DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE


def ensure_dingtalk_deps() -> bool:
    """ACTIVE deps-only installer (registry ``ensure_deps_fn``).

    Lazy-installs dingtalk-stream/httpx and rebinds module globals.
    Deliberately does NOT check credentials — ``ensure_deps_fn``'s contract
    is deps-only ("Returns True once deps are importable"); credentials are
    gated by ``is_connected``/``validate_config``.  Otherwise a platform
    configured via ``PlatformConfig.extra`` (which ``_is_connected``
    accepts) would pass enablement, reach ``create_adapter()``, and have
    the installer veto on env-var grounds before ever installing —
    re-creating the #79812 deadlock for extra-configured setups.
    """
    global DINGTALK_STREAM_AVAILABLE, dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage
    global HTTPX_AVAILABLE, httpx
    if DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.dingtalk", prompt=False)
    except Exception:
        return False
    try:
        import dingtalk_stream as _ds
        from dingtalk_stream import ChatbotMessage as _CM
        from dingtalk_stream.frames import CallbackMessage as _CBM, AckMessage as _AM
        import httpx as _httpx
    except Exception:
        return False
    dingtalk_stream = _ds
    ChatbotMessage = _CM
    CallbackMessage = _CBM
    AckMessage = _AM
    httpx = _httpx
    DINGTALK_STREAM_AVAILABLE = True
    HTTPX_AVAILABLE = True
    return True


def check_dingtalk_requirements() -> bool:
    """Check if DingTalk dependencies are available and configured.

    Lazy-installs dingtalk-stream via :func:`ensure_dingtalk_deps`, then
    additionally requires credentials.  Kept for setup/status callers that
    want the combined deps+credentials answer; the registry uses the
    deps-only :func:`ensure_dingtalk_deps` as ``ensure_deps_fn``.
    """
    if not ensure_dingtalk_deps():
        return False
    if not os.getenv("DINGTALK_CLIENT_ID") or not _get_scoped_secret("DINGTALK_CLIENT_SECRET"):
        return False
    return True


class DingTalkAdapter(BasePlatformAdapter):
    """DingTalk chatbot adapter using Stream Mode.

    The dingtalk-stream SDK maintains a long-lived WebSocket connection.
    Incoming messages arrive via a ChatbotHandler callback. Replies are
    sent via the incoming message's session_webhook URL using httpx.

    Features:
    - Text messages (plain + rich text)
    - Images, audio, video, files (via download codes)
    - Group chat @mention detection
    - Session webhook caching with expiry tracking
    - Markdown formatted replies
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    @property
    def SUPPORTS_MESSAGE_EDITING(self) -> bool:  # noqa: N802
        """Edits only meaningful when AI Cards are configured.

        The gateway gates streaming cursor + edit behaviour on this flag,
        so we must reflect the actual adapter capability at runtime.
        """
        return bool(self._card_template_id and self._card_sdk)

    @property
    def REQUIRES_EDIT_FINALIZE(self) -> bool:  # noqa: N802
        """AI Card lifecycle requires an explicit ``finalize=True`` edit
        to close the streaming indicator, even when the final content is
        identical to the last streamed update.  Enabled only when cards
        are configured — webhook-only DingTalk doesn't need it.
        """
        return bool(self._card_template_id and self._card_sdk)

    @property
    def SUPPORTS_TURN_STATUS_CARD(self) -> bool:  # noqa: N802
        """DingTalk AI Cards can keep one editable progress/status card per turn."""
        return bool(self._card_template_id and self._card_sdk)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)

        extra = config.extra or {}
        self._client_id: str = extra.get("client_id") or os.getenv(
            "DINGTALK_CLIENT_ID", ""
        )
        self._client_secret: str = extra.get("client_secret") or _get_scoped_secret(
            "DINGTALK_CLIENT_SECRET", ""
        )

        # Group-chat gating (mirrors Slack/Telegram/Discord/WhatsApp conventions).
        # Mention state is the structured ``is_in_at_list`` attribute from the
        # dingtalk-stream SDK (set from the callback's ``isInAtList`` flag),
        # not text parsing.
        self._mention_patterns: List[re.Pattern] = self._compile_mention_patterns()
        self._allowed_users: Set[str] = self._load_allowed_users()

        self._stream_client: Any = None
        self._stream_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        # Liveness watchdog tuning (env-overridable for ops without a redeploy).
        self._ping_interval: int = _env_int(
            "DINGTALK_STREAM_PING_INTERVAL", STREAM_PING_INTERVAL
        )
        self._ping_timeout: int = _env_int(
            "DINGTALK_STREAM_PING_TIMEOUT", STREAM_PING_TIMEOUT
        )
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._card_sdk: Optional[Any] = None
        self._robot_sdk: Optional[Any] = None
        self._robot_code: str = (
            extra.get("robot_code")
            or os.getenv("DINGTALK_ROBOT_CODE", "")
            or self._client_id
        )
        self._app_code: str = extra.get("app_code", "")
        self._corp_id: str = extra.get("corp_id", "")
        self._agent_id: str = extra.get("agent_id", "")
        self._reply_at_sender: bool = self._read_bool_setting(
            extra.get("reply_at_sender"),
            env_name="DINGTALK_REPLY_AT_SENDER",
            default=False,
        )

        # Message deduplication
        self._dedup = MessageDeduplicator(max_size=1000)
        # Map chat_id -> (session_webhook, expired_time_ms) for reply routing
        self._session_webhooks: Dict[str, tuple[str, int]] = {}
        # Map chat_id -> last inbound ChatbotMessage. Keyed by chat_id instead
        # of a single class attribute to avoid cross-message clobbering when
        # multiple conversations run concurrently.
        self._message_contexts: Dict[str, Any] = {}
        configured_template = str(extra.get("card_template_id") or "").strip()
        self._card_template_id: Optional[str] = (
            configured_template or DEFAULT_AI_CARD_TEMPLATE_ID
        )
        self._card_uses_default_template = not configured_template
        self._card_content_key_override = str(extra.get("card_content_key") or "").strip()
        self._card_content_key = self._current_card_content_key()

        # Chats for which we've already fired the final reaction — prevents
        # double-firing across segment boundaries or parallel flows
        # (tool-progress + stream-consumer both finalizing their cards).
        # Reset each inbound message.
        self._done_emoji_fired: Set[str] = set()
        # Per-chat reply state set by the gateway runner before the
        # final adapter.send() so we can pick the matching completion
        # reaction (success / error / interrupted). Popped on use; the
        # default is "success" when unset, matching the historical
        # behaviour where every final send fired the Done reaction.
        self._pending_reply_state: Dict[str, str] = {}
        # Stage-aware reaction state: the label currently rendered on
        # the user message for each chat. Defaults to
        # ``REACTION_THINKING`` after the inbound emotion is fired.
        # ``notify_tool_started`` swaps it for a category label as the
        # agent makes progress; ``_fire_done_reaction`` recalls the
        # final value so the Done reaction lands on the right anchor.
        self._current_stage_label: Dict[str, str] = {}
        # Per-chat asyncio lock that serializes stage-label swaps so
        # parallel ``tool.started`` events do not race each other when
        # recalling and re-firing the emotion.
        self._stage_locks: Dict[str, "asyncio.Lock"] = {}
        # Cards in streaming state per chat: chat_id -> { out_track_id -> last_content }.
        # Every `send()` creates+finalizes a card (closed state).  A subsequent
        # `edit_message(finalize=False)` re-opens the card (DingTalk's API
        # allows streaming_update on a finalized card — it flips back to
        # streaming).  We track those reopened cards so the next `send()` can
        # auto-close them as siblings — otherwise tool-progress cards get
        # stuck in streaming state forever.
        self._streaming_cards: Dict[str, Dict[str, str]] = {}
        # Track fire-and-forget emoji/reaction coroutines so Python's GC
        # doesn't drop them mid-flight, and we can cancel them on disconnect.
        self._bg_tasks: Set[asyncio.Task] = set()
        self._bg_futures: Set[concurrent.futures.Future] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to DingTalk via Stream Mode."""
        if not DINGTALK_STREAM_AVAILABLE:
            logger.warning(
                "[%s] dingtalk-stream not installed. Run: pip install 'dingtalk-stream>=0.20'",
                self.name,
            )
            return False
        if not HTTPX_AVAILABLE:
            logger.warning(
                "[%s] httpx not installed. Run: pip install httpx", self.name
            )
            return False
        if not self._client_id or not self._client_secret:
            logger.warning(
                "[%s] DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required", self.name
            )
            return False

        try:
            self._http_client = httpx.AsyncClient(
                **_dingtalk_http_client_kwargs(timeout=30.0)
            )

            credential = dingtalk_stream.Credential(
                self._client_id, self._client_secret
            )
            self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)

            # Initialize card SDK if available and configured
            if CARD_SDK_AVAILABLE and self._card_template_id:
                sdk_config = open_api_models.Config()
                sdk_config.protocol = "https"
                sdk_config.region_id = "central"
                self._card_sdk = dingtalk_card_client.Client(sdk_config)
                self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                logger.info(
                    "[%s] Card SDK initialized with template: %s content_key=%s",
                    self.name,
                    self._card_template_id,
                    self._card_content_key,
                )
            elif CARD_SDK_AVAILABLE:
                # Initialize robot SDK even without card template (for media download)
                sdk_config = open_api_models.Config()
                sdk_config.protocol = "https"
                sdk_config.region_id = "central"
                self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                logger.info("[%s] Robot SDK initialized (media download)", self.name)

            # Capture the current event loop for cross-thread dispatch
            loop = asyncio.get_running_loop()
            self._loop = loop
            handler = _IncomingHandler(self, loop)
            self._stream_client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC, handler
            )

            self._stream_task = asyncio.create_task(self._run_stream())
            self._watchdog_task = asyncio.create_task(self._run_watchdog())
            self._mark_connected()
            logger.info("[%s] Connected via Stream Mode", self.name)
            # Plugin-registered native handlers (DingTalkStreamClient — register_callback_handler()).
            self._wire_plugin_handlers(self._stream_client)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """Run the async stream client with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                logger.debug("[%s] Starting stream client...", self.name)
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream client error: %s", self.name, e)

            if not self._running:
                return

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def _run_watchdog(self) -> None:
        """Detect and recover half-open Stream connections.

        Periodically pings the live websocket and awaits the pong. A healthy
        (even if idle) connection returns the pong promptly; a half-open one
        never does. On timeout we force the websocket closed, which unblocks
        the SDK's inner ``async for`` and triggers a fresh reconnect with a new
        ticket. See ``STREAM_PING_INTERVAL`` for the full rationale.
        """
        while self._running:
            await asyncio.sleep(self._ping_interval)
            if not self._running:
                return

            websocket = (
                getattr(self._stream_client, "websocket", None)
                if self._stream_client
                else None
            )
            if websocket is None:
                # Not connected yet (or mid-reconnect); nothing to probe.
                continue

            try:
                # ws.ping() returns a future that resolves when the matching
                # pong arrives. Awaiting it under a timeout is the actual
                # half-open detector (the SDK's own keepalive never awaits it).
                pong_waiter = await websocket.ping()
                await asyncio.wait_for(pong_waiter, timeout=self._ping_timeout)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                # Includes asyncio.TimeoutError (pong never arrived) and any
                # ConnectionClosed* raised by ping() on an already-dead socket.
                if not self._running:
                    return
                logger.warning(
                    "[%s] Stream liveness check failed (%s: %s) — forcing "
                    "reconnect on suspected half-open connection",
                    self.name,
                    type(exc).__name__,
                    exc,
                )
                try:
                    await websocket.close()
                except Exception as close_exc:
                    logger.debug(
                        "[%s] watchdog websocket close failed: %s",
                        self.name,
                        close_exc,
                    )

    async def disconnect(self) -> None:
        """Disconnect from DingTalk."""
        self._running = False
        self._mark_disconnected()

        # Stop the liveness watchdog first so it doesn't race the shutdown
        # close() below and trigger a spurious "half-open" reconnect.
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug(
                    "[%s] watchdog task did not exit cleanly during disconnect",
                    self.name,
                )
            self._watchdog_task = None

        # Close the active websocket first so the stream task sees the
        # disconnection and exits cleanly, rather than getting stuck
        # awaiting frames that will never arrive.
        websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as e:
                logger.debug("[%s] websocket close during disconnect failed: %s", self.name, e)

        if self._stream_task:
            # Try graceful close first if SDK supports it. The SDK's close()
            # is sync and may block on network I/O, so offload to a thread.
            if hasattr(self._stream_client, "close"):
                try:
                    await asyncio.to_thread(self._stream_client.close)
                except Exception:
                    pass

            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("[%s] stream task did not exit cleanly during disconnect", self.name)
            self._stream_task = None

        # Cancel any in-flight background tasks (emoji reactions, etc.)
        if self._bg_tasks:
            for task in list(self._bg_tasks):
                task.cancel()
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
        if self._bg_futures:
            for fut in list(self._bg_futures):
                fut.cancel()
            self._bg_futures.clear()

        # Finalize any open streaming cards before the HTTP client closes so
        # they don't stay stuck in streaming state on DingTalk's UI after
        # a gateway restart.  _close_streaming_siblings handles its own
        # per-card exceptions; the outer try is a safety net for token fetch.
        for _chat_id in list(self._streaming_cards):
            try:
                await self._close_streaming_siblings(_chat_id)
            except Exception as _exc:
                logger.debug(
                    "[%s] Failed to finalize streaming card on disconnect for %s: %s",
                    self.name, _chat_id, _exc,
                )

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._stream_client = None
        self._session_webhooks.clear()
        self._message_contexts.clear()
        self._streaming_cards.clear()
        self._done_emoji_fired.clear()
        self._pending_reply_state.clear()
        self._current_stage_label.clear()
        self._stage_locks.clear()
        self._dedup.clear()
        logger.info("[%s] Disconnected", self.name)

    @staticmethod
    def _read_bool_setting(
        value: Any,
        *,
        env_name: str,
        default: bool = False,
    ) -> bool:
        """Read a bool from config first, then env, matching gateway config style."""
        if value is None:
            value = os.getenv(env_name)
        if value is None:
            return default
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)

    # -- Group gating --------------------------------------------------------

    def _dingtalk_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("DINGTALK_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}

    def _dingtalk_free_response_chats(self) -> Set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("DINGTALK_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _dingtalk_allowed_chats(self) -> Set[str]:
        """Return the whitelist of group chat IDs the bot will respond in.

        When non-empty, group messages from chats NOT in this set are silently
        ignored — even if the bot is @mentioned.  DMs are never filtered.
        Empty set means no restriction (fully backward compatible).
        """
        raw = self.config.extra.get("allowed_chats") if self.config.extra else None
        if raw is None:
            raw = os.getenv("DINGTALK_ALLOWED_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        patterns = self.config.extra.get("mention_patterns") if self.config.extra else None
        if patterns is None:
            raw = os.getenv("DINGTALK_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded

        if patterns is None:
            # Parity with the historical inline implementation: return before
            # evaluating ``self.name`` (avoids touching adapter attributes on
            # the no-patterns path).
            return []

        return compile_mention_patterns(
            patterns,
            log_prefix=self.name,
            platform_label="dingtalk",
            display_label="DingTalk",
            logger_=logger,
        )

    def _load_allowed_users(self) -> Set[str]:
        """Load allowed-users list from config.extra or env var.

        IDs are matched case-insensitively against the sender's ``staff_id`` and
        ``sender_id``. A wildcard ``*`` disables the check.
        """
        raw = self.config.extra.get("allowed_users") if self.config.extra else None
        if raw is None:
            raw = os.getenv("DINGTALK_ALLOWED_USERS", "")
        if isinstance(raw, list):
            items = [str(part).strip() for part in raw if str(part).strip()]
        else:
            items = [part.strip() for part in str(raw).split(",") if part.strip()]
        return {item.lower() for item in items}

    def _is_user_allowed(self, sender_id: str, sender_staff_id: str) -> bool:
        if not self._allowed_users or "*" in self._allowed_users:
            return True
        candidates = {(sender_id or "").lower(), (sender_staff_id or "").lower()}
        candidates.discard("")
        return bool(candidates & self._allowed_users)

    def _message_mentions_bot(self, message: "ChatbotMessage") -> bool:
        """True if the bot was @-mentioned in a group message.

        dingtalk-stream sets ``is_in_at_list`` on the incoming ChatbotMessage
        when the bot is addressed via @-mention.
        """
        return bool(getattr(message, "is_in_at_list", False))

    def _message_matches_mention_patterns(self, text: str) -> bool:
        if not text or not self._mention_patterns:
            return False
        return any(pattern.search(text) for pattern in self._mention_patterns)

    def _should_process_message(self, message: "ChatbotMessage", text: str, is_group: bool, chat_id: str) -> bool:
        """Apply DingTalk group trigger rules.

        DMs remain unrestricted (subject to ``allowed_users`` which is enforced
        earlier). Group messages are accepted when:
        - the chat passes the ``allowed_chats`` whitelist (when set)
        - the chat is explicitly allowlisted in ``free_response_chats``
        - ``require_mention`` is disabled
        - the bot is @mentioned (``is_in_at_list``)
        - the text matches a configured regex wake-word pattern

        When ``allowed_chats`` is non-empty, it acts as a hard gate — messages
        from any group chat not in the list are ignored regardless of the
        other rules.
        """
        if not is_group:
            return True
        allowed = self._dingtalk_allowed_chats()
        if allowed and chat_id and chat_id not in allowed:
            return False
        if chat_id and chat_id in self._dingtalk_free_response_chats():
            return True
        if not self._dingtalk_require_mention():
            return True
        if self._message_mentions_bot(message):
            return True
        return self._message_matches_mention_patterns(text)

    def _spawn_bg(self, coro) -> None:
        """Start a fire-and-forget coroutine and track it for cleanup."""
        target_loop = self._loop if self._loop and self._loop.is_running() else None
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if target_loop is not None and running_loop is not target_loop:
            fut = asyncio.run_coroutine_threadsafe(coro, target_loop)
            self._bg_futures.add(fut)
            fut.add_done_callback(self._bg_futures.discard)
            return

        if running_loop is not None:
            task = running_loop.create_task(coro)
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return

        coro.close()
        logger.debug("[%s] Dropped background coroutine: no running event loop", self.name)

    # -- AI Card lifecycle helpers ------------------------------------------

    # Plain text shown when ``send(expect_edits=True)`` fails so the
    # editable status card cannot be created. Kept short so it does not
    # compete with the final-answer card.
    _DEGRADED_PROGRESS_NOTICE = "⚠️ 实时进度暂不可用，答案稍后返回"

    async def _send_degraded_progress_notice(
        self,
        chat_id: str,
        session_webhook: Optional[str],
    ) -> None:
        """Send a one-shot text notice when the editable AI Card path fails.

        Used only when ``send(metadata={"expect_edits": True})`` could
        not create an editable card. The notice intentionally does NOT
        return a ``message_id`` to the caller — the outer ``send()``
        still returns ``success=False`` so the turn-status coordinator
        disables itself (preventing the "outTrackId: card is not exist"
        edit-storm against a webhook id). All errors are best-effort;
        failure to deliver the notice falls back to silence rather than
        masking the original cause.
        """
        if not session_webhook:
            webhook_info = self._get_valid_webhook(chat_id)
            if webhook_info:
                session_webhook, _ = webhook_info
        if not session_webhook or not self._http_client:
            logger.debug(
                "[%s] Degraded progress notice skipped (no webhook): chat=%s",
                self.name, chat_id,
            )
            return
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "Hermes",
                "text": self._DEGRADED_PROGRESS_NOTICE,
            },
        }
        try:
            resp = await self._http_client.post(
                session_webhook, json=payload, timeout=10.0,
            )
            if resp.status_code >= 300:
                logger.debug(
                    "[%s] Degraded progress notice HTTP %d: %s",
                    self.name, resp.status_code, str(resp.text)[:200],
                )
                return
            logger.info(
                "[%s] Degraded progress notice delivered to chat=%s",
                self.name, chat_id,
            )
        except Exception as exc:
            logger.debug(
                "[%s] Degraded progress notice send failed: %s",
                self.name, exc,
            )

    async def _close_streaming_siblings(self, chat_id: str) -> None:
        """Finalize any previously-open streaming cards for this chat.

        Called at the start of every ``send()`` so lingering tool-progress
        cards that were reopened by ``edit_message(finalize=False)`` get
        cleanly closed before the next card is created.  Without this,
        tool-progress cards stay stuck in streaming state after the agent
        moves on (there is no explicit "turn end" signal from the gateway).
        """
        cards = self._streaming_cards.pop(chat_id, None)
        if not cards:
            return
        token = await self._get_access_token()
        if not token:
            return
        for out_track_id, last_content in list(cards.items()):
            try:
                await self._stream_card_content(
                    out_track_id, token, last_content, finalize=True,
                )
                logger.info(
                    "[%s] AI Card sibling closed: %s",
                    self.name, out_track_id,
                )
            except Exception as e:
                logger.debug(
                    "[%s] Sibling close failed for %s: %s",
                    self.name, out_track_id, e,
                )

    # Reaction labels for the user-message lifecycle.
    #
    # These are the visible text-emotion labels DingTalk renders next to
    # the inbound user message. They are NOT the message-card content.
    #
    # Lifecycle:
    #   1. ``REACTION_THINKING`` fires the moment an inbound message
    #      arrives so the user sees feedback within ~100ms.
    #   2. While the agent runs, the label SHIFTS to a stage-aware
    #      label (``_TOOL_STAGE_LABELS``) whenever the dominant tool
    #      category changes — e.g. ``⌨️ 敲命令中`` when terminal
    #      starts, ``👀 看文件中`` when read_file starts. Each
    #      category only fires one swap regardless of how many
    #      back-to-back calls it makes.
    #   3. On completion the label is replaced one last time with
    #      ``REACTION_DONE`` / ``REACTION_ERROR`` / ``REACTION_INTERRUPTED``
    #      based on the turn outcome.
    REACTION_THINKING = "🤔 想一想"
    REACTION_DONE = "✅ 搞定了"
    REACTION_ERROR = "😓 遇到麻烦了"
    REACTION_INTERRUPTED = "⏸️ 先停一下"

    # Tool → broad category label. Categories are coarse on purpose:
    # back-to-back terminal calls or back-to-back file reads should
    # not produce a flicker of swaps, only the FIRST call in a new
    # category triggers a label change. Tools missing from this map
    # do not swap the label (the previous stage label stays).
    _TOOL_STAGE_LABELS: Dict[str, str] = {
        "terminal":           "⌨️ 敲命令中",
        "code_execution":     "⌨️ 敲命令中",
        "execute_code":       "🔬 跑代码中",
        "read_file":          "👀 看文件中",
        "write_file":         "✍️ 写代码中",
        "patch":              "✍️ 改代码中",
        "search_files":       "🔎 搜文件中",
        "web_search":         "🔍 搜一搜",
        "web_extract":        "🌍 抓网页中",
        "browser_navigate":   "🧭 逛网页中",
        "browser_click":      "🧭 逛网页中",
        "browser_type":       "🧭 逛网页中",
        "browser_screenshot": "🧭 逛网页中",
        "browser_back":       "🧭 逛网页中",
        "browser_scroll":     "🧭 逛网页中",
        "browser_press":      "🧭 逛网页中",
        "browser_vision":     "🧭 逛网页中",
        "browser_console":    "🧭 逛网页中",
        "browser_get_images": "🧭 逛网页中",
        "memory":             "💡 想起来了",
        "delegate_task":      "🤖 叫小弟去办",
        "todo":               "📋 整理一下",
        "clarify":            "🙋 稍等确认",
        "skill_manage":       "🎯 加载技能",
        "vision":             "👁️ 看图中",
        "image_generation":   "🎨 画画中",
        "video_generation":   "🎬 剪片中",
    }

    # Terminal command → more specific reaction label.
    # Matched in order; first hit wins.
    _TERMINAL_STAGE_LABELS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"^\s*git\b"),                          "🌳 提交代码中"),
        (re.compile(r"^\s*(pytest|unittest|jest|vitest|mocha|cargo\s+test|go\s+test|npm\s+test|pnpm\s+test)\b"), "🧪 跑测试中"),
        (re.compile(r"^\s*(pip|pip3|uv|npm|pnpm|yarn|cargo|brew|apt|apt-get)\s+(install|add|i|sync)\b"), "📦 装依赖中"),
        (re.compile(r"^\s*(docker|docker-compose|kubectl|helm)\b"),  "🐳 跑容器中"),
        (re.compile(r"^\s*(curl|wget|http)\b"),             "📡 请求接口中"),
        (re.compile(r"^\s*(grep|rg|ripgrep|ag)\b"),         "🔍 搜一搜"),
        (re.compile(r"^\s*(python|python3|node|deno|ruby|bash|sh|tsx|ts-node)\b"), "▶️ 跑脚本中"),
        (re.compile(r"^\s*(make|cmake|cargo\s+build|go\s+build|mvn)\b"), "🔨 编译中"),
        (re.compile(r"^\s*(cat|head|tail|bat)\b"),           "👀 看文件中"),
        (re.compile(r"^\s*(ls|find|tree|fd)\b"),             "🗂️ 翻目录中"),
    ]

    @classmethod
    def _stage_label_for_tool(
        cls, tool_name: Optional[str], preview: str = "",
    ) -> Optional[str]:
        """Return the stage label for *tool_name*, or None to keep current label.

        For ``terminal`` calls, *preview* (the command string) is used to
        pick a more specific label from ``_TERMINAL_STAGE_LABELS``.
        """
        if not tool_name:
            return None
        if tool_name == "terminal" and preview:
            for pattern, label in cls._TERMINAL_STAGE_LABELS:
                if pattern.match(preview):
                    return label
        return cls._TOOL_STAGE_LABELS.get(tool_name)

    def set_pending_reply_state(self, chat_id: str, state: str) -> None:
        """Record the outcome of the agent run for the next final send.

        Called by the gateway runner once it knows whether the turn
        succeeded, failed, or was interrupted. The next ``send()`` that
        triggers a final reaction reads this and picks the matching
        completion label. Unknown / unset states fall back to
        ``"success"`` so we never silently lose the Done reaction.

        Valid states: ``"success"``, ``"error"``, ``"interrupted"``.
        """
        if not chat_id:
            return
        if state not in ("success", "error", "interrupted"):
            state = "success"
        self._pending_reply_state[chat_id] = state

    def notify_tool_started(
        self, chat_id: str, tool_name: Optional[str], preview: str = "",
    ) -> None:
        """Optionally swap the in-flight reaction to a stage-aware label.

        Called by the gateway runner on every ``tool.started`` event.
        Looks up a broad category label for the tool and, if the
        category has changed since the last swap on this chat, fires
        a recall+reply pair to update the visible label. Tools not in
        ``_TOOL_STAGE_LABELS`` (or repeat calls of the same category)
        are no-ops, so a run of 5 back-to-back terminal calls costs
        exactly one swap.

        Safe to call from any thread / loop context; the actual
        recall+reply happens in a background task serialized by a
        per-chat lock so parallel tool starts cannot race.
        """
        if not chat_id:
            return
        new_label = self._stage_label_for_tool(tool_name, preview=preview)
        if new_label is None:
            return
        # Cheap pre-lock check — skip spawning a task when nothing
        # would change. The lock holds the source of truth.
        current = self._current_stage_label.get(chat_id, self.REACTION_THINKING)
        if current == new_label:
            return
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _swap() -> None:
            lock = self._stage_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                # Re-read inside the lock — another swap may have
                # landed between the cheap check and the spawn.
                actual_current = self._current_stage_label.get(
                    chat_id, self.REACTION_THINKING,
                )
                if actual_current == new_label:
                    return
                await self._send_emotion(
                    msg_id, conversation_id, actual_current, recall=True,
                )
                await self._send_emotion(
                    msg_id, conversation_id, new_label, recall=False,
                )
                self._current_stage_label[chat_id] = new_label

        self._spawn_bg(_swap())

    def _fire_done_reaction(self, chat_id: str) -> None:
        """Swap the in-flight reaction for the turn outcome.

        Reads the pending reply state set by the gateway runner via
        :meth:`set_pending_reply_state`. Defaults to "success" so this
        is safe to call even when nothing set the state (preserves the
        previous behaviour for adapters that haven't wired the hook).
        Idempotent per chat_id — safe to call from segment-break
        flushes and final-done flushes without double-firing.

        Recalls whatever stage label was last fired by
        :meth:`notify_tool_started` (default ``REACTION_THINKING``) so
        the recall matches what DingTalk is actually rendering. Doing
        a blind ``REACTION_THINKING`` recall would leave a stage label
        like ``⌨️ 敲命令中`` orphaned on the message.
        """
        if chat_id in self._done_emoji_fired:
            return
        self._done_emoji_fired.add(chat_id)
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return
        state = self._pending_reply_state.pop(chat_id, "success")
        if state == "error":
            final_label = self.REACTION_ERROR
        elif state == "interrupted":
            final_label = self.REACTION_INTERRUPTED
        else:
            final_label = self.REACTION_DONE

        async def _swap() -> None:
            lock = self._stage_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                current = self._current_stage_label.pop(
                    chat_id, self.REACTION_THINKING,
                )
                await self._send_emotion(
                    msg_id, conversation_id, current, recall=True,
                )
                await self._send_emotion(
                    msg_id, conversation_id, final_label, recall=False,
                )

        self._spawn_bg(_swap())

    def _fire_custom_reactions(self, chat_id: str, emotion_names: List[str]) -> None:
        """Reply with custom DingTalk text emotions requested in message tags."""
        if not emotion_names:
            return
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _send_all() -> None:
            for emotion_name in emotion_names:
                await self._send_emotion(
                    msg_id, conversation_id, emotion_name, recall=False,
                )

        self._spawn_bg(_send_all())

    @classmethod
    def _extract_emotion_tags(cls, content: str) -> tuple[str, List[str]]:
        """Extract ``[[emotion:...]]``/``[[dingtalk:emotion=...]]`` tags."""
        if not content:
            return content, []
        emotions: List[str] = []

        def _replace(match: re.Match) -> str:
            name = (match.group(1) or "").strip()
            if name:
                emotions.append(name[:64])
            return ""

        cleaned = _DINGTALK_EMOTION_TAG_RE.sub(_replace, content)
        return cleaned.strip(), emotions

    @staticmethod
    def _metadata_values(metadata: Dict[str, Any], *keys: str) -> List[str]:
        values: List[str] = []
        for key in keys:
            raw = metadata.get(key)
            if raw is None:
                continue
            if isinstance(raw, (list, tuple, set)):
                values.extend(str(part).strip() for part in raw)
            else:
                values.extend(part.strip() for part in str(raw).split(","))
        return [value for value in values if value]

    @staticmethod
    def _metadata_bool(metadata: Dict[str, Any], *keys: str) -> bool:
        for key in keys:
            raw = metadata.get(key)
            if raw is None:
                continue
            if isinstance(raw, str):
                return raw.lower() in {"true", "1", "yes", "on"}
            return bool(raw)
        return False

    @staticmethod
    def _metadata_path(metadata: Dict[str, Any], *keys: str) -> Optional[Path]:
        for key in keys:
            raw = metadata.get(key)
            if raw is None:
                continue
            path = Path(str(raw)).expanduser()
            if path.is_file():
                return path
        return None

    @staticmethod
    def _duration_ms_from_metadata(metadata: Dict[str, Any]) -> Optional[int]:
        for key in ("dingtalk_duration_ms", "duration_ms"):
            raw = metadata.get(key)
            if raw is None:
                continue
            try:
                value = int(float(str(raw)))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value

        for key in ("dingtalk_duration_seconds", "duration_seconds", "duration"):
            raw = metadata.get(key)
            if raw is None:
                continue
            try:
                value = int(float(str(raw)) * 1000)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _probe_media_duration_ms(path: Path) -> Optional[int]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            seconds = float((result.stdout or "").strip())
            duration_ms = int(seconds * 1000)
            return duration_ms if duration_ms > 0 else None
        except Exception:
            return None

    @staticmethod
    def _generate_video_cover(path: Path) -> Optional[Path]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        output = Path(tempfile.gettempdir()) / f"hermes_dingtalk_video_{uuid.uuid4().hex}.jpg"
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(output),
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                return output
        except Exception:
            pass
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    @staticmethod
    def _write_default_video_cover() -> Optional[Path]:
        output = Path(tempfile.gettempdir()) / f"hermes_dingtalk_video_cover_{uuid.uuid4().hex}.png"
        try:
            width, height = 320, 180
            row = b"\x00" + (b"\x1f\x1f\x1f" * width)
            raw = row * height

            def chunk(kind: bytes, data: bytes) -> bytes:
                return (
                    struct.pack(">I", len(data))
                    + kind
                    + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
                )

            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b"")
            )
            output.write_bytes(png)
            return output
        except Exception:
            try:
                output.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    @staticmethod
    def _looks_like_mp4(path: Path) -> bool:
        try:
            with path.open("rb") as fh:
                header = fh.read(32)
        except Exception:
            return False
        return len(header) >= 12 and b"ftyp" in header[:16]

    @staticmethod
    def _looks_like_native_audio(path: Path, ext: str) -> bool:
        try:
            with path.open("rb") as fh:
                header = fh.read(16)
        except Exception:
            return False
        if ext == "ogg":
            return header.startswith(b"OggS")
        if ext == "amr":
            return header.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
        return False

    def _collect_at_users(
        self,
        chat_id: str,
        metadata: Dict[str, Any],
        *,
        include_sender: bool,
    ) -> Dict[str, str]:
        """Collect DingTalk user IDs for @ mentions.

        Values are accepted from metadata for explicit sends and optionally
        from the current inbound group message when final replies should @ the
        sender.  The returned mapping shape matches DingTalk card deliver
        ``atUserIds`` while webhook payloads use just the keys.
        """
        users: Dict[str, str] = {}
        for user_id in self._metadata_values(
            metadata, "dingtalk_at_user_ids", "at_user_ids", "atUserIds",
        ):
            users[user_id] = user_id

        if include_sender:
            msg = self._message_contexts.get(chat_id)
            conversation_type = getattr(msg, "conversation_type", "") if msg else ""
            if str(conversation_type) == "2":
                sender_staff_id = getattr(msg, "sender_staff_id", "") or ""
                sender_nick = getattr(msg, "sender_nick", "") or sender_staff_id
                if sender_staff_id:
                    users[sender_staff_id] = sender_nick
        return users

    def _build_webhook_at_payload(
        self,
        metadata: Dict[str, Any],
        at_users: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        at_mobiles = self._metadata_values(
            metadata, "dingtalk_at_mobiles", "at_mobiles", "atMobiles",
        )
        at_all = self._metadata_bool(metadata, "dingtalk_at_all", "at_all", "isAtAll")
        if not at_users and not at_mobiles and not at_all:
            return None
        return {
            "atUserIds": list(at_users.keys()),
            "atMobiles": at_mobiles,
            "isAtAll": at_all,
        }

    @staticmethod
    def _prepend_mention_tokens(
        content: str,
        at_payload: Optional[Dict[str, Any]],
    ) -> str:
        """Prepend DingTalk mention tokens required by webhook @ delivery.

        DingTalk webhook @ semantics require visible markdown/text to contain
        the same mobile or user ID listed in the ``at`` payload.  AI Cards use
        structured @ fields instead, so card content should stay clean.  A
        display name such as ``@Alice`` renders as plain text for
        ``atUserIds=["staff-1"]``.
        """
        if not at_payload:
            return content

        mentions: List[str] = []
        if at_payload.get("isAtAll"):
            mentions.append("@所有人")
        for mobile in at_payload.get("atMobiles") or []:
            mobile_text = str(mobile).strip()
            if mobile_text:
                mentions.append(f"@{mobile_text}")
        for user_id in at_payload.get("atUserIds") or []:
            user_text = str(user_id).strip()
            if user_text:
                mentions.append(f"@{user_text}")

        # Preserve order and remove duplicates.
        deduped = list(dict.fromkeys(mentions))
        if not deduped:
            return content
        prefix = " ".join(deduped)
        if content.lstrip().startswith(prefix):
            return content
        return f"{prefix}\n\n{content}" if content else prefix

    def _current_card_content_key(self) -> str:
        """Return the dashboard-configured AI Card content key.

        This is read dynamically so Dashboard config changes take effect for
        the next card update without a gateway restart.  Empty config falls
        back to DingTalk's default markdown card variable.
        """
        try:
            if self._card_content_key_override:
                return self._card_content_key_override
            config = load_config_readonly()
            dingtalk_config = config.get("dingtalk")
            if isinstance(dingtalk_config, dict):
                configured = str(dingtalk_config.get("card_content_key") or "").strip()
                if configured:
                    return configured
        except Exception as exc:
            logger.debug("[%s] Failed to read DingTalk card_content_key: %s", self.name, exc)
        return DEFAULT_AI_CARD_CONTENT_KEY

    def _card_initial_param_map(self) -> Dict[str, str]:
        """Return initial card data for custom templates or the SDK default."""
        if not self._card_uses_default_template:
            return {self._current_card_content_key(): ""}
        order = [
            "msgTitle",
            "msgContent",
            "staticMsgContent",
            "msgTextList",
            "msgImages",
            "msgSlider",
            "msgButtons",
        ]
        return {
            "msgContent": "",
            "staticMsgContent": "",
            "flowStatus": "1",
            "sys_full_json_obj": json.dumps({"order": order}, ensure_ascii=False),
        }

    # -- Inbound message processing -----------------------------------------

    async def _on_message(
        self,
        message: "ChatbotMessage",
    ) -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return

        # Chat context
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""

        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        # Allowed-users gate (applies to both DM and group)
        if not self._is_user_allowed(sender_id, sender_staff_id):
            logger.debug(
                "[%s] Dropping message from non-allowlisted user staff_id=%s sender_id=%s",
                self.name, sender_staff_id, sender_id,
            )
            return

        # Group mention/pattern gate.  DMs pass through unconditionally.
        # We need the message text for regex wake-word matching; extract it
        # early but don't consume the rest of the pipeline until after the
        # gate decides whether to process.
        _early_text = self._extract_text(message) or ""
        if not self._should_process_message(message, _early_text, is_group, chat_id):
            logger.debug(
                "[%s] Dropping group message that failed mention gate message_id=%s chat_id=%s",
                self.name, msg_id, chat_id,
            )
            return

        # Stash the incoming message keyed by chat_id so concurrent
        # conversations don't clobber each other's context.  Also reset
        # the per-chat "Done emoji fired" marker so a new inbound message
        # gets its own Thinking→Done cycle.
        if chat_id:
            self._message_contexts[chat_id] = message
            self._done_emoji_fired.discard(chat_id)

        # Store session webhook
        session_webhook = getattr(message, "session_webhook", None) or ""
        session_webhook_expired_time = (
            getattr(message, "session_webhook_expired_time", 0) or 0
        )
        if session_webhook and chat_id and _DINGTALK_WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                try:
                    self._session_webhooks.pop(next(iter(self._session_webhooks)))
                except StopIteration:
                    pass
            self._session_webhooks[chat_id] = (
                session_webhook,
                session_webhook_expired_time,
            )

        # Resolve media download codes to URLs so vision tools can use them
        await self._resolve_media_codes(message)

        # Extract text content
        text = self._extract_text(message)

        # Determine message type and build media list
        msg_type, media_urls, media_types = self._extract_media(message)
        media_errors = self._extract_media_errors(message)

        if not text and not media_urls and not media_errors:
            logger.debug("[%s] Empty message, skipping", self.name)
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=getattr(message, "conversation_title", None),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_nick,
            user_id_alt=sender_staff_id if sender_staff_id else None,
        )

        # Parse timestamp
        create_at = getattr(message, "create_at", None)
        try:
            timestamp = (
                datetime.fromtimestamp(int(create_at) / 1000, tz=timezone.utc)
                if create_at
                else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id,
            raw_message=message,
            media_urls=media_urls,
            media_types=media_types,
            media_errors=media_errors,
            timestamp=timestamp,
        )

        logger.debug(
            "[%s] Message from %s in %s: %s",
            self.name,
            sender_nick,
            chat_id[:20] if chat_id else "?",
            text[:80] if text else "(media)",
        )
        await self.handle_message(event)

    @staticmethod
    def _extract_text(message: "ChatbotMessage") -> str:
        """Extract plain text from a DingTalk chatbot message.

        Handles both legacy and current dingtalk-stream SDK payload shapes:
          * legacy: ``message.text`` was a dict ``{"content": "..."}``
          * >= 0.20: ``message.text`` is a ``TextContent`` dataclass whose
            ``__str__`` returns ``"TextContent(content=...)"`` — never fall
            back to ``str(text)`` without extracting ``.content`` first.
          * rich text moved from ``message.rich_text`` (list) to
            ``message.rich_text_content.rich_text_list`` (list of dicts).
        """
        text = getattr(message, "text", None) or ""

        # Handle TextContent object (SDK style)
        if hasattr(text, "content"):
            content = (text.content or "").strip()
        elif isinstance(text, dict):
            content = text.get("content", "").strip()
        else:
            content = str(text).strip()

        if not content:
            rich_text = getattr(message, "rich_text_content", None) or getattr(
                message, "rich_text", None
            )
            if rich_text:
                rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
                if isinstance(rich_list, list):
                    parts = []
                    for item in rich_list:
                        if isinstance(item, dict):
                            t = item.get("text") or item.get("content") or ""
                            if t:
                                parts.append(t)
                        elif hasattr(item, "text") and item.text:
                            parts.append(item.text)
                    content = " ".join(parts).strip()

        # Fallback: audio message → use recognition text
        if not content:
            msg_type = getattr(message, "message_type", "")
            if msg_type == "audio":
                extensions = getattr(message, "extensions", {}) or {}
                audio_content = extensions.get("content", {})
                if isinstance(audio_content, dict):
                    recognition = audio_content.get("recognition", "")
                    if recognition:
                        content = recognition.strip()

        # Fallback: file message → use fileName as text
        if not content:
            msg_type = getattr(message, "message_type", "")
            if msg_type == "file":
                extensions = getattr(message, "extensions", {}) or {}
                file_content = extensions.get("content", {})
                if isinstance(file_content, dict):
                    fname = file_content.get("fileName", "")
                    if fname:
                        content = f"[文件] {fname}"

        # Fallback: card message (钉钉文档分享卡片 / link card)
        # When a user shares a DingTalk Doc to the bot, the msgtype is "card"
        # and the card data lives in extensions['card'] (SDK's from_dict maps
        # unhandled fields to extensions).  Extract title + doc URL so the
        # message isn't silently dropped as "empty".
        if not content:
            msg_type = getattr(message, "message_type", "")
            # Handle card-type messages (文档分享卡片 / link card)
            if msg_type == "card":
                extensions = getattr(message, "extensions", {}) or {}
                card = extensions.get("card", {})
                if isinstance(card, dict):
                    title = card.get("title", "")
                    raw_content = card.get("content", "")
                    doc_url = ""
                    if raw_content is None:
                        doc_url = ""
                    elif isinstance(raw_content, dict):
                        doc_url = raw_content.get("url", "") or raw_content.get("docUrl", "")
                    elif isinstance(raw_content, str):
                        stripped = raw_content.strip()
                        if not stripped:
                            doc_url = ""
                        else:
                            try:
                                parsed = json.loads(stripped)
                                if isinstance(parsed, dict):
                                    doc_url = parsed.get("url", "") or parsed.get("docUrl", "")
                            except (ValueError, TypeError):
                                doc_url = raw_content
                    parts = []
                    if title:
                        parts.append(f"[文档] {title}")
                    if doc_url:
                        parts.append(doc_url)
                    if parts:
                        content = " ".join(parts)
                # Last-resort: raw text field from extensions (if present)
                if not content:
                    ext_text = extensions.get("text", {})
                    if isinstance(ext_text, dict):
                        content = (ext_text.get("content", "") or "").strip()

            # Handle interactiveCard messages (钉钉文档分享卡片 / doc link card)
            # structure: extensions["content"]["biz_custom_action_url"] and
            # extensions["content"]["title"] for the card title
            if msg_type == "interactiveCard" and not content:
                extensions = getattr(message, "extensions", {}) or {}
                ext_content = extensions.get("content", {})
                if isinstance(ext_content, dict):
                    doc_url = ext_content.get("biz_custom_action_url", "")
                    title = ext_content.get("title", "")
                    if doc_url or title:
                        parts = []
                        if title:
                            parts.append(f"[文档卡片] {title}")
                        else:
                            parts.append("[文档卡片]")
                        if doc_url:
                            parts.append(doc_url)
                        content = " ".join(parts)

        # Do NOT strip "@bot" from the text.  The mention is a routing
        # signal (delivered structurally via callback `isInAtList`), and
        # regex-stripping @handles would collateral-damage e-mails
        # (alice@example.com), SSH URLs (git@github.com), and literal
        # references the user wrote ("what does @openai think").  Let the
        # LLM see the raw text — it handles "@bot hello" cleanly.
        return content

    _MEDIA_CODE_KEYS = ("downloadCode", "pictureDownloadCode", "download_code")
    _MEDIA_URL_KEYS = ("downloadUrl", "download_url")
    _MEDIA_TYPE_KEYS = ("type", "msgtype", "msgType", "fileType", "file_type")
    _MEDIA_FILENAME_KEYS = (
        "fileName",
        "file_name",
        "filename",
        "name",
        "title",
    )

    @staticmethod
    def _media_get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        value = getattr(obj, key, default)
        return default if value is None else value

    @staticmethod
    def _media_set(obj: Any, key: str, value: Any) -> None:
        if isinstance(obj, dict):
            obj[key] = value
            return
        try:
            setattr(obj, key, value)
        except Exception:
            logger.debug("Failed to set DingTalk media field %s", key, exc_info=True)

    @staticmethod
    def _iter_rich_text_items(message: "ChatbotMessage") -> List[Any]:
        """Return rich-text items across legacy and current SDK shapes."""
        rich_sources = [
            getattr(message, "rich_text_content", None),
            getattr(message, "rich_text", None),
        ]
        for rich_text in rich_sources:
            if not rich_text:
                continue
            if isinstance(rich_text, dict):
                rich_list = (
                    rich_text.get("richTextList")
                    or rich_text.get("rich_text_list")
                    or rich_text.get("richText")
                    or rich_text.get("items")
                    or []
                )
            else:
                rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
            if isinstance(rich_list, list):
                return rich_list
        return []

    @classmethod
    def _first_media_ref(cls, item: Any) -> tuple[Optional[str], Optional[str], bool]:
        """Return (ref, key, is_download_code) for a DingTalk media item."""
        for key in cls._MEDIA_CODE_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, True
        for key in cls._MEDIA_URL_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, False
        return None, None, False

    @classmethod
    def _rich_item_type(cls, item: Any) -> str:
        for key in cls._MEDIA_TYPE_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value).strip().lower()
        return ""

    @classmethod
    def _rich_item_filename(cls, item: Any) -> Optional[str]:
        for key in cls._MEDIA_FILENAME_KEYS:
            value = cls._media_get(item, key)
            if value:
                return Path(str(value)).name
        return None

    @staticmethod
    def _default_media_type(mapped: str, filename: Optional[str] = None) -> str:
        if filename:
            guessed, _ = mimetypes.guess_type(filename)
            if guessed:
                return guessed
        if mapped == "image":
            return "image/jpeg"
        if mapped == "audio":
            return "audio/ogg"
        if mapped == "video":
            return "video/mp4"
        return "application/octet-stream"

    @staticmethod
    def _extension_for_media(
        mapped: str,
        media_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        if filename:
            ext = Path(filename).suffix
            if ext:
                return ext
        if media_type:
            ext = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
            if ext:
                return ".jpg" if ext == ".jpe" else ext
        if mapped == "image":
            return ".jpg"
        if mapped == "audio":
            return ".ogg"
        if mapped == "video":
            return ".mp4"
        return ".bin"

    @classmethod
    def _media_type_for_item(
        cls,
        item: Any,
        mapped: str,
        filename: Optional[str] = None,
    ) -> str:
        explicit = cls._media_get(item, "_hermes_media_type")
        if explicit:
            return str(explicit)
        return cls._default_media_type(mapped, filename)

    @classmethod
    def _set_cached_media_ref(
        cls,
        obj: Any,
        key: str,
        value: str,
        media_type: str,
        filename: Optional[str],
    ) -> None:
        cls._media_set(obj, key, value)
        if isinstance(obj, dict):
            obj["_hermes_media_type"] = media_type
            if filename:
                obj["_hermes_file_name"] = filename
            return
        try:
            setattr(obj, "_hermes_media_type", media_type)
            if filename:
                setattr(obj, "_hermes_file_name", filename)
        except Exception:
            logger.debug("Failed to attach DingTalk media metadata", exc_info=True)

    @classmethod
    def _set_media_error(cls, obj: Any, message: str) -> None:
        if isinstance(obj, dict):
            obj["_hermes_media_error"] = message
            return
        try:
            setattr(obj, "_hermes_media_error", message)
        except Exception:
            logger.debug("Failed to attach DingTalk media error", exc_info=True)

    @classmethod
    def _media_error_for_item(cls, item: Any) -> Optional[str]:
        value = cls._media_get(item, "_hermes_media_error")
        return str(value) if value else None

    def _extract_media(self, message: "ChatbotMessage"):
        """Extract media info from message. Returns (MessageType, [urls], [mime_types])."""
        msg_type = MessageType.TEXT
        media_urls = []
        media_types = []

        # Check for image/picture
        image_content = getattr(message, "image_content", None)
        if image_content:
            if self._media_error_for_item(image_content):
                msg_type = MessageType.PHOTO
            else:
                media_ref, _, _ = self._first_media_ref(image_content)
                if media_ref:
                    media_urls.append(media_ref)
                    media_types.append(self._media_type_for_item(image_content, "image"))
                    msg_type = MessageType.PHOTO

        # Check for rich text with mixed content
        for item in self._iter_rich_text_items(message):
            error = self._media_error_for_item(item)
            media_ref, _, _ = self._first_media_ref(item)
            if not media_ref and not error:
                continue
            item_type = self._rich_item_type(item)
            mapped = DINGTALK_TYPE_MAPPING.get(item_type, "file")
            if error:
                if msg_type == MessageType.TEXT:
                    if mapped == "image":
                        msg_type = MessageType.PHOTO
                    elif mapped == "audio":
                        msg_type = MessageType.VOICE if item_type == "voice" else MessageType.AUDIO
                    elif mapped == "video":
                        msg_type = MessageType.VIDEO
                    else:
                        msg_type = MessageType.DOCUMENT
                continue
            filename = self._rich_item_filename(item)
            media_urls.append(media_ref)
            media_types.append(self._media_type_for_item(item, mapped, filename))
            if msg_type == MessageType.TEXT:
                if mapped == "image":
                    msg_type = MessageType.PHOTO
                elif mapped == "audio":
                    # DingTalk's "voice" rich-text item is a native voice note
                    # and should enter STT. Uploaded audio files stay as AUDIO.
                    msg_type = MessageType.VOICE if item_type == "voice" else MessageType.AUDIO
                elif mapped == "video":
                    msg_type = MessageType.VIDEO
                else:
                    msg_type = MessageType.DOCUMENT

        msg_type_str = getattr(message, "message_type", "") or ""
        if msg_type_str == "picture" and not media_urls:
            msg_type = MessageType.PHOTO
        elif msg_type_str == "richText":
            # Only re-derive the type when the rich-text scan above left it
            # at TEXT. The scan may already have promoted it to VOICE/AUDIO/
            # VIDEO/DOCUMENT for embedded media items — resetting those here
            # dropped native voice notes back to TEXT and skipped STT
            # (#38211, #38219; analysis from #38276).
            if msg_type == MessageType.TEXT and any(
                "image" in t for t in media_types
            ):
                msg_type = MessageType.PHOTO
        elif msg_type_str == "audio":
            # Voice message — DingTalk already provides recognition text.
            # Do NOT add media_urls here: if audio_paths is non-empty,
            # run.py's _enrich_message_with_transcription will overwrite
            # the recognition text with a failed STT attempt (whisper not installed).
            # The recognition text from extensions['content']['recognition']
            # is sufficient and already extracted by _extract_text.
            if msg_type == MessageType.TEXT:
                msg_type = MessageType.VOICE
        elif msg_type_str in ("file", "image"):
            extensions = getattr(message, "extensions", {}) or {}
            ext_content = extensions.get("content", {})
            if isinstance(ext_content, dict):
                dl_code = ext_content.get("downloadCode") or ""
                fname = ext_content.get("fileName", "")
                if dl_code:
                    media_urls.append(dl_code)
                    mime = "application/octet-stream"
                    # Map common extensions
                    if fname:
                        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                        mime = EXT_MAP.get(ext, mime)
                    media_types.append(mime)
                    if msg_type == MessageType.TEXT:
                        # Image messages → PHOTO (distinct busy-session handling
                        # in gateway/platforms/base.py).
                        # File messages with image MIME types (e.g. a .png sent
                        # as a file attachment) are also classified as PHOTO —
                        # the user's intent is to share an image regardless of
                        # how DingTalk delivers it.
                        if msg_type_str == "image" or mime.startswith("image/"):
                            msg_type = MessageType.PHOTO
                        else:
                            msg_type = MessageType.DOCUMENT

        return msg_type, media_urls, media_types

    def _extract_media_errors(self, message: "ChatbotMessage") -> List[str]:
        errors: List[str] = []
        image_content = getattr(message, "image_content", None)
        if image_content:
            error = self._media_error_for_item(image_content)
            if error:
                errors.append(error)
        for item in self._iter_rich_text_items(message):
            error = self._media_error_for_item(item)
            if error:
                errors.append(error)
        return errors

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a markdown reply via DingTalk session webhook."""
        metadata = metadata or {}
        content = str(content or "")
        content, emotion_names = self._extract_emotion_tags(content)
        logger.debug(
            "[%s] send() chat_id=%s card_enabled=%s",
            self.name,
            chat_id,
            bool(self._card_template_id and self._card_sdk),
        )

        # Check metadata first (for direct webhook sends). Do not fail here:
        # AI Card delivery does not use session_webhook, and should still be
        # attempted when the Stream callback did not provide/cache a webhook.
        session_webhook = metadata.get("session_webhook")
        if not session_webhook:
            webhook_info = self._get_valid_webhook(chat_id)
            if webhook_info:
                session_webhook, _ = webhook_info

        # Look up the inbound message for this chat (for AI Card routing)
        current_message = self._message_contexts.get(chat_id)

        # ``metadata.expect_edits`` is the explicit lifecycle contract for
        # editable previews/status cards.  Only those cards remain in
        # streaming state after create; ordinary sends without a reply anchor
        # (background notices, queued follow-up delivery, slash command output)
        # are one-shot finalized cards.  ``reply_to`` still means "this is the
        # final response to an inbound user message" for @-sender and Done
        # reactions, but it no longer decides whether the card is left open.
        expect_edits = bool(metadata.get("expect_edits"))
        is_final_reply = reply_to is not None
        finalize_on_create = not expect_edits
        fire_final_reaction = is_final_reply and finalize_on_create
        at_users = self._collect_at_users(
            chat_id, metadata, include_sender=fire_final_reaction and self._reply_at_sender,
        )
        at_payload = self._build_webhook_at_payload(metadata, at_users)
        if at_users:
            logger.info(
                "[%s] DingTalk @ mentions prepared: users=%d final_reply=%s card_enabled=%s",
                self.name,
                len(at_users),
                is_final_reply,
                bool(self._card_template_id and current_message and self._card_sdk),
            )
        if fire_final_reaction and self._reply_at_sender and not at_users:
            conversation_type = (
                getattr(current_message, "conversation_type", "") if current_message else ""
            )
            if str(conversation_type) == "2":
                logger.warning(
                    "[%s] reply_at_sender is enabled but sender_staff_id is missing; "
                    "cannot @ the DingTalk group sender",
                    self.name,
                )

        if not content.strip() and emotion_names:
            self._fire_custom_reactions(chat_id, emotion_names)
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

        # Try AI Card first (using alibabacloud_dingtalk.card_1_0 SDK).
        # AI Card only supports user-id mentions.  Mobile and @all mentions
        # stay on the webhook path, whose payload supports those fields.
        card_can_deliver_at = (
            not at_payload
            or (
                bool(at_users)
                and not at_payload.get("atMobiles")
                and not at_payload.get("isAtAll")
            )
        )
        if self._card_template_id and current_message and self._card_sdk and card_can_deliver_at:
            # Close any previously-open streaming cards for this chat
            # before creating a new one (handles tool-progress → final-
            # response handoff; also cleans up lingering commentary cards).
            await self._close_streaming_siblings(chat_id)

            result = await self._create_and_stream_card(
                chat_id, current_message, content,
                finalize=finalize_on_create,
                at_users=at_users,
            )
            if result and result.success:
                self._fire_custom_reactions(chat_id, emotion_names)
                if fire_final_reaction:
                    # Final reply: card closed, swap Thinking → Done.
                    self._fire_done_reaction(chat_id)
                if expect_edits:
                    # Intermediate (tool progress / commentary / streaming
                    # first chunk): keep the card open and track it so the
                    # next send() auto-closes it as a sibling, or
                    # edit_message(finalize=True) closes it explicitly.
                    self._streaming_cards.setdefault(chat_id, {})[
                        result.message_id
                    ] = content
                return result

            logger.warning("[%s] AI Card send failed, falling back to webhook", self.name)
            if expect_edits:
                # The editable AI Card path failed (e.g. IP whitelist
                # outage, transient SDK error). Returning success=False
                # here makes the turn-status coordinator disable itself,
                # which prevents an edit-storm against a webhook
                # message_id that DingTalk's streaming_update API does
                # not accept (#2026-06-20 15:15 IP whitelist incident).
                #
                # But silent failure is its own bad UX — the user is
                # left staring at no progress for the rest of the turn.
                # As a one-shot notice, send a plain webhook line so
                # the user knows real-time progress is unavailable.
                # The notice is best-effort and intentionally does NOT
                # return its message_id (the caller still gets
                # success=False so no edits are attempted).
                await self._send_degraded_progress_notice(
                    chat_id, session_webhook,
                )
                return SendResult(
                    success=False,
                    error=(
                        "Editable DingTalk AI Card send failed; "
                        "webhook fallback cannot be edited"
                    ),
                )

        if not session_webhook:
            # Defect #2 fix: a session_webhook is only valid for a short
            # window after the user's inbound message. Long agent turns and
            # gateway restarts routinely outlive it, and the old code simply
            # dropped the reply here ("Reply must follow an incoming
            # message"). We instead fall back to the robot-native proactive
            # message path (_send_robot_native_message → OrgGroupSend /
            # PrivateChatSend), which authenticates with the app access
            # token instead of the ephemeral webhook and can deliver at any
            # time. This is the same capability already used for native
            # media messages; here we wire it for plain markdown replies.
            logger.warning(
                "[%s] No valid session_webhook for chat_id=%s — falling back "
                "to robot-native proactive send",
                self.name, chat_id,
            )
            return await self._send_markdown_proactive(
                chat_id, content, at_payload, metadata,
                emotion_names=emotion_names,
                fire_final_reaction=fire_final_reaction,
            )

        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        logger.debug("[%s] Sending via webhook", self.name)
        # Normalize markdown for DingTalk
        normalized = self._normalize_markdown(content[: self.MAX_MESSAGE_LENGTH])
        normalized = self._prepend_mention_tokens(normalized, at_payload)

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Hermes", "text": normalized},
        }
        if at_payload:
            payload["at"] = at_payload

        try:
            resp = await self._http_client.post(
                session_webhook, json=payload, timeout=15.0
            )
            if resp.status_code < 300:
                self._fire_custom_reactions(chat_id, emotion_names)
                # Webhook path: fire Done only for final replies, same as
                # the card path.
                if fire_final_reaction:
                    self._fire_done_reaction(chat_id)
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            body = resp.text
            logger.warning(
                "[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200]
            )
            # Defect #2 fix (continued): a webhook that DingTalk rejects
            # (expired mid-flight → 400 "expired", robot removed from group,
            # etc.) is just as dead as a missing one. Fall back to the
            # proactive path rather than losing the reply. We only retry on
            # 4xx (the webhook itself is bad); 5xx is a transient DingTalk
            # server issue where a retry against the SAME dead webhook is
            # pointless and the proactive path may hit the same outage.
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "[%s] webhook rejected (HTTP %d) — falling back to "
                    "robot-native proactive send for chat_id=%s",
                    self.name, resp.status_code, chat_id,
                )
                fallback = await self._send_markdown_proactive(
                    chat_id, content, at_payload, metadata,
                    emotion_names=emotion_names,
                    fire_final_reaction=fire_final_reaction,
                )
                if fallback.success:
                    return fallback
            return SendResult(
                success=False, error=f"HTTP {resp.status_code}: {body[:200]}"
            )
        except httpx.TimeoutException:
            return SendResult(
                success=False, error="Timeout sending message to DingTalk"
            )
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _send_markdown_proactive(
        self,
        chat_id: str,
        content: str,
        at_payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        emotion_names: Optional[list] = None,
        fire_final_reaction: bool = False,
    ) -> SendResult:
        """Deliver a markdown reply without a session_webhook (Defect #2).

        A session_webhook is only valid for a short window after the user's
        inbound message; long agent turns and gateway restarts outlive it.
        Two webhook-independent transports exist for a Stream app, tried in
        order of reliability:

          1. **AI Card** (``_create_and_stream_card``) — the SDK-proven
             transport. It authenticates with the app access token and
             targets a group by ``dtv1.card//IM_GROUP.{conversation_id}``
             (or a DM robot open-space), so it works with no live webhook.
             This is the SAME path ``send()`` already prefers when an
             inbound message context exists; here we drive it explicitly
             for the resume case where that context was lost on restart, by
             synthesizing a minimal message carrying ``conversation_id`` =
             ``chat_id``.
          2. **Robot-native ``sampleMarkdown``** (OrgGroupSend /
             PrivateChatSend) — only works for a *published org-internal
             robot*; a plain Stream app is rejected with ``robot 不存在``.
             Kept as a best-effort last resort for deployments that DO have
             one configured.

        @mentions are best-effort on both paths: the proactive template
        carries no structured ``at`` payload the way the webhook does, so
        mention tokens are prepended inline instead.
        """
        normalized = self._normalize_markdown(content[: self.MAX_MESSAGE_LENGTH])
        normalized = self._prepend_mention_tokens(normalized, at_payload or {})
        if not normalized.strip():
            return SendResult(success=False, error="Empty content; nothing to send")

        # Transport 1: AI Card. Reuse a live inbound context if we still
        # have one; otherwise synthesize a group-targeted message from
        # chat_id (a DingTalk cid... conversationId).
        card_result: Optional[SendResult] = None
        if self._card_template_id and self._card_sdk:
            current_message = self._message_contexts.get(chat_id)
            if current_message is None:
                current_message = SimpleNamespace(
                    conversation_id=chat_id,
                    # cid-group conversations use conversation_type "2"; a
                    # cid... id that is actually a DM still delivers via the
                    # group open-space fallback inside the card path, so
                    # defaulting to group is the safer guess post-restart.
                    conversation_type="2",
                    sender_staff_id="",
                    robot_code=self._robot_code,
                )
            card_result = await self._create_and_stream_card(
                chat_id, current_message, normalized,
                finalize=True,
                at_users=None,
            )
            if card_result and card_result.success:
                self._fire_custom_reactions(chat_id, emotion_names or [])
                if fire_final_reaction:
                    self._fire_done_reaction(chat_id)
                return card_result

        # Transport 2: robot-native sampleMarkdown (last resort).
        result = await self._send_robot_native_message(
            chat_id,
            msg_key="sampleMarkdown",
            msg_param={"title": "Hermes", "text": normalized},
            metadata=metadata,
        )
        if result.success:
            self._fire_custom_reactions(chat_id, emotion_names or [])
            if fire_final_reaction:
                self._fire_done_reaction(chat_id)
            return result
        # Neither transport worked — surface the more informative error.
        if card_result is not None and not card_result.success:
            return SendResult(
                success=False,
                error=(
                    f"proactive AI Card failed ({card_result.error}); "
                    f"robot-native fallback failed ({result.error})"
                ),
            )
        return result

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """DingTalk does not support typing indicators."""
        pass

    async def _upload_robot_media(self, file_path: str, media_type: str) -> SendResult:
        """Upload a local file to DingTalk's robot media endpoint.

        DingTalk robot native media messages require a temporary ``media_id``.
        The official robot message types reference this value for
        ``sampleImageMsg`` / ``sampleAudio`` / ``sampleVideo`` /
        ``sampleFile`` payloads.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {file_path}")
        if media_type not in {"image", "file", "voice", "video"}:
            return SendResult(success=False, error=f"Unsupported DingTalk media type: {media_type}")
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="DingTalk access token unavailable")

        # DingTalk's robot media upload rejects browser-renderable MIME types
        # such as text/html with errcode 40005, even though the same bytes are
        # accepted as a generic file. Preserve the filename/fileType in the
        # subsequent sampleFile message, but upload file attachments as opaque
        # bytes so valid extensions are not blocked by Content-Type sniffing.
        if media_type == "file":
            mime_type = "application/octet-stream"
        else:
            mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            with path.open("rb") as fh:
                response = await self._http_client.post(
                    _DINGTALK_MEDIA_UPLOAD_URL,
                    params={"access_token": token, "type": media_type},
                    files={"media": (path.name, fh, mime_type)},
                    timeout=60.0,
                )
            try:
                body = response.json()
            except Exception:
                body = {}
            if response.status_code >= 300:
                return SendResult(
                    success=False,
                    error=f"DingTalk media upload failed HTTP {response.status_code}: {response.text[:200]}",
                )
            errcode = body.get("errcode", 0)
            if errcode not in (0, "0", None):
                errmsg = body.get("errmsg") or body.get("message") or "unknown error"
                return SendResult(
                    success=False,
                    error=f"DingTalk media upload failed: {errcode} {errmsg}",
                    raw_response=body,
                )
            media_id = body.get("media_id") or body.get("mediaId")
            if not media_id:
                return SendResult(
                    success=False,
                    error="DingTalk media upload failed: missing media_id",
                    raw_response=body,
                )
            logger.info(
                "[%s] DingTalk media uploaded: type=%s file=%s",
                self.name,
                media_type,
                path.name,
            )
            return SendResult(success=True, message_id=str(media_id), raw_response=body)
        except Exception as exc:
            logger.warning("[%s] DingTalk media upload failed: %s", self.name, exc)
            return SendResult(success=False, error=f"DingTalk media upload failed: {exc}")

    async def _send_robot_native_message(
        self,
        chat_id: str,
        msg_key: str,
        msg_param: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a DingTalk native robot message via OpenAPI."""
        metadata = metadata or {}
        if not self._robot_sdk or not dingtalk_robot_models or not tea_util_models:
            return SendResult(success=False, error="DingTalk robot SDK is unavailable")

        current_message = self._message_contexts.get(chat_id)
        open_conversation_id = (
            metadata.get("dingtalk_open_conversation_id")
            or metadata.get("open_conversation_id")
            # Match the dingtalk_stream SDK's own proactive path, which uses
            # incoming_message.conversation_id as openConversationId. chat_id
            # is normally the cid conversationId already, but fall through to
            # the cached inbound message's conversation_id when it isn't.
            or getattr(current_message, "conversation_id", None)
            or chat_id
        )
        if not open_conversation_id:
            return SendResult(success=False, error="DingTalk openConversationId is unavailable")

        robot_code = metadata.get("dingtalk_robot_code")
        robot_code_source = "metadata.dingtalk_robot_code"
        if not robot_code:
            robot_code = metadata.get("robot_code")
            robot_code_source = "metadata.robot_code"
        if not robot_code:
            robot_code = self._robot_code
            robot_code_source = "config.robot_code"
        if not robot_code:
            robot_code = getattr(current_message, "robot_code", None)
            robot_code_source = "current_message.robot_code"
        if not robot_code:
            return SendResult(success=False, error="DingTalk robotCode is unavailable")

        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="DingTalk access token unavailable")

        conversation_type = (
            metadata.get("dingtalk_conversation_type")
            or metadata.get("conversation_type")
            or getattr(current_message, "conversation_type", None)
        )
        sender_staff_id = (
            metadata.get("dingtalk_sender_staff_id")
            or metadata.get("sender_staff_id")
            or getattr(current_message, "sender_staff_id", None)
        )
        msg_param_json = json.dumps(msg_param, ensure_ascii=False)
        runtime = tea_util_models.RuntimeOptions()
        send_route = "org_group_send"
        requested_route = (
            metadata.get("dingtalk_send_route")
            or metadata.get("send_route")
            or ""
        )
        requested_route_lc = str(requested_route).lower()
        cool_app_code = (
            metadata.get("dingtalk_app_code")
            or metadata.get("app_code")
            or self._app_code
            or None
        )
        try:
            if (
                (
                    requested_route_lc in {"", "batch_send_oto", "batch_oto", "oto"}
                    and requested_route_lc not in {
                        "private_chat_send",
                        "private_chat",
                        "private",
                    }
                )
                and str(conversation_type) == "1"
                and sender_staff_id
                and hasattr(dingtalk_robot_models, "BatchSendOTORequest")
                and hasattr(self._robot_sdk, "batch_send_otowith_options_async")
            ):
                send_route = "batch_send_oto"
                request = dingtalk_robot_models.BatchSendOTORequest(
                    msg_key=msg_key,
                    msg_param=msg_param_json,
                    robot_code=str(robot_code),
                    user_ids=[str(sender_staff_id)],
                )
                headers = dingtalk_robot_models.BatchSendOTOHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                response = await self._robot_sdk.batch_send_otowith_options_async(
                    request, headers, runtime
                )
            elif str(conversation_type) == "1":
                send_route = "private_chat_send"
                request = dingtalk_robot_models.PrivateChatSendRequest(
                    cool_app_code=str(cool_app_code) if cool_app_code else None,
                    msg_key=msg_key,
                    msg_param=msg_param_json,
                    open_conversation_id=str(open_conversation_id),
                    robot_code=str(robot_code),
                )
                headers = dingtalk_robot_models.PrivateChatSendHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                response = await self._robot_sdk.private_chat_send_with_options_async(
                    request, headers, runtime
                )
            else:
                request = dingtalk_robot_models.OrgGroupSendRequest(
                    msg_key=msg_key,
                    msg_param=msg_param_json,
                    open_conversation_id=str(open_conversation_id),
                    robot_code=str(robot_code),
                )
                headers = dingtalk_robot_models.OrgGroupSendHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                response = await self._robot_sdk.org_group_send_with_options_async(
                    request, headers, runtime
                )
            body = getattr(response, "body", None)
            invalid_staff_ids = getattr(body, "invalid_staff_id_list", None) or []
            if invalid_staff_ids:
                logger.warning(
                    "[%s] DingTalk native robot message rejected invalid OTO staff IDs: %s "
                    "(msg_key=%s chat=%s route=%s)",
                    self.name,
                    invalid_staff_ids,
                    msg_key,
                    str(open_conversation_id)[:20],
                    send_route,
                )
                return SendResult(
                    success=False,
                    error=f"DingTalk OTO send invalid staff IDs: {invalid_staff_ids}",
                    raw_response=response,
                )
            process_query_key = getattr(body, "process_query_key", None) or uuid.uuid4().hex[:12]
            logger.info(
                "[%s] DingTalk native robot message sent: msg_key=%s chat=%s route=%s",
                self.name,
                msg_key,
                str(open_conversation_id)[:20],
                send_route,
            )
            return SendResult(
                success=True,
                message_id=str(process_query_key),
                raw_response=response,
            )
        except Exception as exc:
            logger.warning(
                "[%s] DingTalk native robot message failed: %s "
                "(msg_key=%s chat=%s conversation_type=%s route=%s robot_code_source=%s sender_staff_id=%s)",
                self.name,
                exc,
                msg_key,
                str(open_conversation_id)[:20],
                conversation_type,
                send_route,
                robot_code_source,
                bool(sender_staff_id),
            )
            return SendResult(success=False, error=f"DingTalk native send failed: {exc}")

    @staticmethod
    def _image_card_param_map(media_id: str, caption: Optional[str]) -> Dict[str, str]:
        content = caption or ""
        order = [
            "msgTitle",
            "msgContent",
            "staticMsgContent",
            "msgImages",
            "msgButtons",
        ]
        return {
            "msgTitle": "Hermes",
            "msgContent": content,
            "staticMsgContent": content,
            "flowStatus": "2",
            "sys_full_json_obj": json.dumps(
                {
                    "order": order,
                    "msgImages": [media_id],
                },
                ensure_ascii=False,
            ),
        }

    async def _send_robot_card_1_0_image(
        self,
        chat_id: str,
        media_id: str,
        *,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an uploaded image through DingTalk's card_1_0 delivery path.

        The robot native ``BatchSendOTO`` and ``PrivateChatSend`` endpoints are
        not interchangeable with Stream bot DMs.  The same card_1_0
        create+deliver path used for text AI Cards works for DM images when
        ``msgImages`` is supplied on the default AI card template.
        """
        metadata = metadata or {}
        if not self._card_sdk or not dingtalk_card_models or not tea_util_models:
            return SendResult(success=False, error="DingTalk card SDK is unavailable")

        current_message = self._message_contexts.get(chat_id)
        open_conversation_id = (
            metadata.get("dingtalk_open_conversation_id")
            or metadata.get("open_conversation_id")
            or chat_id
        )
        conversation_type = (
            metadata.get("dingtalk_conversation_type")
            or metadata.get("conversation_type")
            or getattr(current_message, "conversation_type", None)
        )
        sender_staff_id = (
            metadata.get("dingtalk_sender_staff_id")
            or metadata.get("sender_staff_id")
            or getattr(current_message, "sender_staff_id", None)
        )

        robot_code = (
            metadata.get("dingtalk_robot_code")
            or metadata.get("robot_code")
            or self._robot_code
            or getattr(current_message, "robot_code", None)
        )
        if not robot_code:
            return SendResult(success=False, error="DingTalk robotCode is unavailable")

        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="DingTalk access token unavailable")

        out_track_id = f"hermes_img_{uuid.uuid4().hex[:12]}"
        runtime = tea_util_models.RuntimeOptions()
        route = "card_1_0_image"
        try:
            create_request = dingtalk_card_models.CreateCardRequest(
                card_template_id=DEFAULT_AI_CARD_TEMPLATE_ID,
                out_track_id=out_track_id,
                card_data=dingtalk_card_models.CreateCardRequestCardData(
                    card_param_map=self._image_card_param_map(media_id, caption),
                ),
                callback_type="STREAM",
                im_group_open_space_model=(
                    dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(
                        support_forward=True,
                    )
                ),
                im_robot_open_space_model=(
                    dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(
                        support_forward=True,
                    )
                ),
            )
            create_headers = dingtalk_card_models.CreateCardHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await self._card_sdk.create_card_with_options_async(
                create_request, create_headers, runtime
            )

            route = "card_1_0_image_group"
            if str(conversation_type) == "1":
                if not sender_staff_id:
                    return SendResult(success=False, error="DingTalk sender_staff_id is unavailable")
                route = "card_1_0_image_single"
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=f"dtv1.card//IM_ROBOT.{sender_staff_id}",
                    im_robot_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(
                            space_type="IM_ROBOT",
                        )
                    ),
                )
            else:
                if not open_conversation_id:
                    return SendResult(success=False, error="DingTalk openConversationId is unavailable")
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=f"dtv1.card//IM_GROUP.{open_conversation_id}",
                    im_group_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(
                            robot_code=str(robot_code),
                        )
                    ),
                )
            deliver_headers = dingtalk_card_models.DeliverCardHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await self._card_sdk.deliver_card_with_options_async(
                deliver_request, deliver_headers, runtime
            )

            logger.info(
                "[%s] DingTalk card_1_0 image sent: chat=%s route=%s",
                self.name,
                str(open_conversation_id)[:20],
                route,
            )
            return SendResult(
                success=True,
                message_id=out_track_id,
            )
        except Exception as exc:
            logger.warning(
                "[%s] DingTalk card_1_0 image failed: %s "
                "(chat=%s conversation_type=%s route=%s sender_staff_id=%s)",
                self.name,
                exc,
                str(open_conversation_id)[:20],
                conversation_type,
                route,
                bool(sender_staff_id),
            )
            return SendResult(success=False, error=f"DingTalk card_1_0 image failed: {exc}")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image via DingTalk markdown.

        DingTalk's session webhook only supports text/markdown payloads, not
        native image/file attachments. For remote image URLs, render the image
        inline with markdown so the user still sees the image. Local files need
        OpenAPI media upload and are handled separately.
        """
        image_block = f"![image]({image_url})"
        content = f"{caption}\n\n{image_block}" if caption else image_block
        return await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image as a native DingTalk robot image message."""
        if image_path.startswith(("http://", "https://")):
            return await self.send_image(
                chat_id=chat_id,
                image_url=image_path,
                caption=caption,
                reply_to=reply_to,
                metadata=metadata,
            )

        media_id = await self._upload_robot_media(image_path, media_type="image")
        if not media_id.success:
            return media_id

        current_message = self._message_contexts.get(chat_id)
        conversation_type = (
            (metadata or {}).get("dingtalk_conversation_type")
            or (metadata or {}).get("conversation_type")
            or getattr(current_message, "conversation_type", None)
        )
        if str(conversation_type) == "1":
            card_result = await self._send_robot_card_1_0_image(
                chat_id=chat_id,
                media_id=media_id.message_id,
                caption=caption,
                metadata=metadata,
            )
            if card_result.success:
                return card_result
            logger.warning(
                "[%s] DingTalk card_1_0 image send failed; "
                "falling back to native image message: %s",
                self.name,
                card_result.error,
            )

        if caption:
            await self.send(
                chat_id=chat_id,
                content=caption,
                reply_to=reply_to,
                metadata=metadata,
            )

        native_result = await self._send_robot_native_message(
            chat_id=chat_id,
            msg_key="sampleImageMsg",
            msg_param={"photoURL": media_id.message_id},
            metadata=metadata,
        )
        if native_result.success:
            return native_result

        logger.warning(
            "[%s] DingTalk native image send failed; retrying image as file: %s",
            self.name,
            native_result.error,
        )
        return await self.send_document(
            chat_id=chat_id,
            file_path=image_path,
            file_name=os.path.basename(image_path) or "image",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local file as a native DingTalk robot file message."""
        if file_path.startswith(("http://", "https://")):
            label = file_name or os.path.basename(file_path) or "file"
            link = f"[{label}]({file_path})"
            content = f"{caption}\n\n{link}" if caption else link
            return await self.send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )

        path = Path(file_path)
        display_name = file_name or path.name or "file"
        media_id = await self._upload_robot_media(str(path), media_type="file")
        if not media_id.success:
            return media_id

        if caption:
            await self.send(
                chat_id=chat_id,
                content=caption,
                reply_to=reply_to,
                metadata=metadata,
            )

        file_type = path.suffix.lstrip(".").lower() or "file"
        return await self._send_robot_native_message(
            chat_id=chat_id,
            msg_key="sampleFile",
            msg_param={
                "mediaId": media_id.message_id,
                "fileName": display_name,
                "fileType": file_type,
            },
            metadata=metadata,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send local MP4 as native DingTalk video, falling back to file."""
        if video_path.startswith(("http://", "https://")):
            label = os.path.basename(video_path) or "video"
            link = f"[{label}]({video_path})"
            content = f"{caption}\n\n{link}" if caption else link
            return await self.send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )

        path = Path(video_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {video_path}")

        metadata = metadata or {}
        ext = path.suffix.lstrip(".").lower()
        if ext in _DINGTALK_NATIVE_VIDEO_EXTS:
            if not self._looks_like_mp4(path):
                return await self.send_document(
                    chat_id=chat_id,
                    file_path=video_path,
                    caption=caption,
                    file_name=os.path.basename(video_path) or "video",
                    reply_to=reply_to,
                    metadata=metadata,
                )

            cover_path = self._metadata_path(
                metadata,
                "dingtalk_video_cover_path",
                "video_cover_path",
                "thumbnail_path",
            )
            generated_cover = False
            if not cover_path:
                cover_path = await asyncio.to_thread(self._generate_video_cover, path)
                generated_cover = bool(cover_path)
            if not cover_path:
                cover_path = self._write_default_video_cover()
                generated_cover = bool(cover_path)

            if cover_path:
                try:
                    video_media = await self._upload_robot_media(str(path), media_type="video")
                    cover_media = await self._upload_robot_media(str(cover_path), media_type="image")
                    if video_media.success and cover_media.success:
                        duration_ms = self._duration_ms_from_metadata(metadata)
                        if not duration_ms:
                            duration_ms = await asyncio.to_thread(self._probe_media_duration_ms, path)
                        duration_ms = duration_ms or 1000
                        native_result = await self._send_robot_native_message(
                            chat_id=chat_id,
                            msg_key="sampleVideo",
                            msg_param={
                                "videoMediaId": video_media.message_id,
                                "videoType": ext,
                                "picMediaId": cover_media.message_id,
                                "duration": str(duration_ms),
                            },
                            metadata=metadata,
                        )
                        if native_result.success:
                            if caption:
                                await self.send(
                                    chat_id=chat_id,
                                    content=caption,
                                    reply_to=reply_to,
                                    metadata=metadata,
                                )
                            return native_result
                finally:
                    if generated_cover:
                        try:
                            cover_path.unlink(missing_ok=True)
                        except Exception:
                            pass

        return await self.send_document(
            chat_id=chat_id,
            file_path=video_path,
            caption=caption,
            file_name=os.path.basename(video_path) or "video",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send OGG/AMR as native DingTalk audio, falling back to file."""
        if audio_path.startswith(("http://", "https://")):
            label = os.path.basename(audio_path) or "audio"
            link = f"[{label}]({audio_path})"
            content = f"{caption}\n\n{link}" if caption else link
            return await self.send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )

        path = Path(audio_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {audio_path}")

        metadata = metadata or {}
        ext = path.suffix.lstrip(".").lower()
        if ext in _DINGTALK_NATIVE_AUDIO_EXTS:
            if not self._looks_like_native_audio(path, ext):
                return await self.send_document(
                    chat_id=chat_id,
                    file_path=audio_path,
                    caption=caption,
                    file_name=os.path.basename(audio_path) or "audio",
                    reply_to=reply_to,
                    metadata=metadata,
                )

            audio_media = await self._upload_robot_media(str(path), media_type="voice")
            if audio_media.success:
                duration_ms = self._duration_ms_from_metadata(metadata)
                if not duration_ms:
                    duration_ms = await asyncio.to_thread(self._probe_media_duration_ms, path)
                duration_ms = duration_ms or 1000
                native_result = await self._send_robot_native_message(
                    chat_id=chat_id,
                    msg_key="sampleAudio",
                    msg_param={
                        "mediaId": audio_media.message_id,
                        "duration": str(duration_ms),
                    },
                    metadata=metadata,
                )
                if native_result.success:
                    if caption:
                        await self.send(
                            chat_id=chat_id,
                            content=caption,
                            reply_to=reply_to,
                            metadata=metadata,
                        )
                    return native_result

        return await self.send_document(
            chat_id=chat_id,
            file_path=audio_path,
            caption=caption,
            file_name=os.path.basename(audio_path) or "audio",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a DingTalk conversation."""
        return {
            "name": chat_id,
            "type": "group" if "group" in chat_id.lower() else "dm",
        }

    def _get_valid_webhook(self, chat_id: str) -> Optional[tuple[str, int]]:
        """Get a valid (non-expired) session webhook for the given chat_id."""
        info = self._session_webhooks.get(chat_id)
        if not info:
            return None
        webhook, expired_time_ms = info
        # Check expiry with 5-minute safety margin
        if expired_time_ms and expired_time_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            safety_margin_ms = 5 * 60 * 1000
            if now_ms + safety_margin_ms >= expired_time_ms:
                # Expired, remove from cache
                self._session_webhooks.pop(chat_id, None)
                return None
        return info

    async def _create_and_stream_card(
        self,
        chat_id: str,
        message: Any,
        content: str,
        *,
        finalize: bool = True,
        at_users: Optional[Dict[str, str]] = None,
    ) -> Optional[SendResult]:
        """Create an AI Card, deliver it to the conversation, and stream initial content.

        ``send()`` decides ``finalize`` based on ``metadata.expect_edits``:

        - ``expect_edits=True`` (turn-status card / streaming preview)
          uses ``finalize=False`` so later ``edit_message`` calls update
          the same card without reopening it via ``streaming_update``.
          The card is tracked in ``_streaming_cards`` so the next send
          can close it as a sibling and ``edit_message(finalize=True)``
          can close it explicitly.
        - Plain sends (no ``expect_edits``) use ``finalize=True`` for a
          one-shot closed card and skip sibling tracking.
        """
        try:
            token = await self._get_access_token()
            if not token:
                return None

            out_track_id = f"hermes_{uuid.uuid4().hex[:12]}"

            conversation_id = getattr(message, "conversation_id", "") or ""
            conversation_type = getattr(message, "conversation_type", "1")
            is_group = str(conversation_type) == "2"
            sender_staff_id = getattr(message, "sender_staff_id", "") or ""

            runtime = tea_util_models.RuntimeOptions()

            # Step 1: Create card with STREAM callback type
            create_kwargs: Dict[str, Any] = {
                "card_template_id": self._card_template_id,
                "out_track_id": out_track_id,
                "card_data": dingtalk_card_models.CreateCardRequestCardData(
                    card_param_map=self._card_initial_param_map(),
                ),
                "callback_type": "STREAM",
                "im_group_open_space_model": (
                    dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(
                        support_forward=True,
                    )
                ),
                "im_robot_open_space_model": (
                    dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(
                        support_forward=True,
                    )
                ),
            }
            if at_users:
                create_kwargs["card_at_user_ids"] = list(at_users.keys())
            create_request = dingtalk_card_models.CreateCardRequest(
                **create_kwargs,
            )

            create_headers = dingtalk_card_models.CreateCardHeaders(
                x_acs_dingtalk_access_token=token,
            )

            await self._card_sdk.create_card_with_options_async(
                create_request, create_headers, runtime
            )

            # Step 2: Deliver card to the conversation
            if is_group:
                open_space_id = f"dtv1.card//IM_GROUP.{conversation_id}"
                deliver_model_kwargs: Dict[str, Any] = {
                    "robot_code": self._robot_code,
                }
                if at_users:
                    deliver_model_kwargs["at_user_ids"] = at_users
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=open_space_id,
                    im_group_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(
                            **deliver_model_kwargs,
                        )
                    ),
                )
            else:
                if not sender_staff_id:
                    logger.warning(
                        "[%s] AI Card skipped: missing sender_staff_id for DM",
                        self.name,
                    )
                    return None
                open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
                deliver_request = dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=open_space_id,
                    im_robot_open_deliver_model=(
                        dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(
                            space_type="IM_ROBOT",
                        )
                    ),
                )

            deliver_headers = dingtalk_card_models.DeliverCardHeaders(
                x_acs_dingtalk_access_token=token,
            )

            await self._card_sdk.deliver_card_with_options_async(
                deliver_request, deliver_headers, runtime
            )

            # Step 3: Stream initial content.  finalize=True closes the
            # card immediately (one-shot); finalize=False keeps it open
            # for streaming edit_message updates by out_track_id.
            await self._stream_card_content(
                out_track_id, token, content, finalize=finalize,
            )

            logger.info(
                "[%s] AI Card %s: %s",
                self.name,
                "created+finalized" if finalize else "created (streaming)",
                out_track_id,
            )
            return SendResult(success=True, message_id=out_track_id)

        except Exception as e:
            logger.warning(
                "[%s] AI Card lifecycle failed: %s\n%s",
                self.name, e, traceback.format_exc(),
            )
            return None
    # ------------------------------------------------------------------
    # Cross-adapter gateway contract: button-based approval / update-confirm
    # ------------------------------------------------------------------

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "SendResult":
        """Send a formatted approval-request card for a dangerous command.

        DingTalk does not yet support interactive callback buttons on AI Cards,
        so we fall back to a richly-formatted AI Card that clearly shows the
        command and the available text responses (approve / deny).  The gateway's
        existing plain-text approval resolver handles the user's reply.

        Reply keywords accepted by the gateway:
          approve / yes / ok / okay / confirm / y / 👍  → execute
          approve session                               → allow for this session
          approve always                                → allow permanently
          deny                                          → cancel
        """
        cmd_preview = command[:400] + "…" if len(command) > 400 else command
        msg = (
            f"⚠️ **危险命令请求授权** | Dangerous Command Approval\n\n"
            f"**原因 / Reason:** {description}\n\n"
            f"```\n{cmd_preview}\n```\n\n"
            f"| 操作 | 回复内容 |\n"
            f"|------|----------|\n"
            f"| ✅ 执行一次 / Approve once | `approve` |\n"
            f"| 🔁 本次会话全部允许 / Allow for session | `approve session` |\n"
            f"| 🌐 永久允许 / Allow always | `approve always` |\n"
            f"| ❌ 拒绝 / Deny | `deny` |\n\n"
            f"或直接回复 `👍` 执行。"
        )
        return await self.send(chat_id, msg, metadata=metadata)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit an AI Card by streaming updated content.

        ``message_id`` is the out_track_id returned by the initial ``send()``
        call that created this card.  Callers (stream_consumer, tool
        progress) track their own ids independently so two parallel flows
        on the same chat_id don't interfere.
        """
        if not message_id:
            return SendResult(success=False, error="message_id required")
        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            await self._stream_card_content(
                message_id, token, content, finalize=finalize,
            )
            if finalize:
                # Remove from streaming-cards tracking and fire Done.  This
                # is the canonical "response ended" signal from stream
                # consumer's final edit.
                self._streaming_cards.get(chat_id, {}).pop(message_id, None)
                if not self._streaming_cards.get(chat_id):
                    self._streaming_cards.pop(chat_id, None)
                logger.info(
                    "[%s] AI Card finalized (edit): %s",
                    self.name, message_id,
                )
                self._fire_done_reaction(chat_id)
            else:
                # Non-final edit reopens the card into streaming state —
                # track it so the next send() can auto-close it as a
                # sibling.
                self._streaming_cards.setdefault(chat_id, {})[message_id] = content
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.warning("[%s] Card edit failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _stream_card_content(
        self,
        out_track_id: str,
        token: str,
        content: str,
        finalize: bool = False,
    ) -> None:
        """Stream content to an existing AI Card."""
        self._card_content_key = self._current_card_content_key()
        stream_request = dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id,
            guid=str(uuid.uuid4()),
            key=self._card_content_key,
            content=content[: self.MAX_MESSAGE_LENGTH],
            is_full=True,
            is_finalize=finalize,
            is_error=False,
        )

        stream_headers = dingtalk_card_models.StreamingUpdateHeaders(
            x_acs_dingtalk_access_token=token,
        )

        runtime = tea_util_models.RuntimeOptions()
        await self._card_sdk.streaming_update_with_options_async(
            stream_request, stream_headers, runtime
        )

    async def _get_access_token(self) -> Optional[str]:
        """Get access token using SDK's cached token."""
        if not self._stream_client:
            return None
        try:
            # SDK's get_access_token is sync and uses requests
            token = await asyncio.to_thread(self._stream_client.get_access_token)
            return token
        except Exception as e:
            logger.error("[%s] Failed to get access token: %s", self.name, e)
            return None

    async def _send_emotion(
        self,
        open_msg_id: str,
        open_conversation_id: str,
        emoji_name: str,
        *,
        recall: bool = False,
    ) -> None:
        """Add or recall an emoji reaction on a message."""
        if not self._robot_sdk or not open_msg_id or not open_conversation_id:
            return
        action = "recall" if recall else "reply"
        try:
            token = await self._get_access_token()
            if not token:
                return

            emotion_kwargs = {
                "robot_code": self._robot_code,
                "open_msg_id": open_msg_id,
                "open_conversation_id": open_conversation_id,
                "emotion_type": 2,
                "emotion_name": emoji_name,
            }
            runtime = tea_util_models.RuntimeOptions()

            if recall:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotRecallEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotRecallEmotionRequest(
                    **emotion_kwargs,
                )
                sdk_headers = dingtalk_robot_models.RobotRecallEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                await self._robot_sdk.robot_recall_emotion_with_options_async(
                    request, sdk_headers, runtime
                )
            else:
                emotion_kwargs["text_emotion"] = (
                    dingtalk_robot_models.RobotReplyEmotionRequestTextEmotion(
                        emotion_id="2659900",
                        emotion_name=emoji_name,
                        text=emoji_name,
                        background_id="im_bg_1",
                    )
                )
                request = dingtalk_robot_models.RobotReplyEmotionRequest(
                    **emotion_kwargs,
                )
                sdk_headers = dingtalk_robot_models.RobotReplyEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                await self._robot_sdk.robot_reply_emotion_with_options_async(
                    request, sdk_headers, runtime
                )
            logger.info(
                "[%s] _send_emotion: %s %s on msg=%s",
                self.name, action, emoji_name, open_msg_id[:24],
            )
        except Exception:
            logger.debug(
                "[%s] _send_emotion %s failed", self.name, action, exc_info=True
            )

    async def _resolve_media_codes(self, message: "ChatbotMessage") -> None:
        """Resolve DingTalk download codes to local cached file paths."""
        robot_code = getattr(message, "robot_code", None) or self._client_id
        codes_to_resolve = []

        # Collect codes and references to update
        # 1. Single image content
        img_content = getattr(message, "image_content", None)
        if img_content:
            media_ref, key, is_code = self._first_media_ref(img_content)
            if media_ref and key:
                codes_to_resolve.append((img_content, key, "image", None, is_code))

        # 2. Rich text list
        for item in self._iter_rich_text_items(message):
            media_ref, key, is_code = self._first_media_ref(item)
            if media_ref and key:
                item_type = self._rich_item_type(item)
                mapped = DINGTALK_TYPE_MAPPING.get(item_type, "file")
                filename = self._rich_item_filename(item)
                codes_to_resolve.append((item, key, mapped, filename, is_code))

        # 3. File/image message (msgtype='file' or 'image', codes in extensions)
        msg_type_str = getattr(message, "message_type", "") or ""
        if msg_type_str in ("file", "image"):
            extensions = getattr(message, "extensions", {}) or {}
            ext_content = extensions.get("content", {})
            if isinstance(ext_content, dict) and ext_content.get("downloadCode"):
                codes_to_resolve.append((ext_content, "downloadCode"))

        if not codes_to_resolve:
            return

        # Resolve all codes in parallel
        tasks = []
        token: Optional[str] = None
        for obj, key, mapped, filename, is_code in codes_to_resolve:
            code = self._media_get(obj, key)
            if not code:
                continue
            code = str(code)
            if is_code:
                if token is None:
                    token = await self._get_access_token()
                if not token:
                    self._set_media_error(
                        obj,
                        "DingTalk media download failed: access token unavailable.",
                    )
                    continue
                tasks.append(
                    self._fetch_download_url(
                        code,
                        robot_code,
                        token,
                        obj,
                        key,
                        mapped=mapped,
                        filename=filename,
                    )
                )
            else:
                tasks.append(
                    self._cache_resolved_media_url(
                        code,
                        obj,
                        key,
                        mapped=mapped,
                        filename=filename,
                    )
                )

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_download_url(
        self,
        code: str,
        robot_code: str,
        token: str,
        obj,
        key: str,
        mapped: str = "file",
        filename: Optional[str] = None,
    ) -> None:
        """Fetch and cache one DingTalk media item using the robot SDK."""
        if not self._robot_sdk:
            self._set_media_error(
                obj,
                "DingTalk media download failed: robot SDK is unavailable.",
            )
            logger.warning(
                "[%s] Robot SDK not initialized, cannot resolve media code",
                self.name,
            )
            return
        try:
            request = dingtalk_robot_models.RobotMessageFileDownloadRequest(
                download_code=code,
                robot_code=robot_code,
            )
            headers = dingtalk_robot_models.RobotMessageFileDownloadHeaders(
                x_acs_dingtalk_access_token=token,
            )
            runtime = tea_util_models.RuntimeOptions()
            response = await self._robot_sdk.robot_message_file_download_with_options_async(
                request, headers, runtime
            )
            body = response.body if response else None
            if body:
                url = getattr(body, "download_url", None)
                if url:
                    await self._cache_resolved_media_url(
                        str(url),
                        obj,
                        key,
                        mapped=mapped,
                        filename=filename,
                    )
            else:
                self._set_media_error(
                    obj,
                    "DingTalk media download failed: empty download URL response.",
                )
                logger.warning(
                    "[%s] Failed to download media: empty response for code %s",
                    self.name,
                    code,
                )
        except Exception as e:
            self._set_media_error(
                obj,
                f"DingTalk media download failed before cache: {e}",
            )
            logger.error("[%s] Error resolving media code %s: %s", self.name, code, e)

    async def _cache_resolved_media_url(
        self,
        url: str,
        obj: Any,
        key: str,
        mapped: str,
        filename: Optional[str] = None,
    ) -> None:
        """Cache a resolved DingTalk media URL and mutate the source object."""
        try:
            path, media_type = await self._cache_media_url(url, mapped, filename)
        except Exception as exc:
            self._set_media_error(
                obj,
                f"DingTalk media download failed: {exc}",
            )
            logger.warning(
                "[%s] Failed to cache DingTalk media %s; skipping media routing: %s",
                self.name,
                safe_url_for_log(url),
                exc,
            )
            return
        self._set_cached_media_ref(obj, key, path, media_type, filename)

    async def _cache_media_url(
        self,
        url: str,
        mapped: str,
        filename: Optional[str] = None,
    ) -> tuple[str, str]:
        """Download a media URL into the existing Hermes media caches."""
        if not HTTPX_AVAILABLE or httpx is None:
            raise RuntimeError("httpx is required to download DingTalk media")

        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(
                f"Blocked unsafe DingTalk media URL: {safe_url_for_log(url)}"
            )

        accept = {
            "image": "image/*,*/*;q=0.8",
            "audio": "audio/*,*/*;q=0.8",
            "video": "video/*,*/*;q=0.8",
        }.get(mapped, "application/octet-stream,*/*;q=0.8")
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            event_hooks={"response": [_ssrf_redirect_guard]},
            trust_env=False,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                    "Accept": accept,
                },
            )
            response.raise_for_status()

        response_type = response.headers.get("content-type", "")
        response_type = response_type.split(";", 1)[0].strip().lower()
        media_type = response_type or self._default_media_type(mapped, filename)
        if media_type == "application/octet-stream" and filename:
            media_type = self._default_media_type(mapped, filename)
        ext = self._extension_for_media(mapped, media_type, filename)

        if mapped == "image":
            return cache_image_from_bytes(response.content, ext), media_type
        if mapped == "audio":
            return cache_audio_from_bytes(response.content, ext), media_type
        if mapped == "video":
            return cache_video_from_bytes(response.content, ext), media_type

        doc_name = filename or f"dingtalk_attachment{ext}"
        return cache_document_from_bytes(response.content, doc_name), media_type

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        """Normalize markdown for DingTalk's parser.

        DingTalk's markdown renderer has quirks:
        - Numbered lists need blank line before them
        - Indented code blocks may render incorrectly
        """
        lines = text.split("\n")
        out = []
        for i, line in enumerate(lines):
            # Ensure blank line before numbered list items
            is_numbered = re.match(r"^\d+\.\s", line.strip())
            if is_numbered and i > 0:
                prev = lines[i - 1]
                if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                    out.append("")
            # Dedent fenced code blocks
            if line.strip().startswith("```") and line != line.lstrip():
                indent = len(line) - len(line.lstrip())
                line = line[indent:]
            out.append(line)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Internal stream handler
# ---------------------------------------------------------------------------


class _IncomingHandler(
    dingtalk_stream.ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object
):
    """dingtalk-stream ChatbotHandler that forwards messages to the adapter.

    SDK >= 0.20 changed process() from sync to async, and the message
    parameter from ChatbotMessage to CallbackMessage. We parse the
    CallbackMessage.data dict into a ChatbotMessage before forwarding.
    """

    def __init__(self, adapter: DingTalkAdapter, loop: Optional[asyncio.AbstractEventLoop] = None):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._adapter = adapter
        self._loop = loop

    def pre_start(self) -> None:
        """No-op pre-start hook required by dingtalk-stream SDK.

        The SDK calls ``pre_start()`` on every registered handler before
        opening the WebSocket connection.  Without this method, the SDK
        raises ``AttributeError: '_IncomingHandler' object has no
        attribute 'pre_start'`` and kills the stream connection.
        """
        return

    async def process(self, message: "CallbackMessage"):
        """Called by dingtalk-stream (>=0.20) when a message arrives.

        dingtalk-stream >= 0.24 passes a CallbackMessage whose ``.data`` contains
        the chatbot payload. Convert it to ChatbotMessage via
        ``ChatbotMessage.from_dict()``.

        Message processing is dispatched as a background task so that this
        method returns the ACK immediately — blocking here would prevent the
        SDK from sending heartbeats, eventually causing a disconnect.
        """
        try:
            # CallbackMessage.data is a dict containing the raw DingTalk payload
            data = message.data
            if isinstance(data, str):
                data = json.loads(data)

            # Parse dict into ChatbotMessage using SDK's from_dict
            chatbot_msg = ChatbotMessage.from_dict(data)

            # Ensure session_webhook is populated even if the SDK's
            # from_dict() did not map it (field name mismatch across
            # SDK versions).
            if not getattr(chatbot_msg, "session_webhook", None):
                webhook = (
                    data.get("sessionWebhook")
                    or data.get("session_webhook")
                    or ""
                ) if isinstance(data, dict) else ""
                if webhook:
                    chatbot_msg.session_webhook = webhook

            # Ensure is_in_at_list is populated from the structured callback
            # flag even if from_dict() did not map it.  DingTalk sends
            # ``isInAtList`` in the raw payload; the adapter's mention check
            # reads the ChatbotMessage attribute ``is_in_at_list``.
            if not getattr(chatbot_msg, "is_in_at_list", False):
                raw_flag = (
                    data.get("isInAtList") if isinstance(data, dict) else False
                )
                if raw_flag:
                    chatbot_msg.is_in_at_list = True

            # Some dingtalk-stream versions expose the raw callback fields but
            # do not map every camelCase payload key onto ChatbotMessage.
            # ``reply_at_sender`` depends on ``sender_staff_id`` because
            # DingTalk's session webhook and card delivery both @ users by
            # staff/user id, not by display name.
            if isinstance(data, dict):
                self._fill_missing_raw_fields(chatbot_msg, data)

            msg_id = getattr(chatbot_msg, "message_id", None) or ""
            conversation_id = getattr(chatbot_msg, "conversation_id", None) or ""

            # Thinking reaction — fire-and-forget, tracked.
            # Uses the adapter's reaction-label constants so the
            # inbound-side label and the later swap-out recall stay in
            # sync (otherwise the recall finds no matching reaction).
            if msg_id and conversation_id:
                self._adapter._spawn_bg(
                    self._adapter._send_emotion(
                        msg_id,
                        conversation_id,
                        self._adapter.REACTION_THINKING,
                        recall=False,
                    )
                )

            # Fire-and-forget: return ACK immediately, process in background.
            # Blocking here would prevent the SDK from sending heartbeats,
            # eventually causing a disconnect.  _on_message is wrapped so
            # exceptions inside the task surface in logs instead of
            # disappearing into the event loop.
            asyncio.create_task(self._safe_on_message(chatbot_msg))
        except Exception:
            logger.exception(
                "[%s] Error preparing incoming message", self._adapter.name
            )
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"

        return AckMessage.STATUS_OK, "OK"

    @staticmethod
    def _first_raw_value(data: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _fill_missing_raw_fields(cls, chatbot_msg: Any, data: Dict[str, Any]) -> None:
        """Backfill raw callback fields that SDK model mapping may miss."""

        field_map = {
            "message_id": ("msgId", "messageId", "message_id"),
            "conversation_id": ("conversationId", "conversation_id"),
            "conversation_type": ("conversationType", "conversation_type"),
            "sender_id": ("senderId", "sender_id"),
            "sender_staff_id": ("senderStaffId", "sender_staff_id"),
            "sender_nick": ("senderNick", "sender_nick"),
            "create_at": ("createAt", "create_at"),
            "robot_code": ("robotCode", "robot_code"),
            "chatbot_user_id": ("chatbotUserId", "chatbot_user_id"),
        }
        for attr, keys in field_map.items():
            if getattr(chatbot_msg, attr, None):
                continue
            value = cls._first_raw_value(data, *keys)
            if value is not None:
                setattr(chatbot_msg, attr, str(value))

    async def _safe_on_message(self, chatbot_msg: "ChatbotMessage") -> None:
        """Wrapper that catches exceptions from _on_message."""
        try:
            await self._adapter._on_message(chatbot_msg)
        except Exception:
            logger.exception(
                "[%s] Error processing incoming message", self._adapter.name
            )


# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the DingTalk adapter moved from gateway/platforms/dingtalk.py into
# this bundled plugin. Mirrors the Discord (#24356) / Slack migrations: a
# register(ctx) entry point plus hook implementations that replace the
# per-platform core touchpoints (the Platform.DINGTALK elif in gateway/run.py,
# the dingtalk_cfg YAML→env block + _PLATFORM_CONNECTED_CHECKERS entry in
# gateway/config.py, the _setup_dingtalk wizard + _PLATFORMS["dingtalk"] static
# dict in hermes_cli/gateway.py, and the _send_dingtalk dispatch in
# tools/send_message_tool.py).
# ──────────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process DingTalk delivery via a static robot webhook URL.

    Implements the standalone_sender_fn contract so deliver=dingtalk cron jobs
    succeed when cron runs separately from the gateway. The live adapter uses
    per-session webhook URLs from incoming messages, which aren't available
    out-of-process; this path uses the static DINGTALK_WEBHOOK_URL / extra
    webhook_url instead. Replaces the legacy _send_dingtalk helper.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}
    try:
        webhook_url = extra.get("webhook_url") or os.getenv("DINGTALK_WEBHOOK_URL", "")
        if not webhook_url:
            return {"error": "DingTalk not configured. Set DINGTALK_WEBHOOK_URL env var or webhook_url in dingtalk platform extra config."}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                webhook_url,
                json={"msgtype": "text", "text": {"content": message}},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode", 0) != 0:
                return {"error": f"DingTalk API error: {data.get('errmsg', 'unknown')}"}
        return {"success": True, "platform": "dingtalk", "chat_id": chat_id}
    except Exception as e:
        # Redact the access_token from webhook URLs that may appear in the
        # exception text. Reuse send_message_tool._error's redaction so the
        # logic stays single-sourced (lazy import avoids a circular at module
        # load). Falls back to a plain message if that helper is unavailable.
        try:
            from tools.send_message_tool import _error as _redact_error
            return _redact_error(f"DingTalk send failed: {e}")
        except Exception:
            return {"error": f"DingTalk send failed: {e}"}


def interactive_setup() -> None:
    """Configure DingTalk — QR scan (recommended) or manual credential entry.

    Replaces hermes_cli/setup.py-era _setup_dingtalk + the static
    _PLATFORMS["dingtalk"] dict in hermes_cli/gateway.py. CLI helpers are
    lazy-imported so the plugin's module-load surface stays minimal.
    """
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.setup import prompt_choice
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_header,
        print_success,
        print_warning,
    )

    print_header("DingTalk")
    existing = get_env_value("DINGTALK_CLIENT_ID")
    if existing:
        print_success(f"DingTalk is already configured (Client ID: {existing}).")
        if not prompt_yes_no("Reconfigure DingTalk?", False):
            return

    method = prompt_choice(
        "Choose setup method",
        [
            "QR Code Scan (Recommended, auto-obtain Client ID and Client Secret)",
            "Manual Input (Client ID and Client Secret)",
        ],
        default=0,
    )

    if method == 0:
        try:
            from hermes_cli.dingtalk_auth import dingtalk_qr_auth
        except ImportError as exc:
            print_warning(f"QR auth module failed to load ({exc}), falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        result = dingtalk_qr_auth()
        if result is None:
            print_warning("QR auth incomplete, falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        client_id, client_secret = result
        save_env_value("DINGTALK_CLIENT_ID", client_id)
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
        print_success("DingTalk configured via QR scan!")
    else:
        _manual_credential_entry(prompt, save_env_value, print_success)


def _manual_credential_entry(prompt, save_env_value, print_success) -> None:
    client_id = prompt("DingTalk Client ID (app key)")
    if not client_id:
        return
    save_env_value("DINGTALK_CLIENT_ID", client_id)
    client_secret = prompt("DingTalk Client Secret", password=True)
    if client_secret:
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
    print_success("DingTalk credentials saved")


def _apply_yaml_config(yaml_cfg: dict, dingtalk_cfg: dict) -> dict | None:
    """Translate config.yaml dingtalk: keys into DINGTALK_* env vars.

    Implements the apply_yaml_config_fn contract (#24849). Mirrors the legacy
    dingtalk_cfg block from gateway/config.py::load_gateway_config(). Env vars
    take precedence over YAML (each assignment guarded by not os.getenv(...)).
    Returns None — everything flows through env.
    """
    import json as _json
    if "require_mention" in dingtalk_cfg and not os.getenv("DINGTALK_REQUIRE_MENTION"):
        os.environ["DINGTALK_REQUIRE_MENTION"] = str(dingtalk_cfg["require_mention"]).lower()
    if "mention_patterns" in dingtalk_cfg and not os.getenv("DINGTALK_MENTION_PATTERNS"):
        os.environ["DINGTALK_MENTION_PATTERNS"] = _json.dumps(dingtalk_cfg["mention_patterns"])
    frc = dingtalk_cfg.get("free_response_chats")
    if frc is not None and not os.getenv("DINGTALK_FREE_RESPONSE_CHATS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["DINGTALK_FREE_RESPONSE_CHATS"] = str(frc)
    ac = dingtalk_cfg.get("allowed_chats")
    if ac is not None and not os.getenv("DINGTALK_ALLOWED_CHATS"):
        if isinstance(ac, list):
            ac = ",".join(str(v) for v in ac)
        os.environ["DINGTALK_ALLOWED_CHATS"] = str(ac)
    allowed = dingtalk_cfg.get("allowed_users")
    if allowed is None:
        # Fall back to the documented nested paths (#44928). The docs
        # (website/docs/user-guide/messaging/dingtalk.md) configure the
        # allowlist at gateway.platforms.dingtalk.extra.allowed_users; the
        # adapter reads it from PlatformConfig.extra, but gateway
        # authorization (_is_user_authorized in gateway/authz_mixin.py)
        # only consults DINGTALK_ALLOWED_USERS — without this bridge a
        # nested-only allowlist passes the adapter and is then denied at
        # the gateway. Check this block's own extra first (the dispatch
        # loop passes the platforms block here when no top-level
        # ``dingtalk:`` section exists), then both nested containers.
        _extra = dingtalk_cfg.get("extra")
        if isinstance(_extra, dict):
            allowed = _extra.get("allowed_users")
        if allowed is None:
            _gw = yaml_cfg.get("gateway")
            _gw_platforms = _gw.get("platforms") if isinstance(_gw, dict) else None
            for _container in (_gw_platforms, yaml_cfg.get("platforms")):
                if not isinstance(_container, dict):
                    continue
                _dt = _container.get("dingtalk")
                _dt_extra = _dt.get("extra") if isinstance(_dt, dict) else None
                if isinstance(_dt_extra, dict) and _dt_extra.get("allowed_users") is not None:
                    allowed = _dt_extra.get("allowed_users")
                    break
    if allowed is not None and not os.getenv("DINGTALK_ALLOWED_USERS"):
        if isinstance(allowed, list):
            allowed = ",".join(str(v) for v in allowed)
        os.environ["DINGTALK_ALLOWED_USERS"] = str(allowed)
    allow_all = dingtalk_cfg.get("allow_all_users")
    if allow_all is not None and not os.getenv("DINGTALK_ALLOW_ALL_USERS"):
        os.environ["DINGTALK_ALLOW_ALL_USERS"] = str(allow_all).lower()
    return None


def _is_connected(config) -> bool:
    """DingTalk is connected when client_id + client_secret are present.

    Mirrors the legacy _PLATFORM_CONNECTED_CHECKERS[Platform.DINGTALK] entry.
    Reads from PlatformConfig.extra first, then env vars.
    """
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID"))
        and (extra.get("client_secret") or _get_scoped_secret("DINGTALK_CLIENT_SECRET"))
    )


def _build_adapter(config):
    """Factory wrapper that constructs DingTalkAdapter from a PlatformConfig."""
    return DingTalkAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="dingtalk",
        label="DingTalk",
        adapter_factory=_build_adapter,
        check_fn=dingtalk_deps_present,
        ensure_deps_fn=ensure_dingtalk_deps,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
        install_hint="pip install 'dingtalk-stream>=0.20' httpx",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="DINGTALK_ALLOWED_USERS",
        allow_all_env="DINGTALK_ALLOW_ALL_USERS",
        cron_deliver_env_var="DINGTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        emoji="🐳",
        allow_update_command=True,
    )
