"""DingTalk platform adapter (Stream Mode via dingtalk-stream >=0.20; replies via session webhook markdown or AI Cards).
Requires ``pip install "dingtalk-stream>=0.20" httpx``. config.yaml ``platforms.dingtalk``: ``enabled``, group gating
(``require_mention``, ``free_response_chats``, ``mention_patterns``, ``allowed_users``/``allowed_chats``) and
``extra.client_id`` / ``extra.client_secret`` (or DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET)."""

import asyncio
import concurrent.futures
import json
import logging
import mimetypes
import os
import re
import time
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
from typing import Any, Dict, List, Optional, Set, Tuple

# Optional SDKs: catch broad Exception, not just ImportError — their transitive cryptography
# dependency can raise AttributeError on version skew; a broken optional SDK must degrade gracefully.
try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import CallbackMessage, AckMessage

    DINGTALK_STREAM_AVAILABLE = True
except Exception:  # noqa: BLE001
    DINGTALK_STREAM_AVAILABLE = False
    dingtalk_stream = ChatbotMessage = CallbackMessage = None  # type: ignore[assignment]
    AckMessage = type("AckMessage", (), {"STATUS_OK": 200, "STATUS_SYSTEM_EXCEPTION": 500})  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from alibabacloud_dingtalk.card_1_0 import client as dingtalk_card_client, models as dingtalk_card_models
    from alibabacloud_dingtalk.robot_1_0 import client as dingtalk_robot_client, models as dingtalk_robot_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    CARD_SDK_AVAILABLE = True
except Exception:
    CARD_SDK_AVAILABLE = False
    dingtalk_card_client = dingtalk_card_models = dingtalk_robot_client = dingtalk_robot_models = None
    open_api_models = tea_util_models = None

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator, compile_mention_patterns
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    _ssrf_redirect_guard,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    safe_url_for_log,
)
from gateway.platforms.event import MessageEvent
from utils import is_truthy_value
from gateway.platforms._shared import (
    apply_yaml_bridge as _apply_yaml_bridge, decode_json_list_literal as _decode_json_list_literal,
    extra_or_secret as _extra_or_secret, get_scoped_secret as _get_scoped_secret, send_error
)
from plugins.platforms.dingtalk.inbound import collect_download_codes, extract_media, extract_text, _rich_list


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
RECONNECT_CIRCUIT_BREAKER_TRIPS = 5  # identical errors escaping start() before the breaker trips
RECONNECT_CIRCUIT_BREAKER_DELAY = 300  # seconds between attempts while tripped (matches the runner's cap)
_SDK_LOG_REPEAT_WINDOW = 300.0  # identical dingtalk_stream.client records are collapsed within this window

# DingTalk media upload endpoint (robot native media messages need a temporary media_id).
_DINGTALK_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
# File extensions natively supported by DingTalk's sampleVideo / sampleAudio robot messages.
_DINGTALK_NATIVE_AUDIO_EXTS = {"ogg", "amr"}
_DINGTALK_NATIVE_VIDEO_EXTS = {"mp4"}
# SDK-proven default AI Card template + content key, used when config doesn't specify a custom one.
DEFAULT_AI_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"
DEFAULT_AI_CARD_CONTENT_KEY = "msgContent"
# Extract ``[[emotion:...]]``/``[[dingtalk:emotion=...]]`` tags from agent output so the adapter
# can fire them as DingTalk reactions instead of rendering them as text.
_DINGTALK_EMOTION_TAG_RE = re.compile(
    r"\[\[(?:dingtalk[:_-])?emotion\s*[:=]\s*([^\]]+?)\s*\]\]",
    re.IGNORECASE,
)

# Stream liveness watchdog. DingTalk Stream Mode connections can silently go "half-open": the TCP
# socket stays established and the SDK's ``async for raw_message in websocket`` blocks forever
# waiting for business frames that never arrive, while ``start()`` neither returns nor raises — so
# the adapter's own reconnect loop (``_run_stream``) never gets control. Observed in production
# 2026-08-12: a connection went silent for 4.5 days with zero exceptions until manual restart.
# The fix is an application-layer watchdog that actively pings the live websocket and force-closes
# it if the pong does not return within a timeout, which breaks the SDK's inner loop and triggers
# its (and our) reconnect path with a fresh ticket. A quiet-but-healthy connection returns the pong
# promptly, so this never churns a live socket. Tunable via env for ops without a redeploy.
STREAM_PING_INTERVAL = 60  # seconds between liveness pings
STREAM_PING_TIMEOUT = 20  # seconds to await the pong before declaring half-open


def _positive_int(raw: Any, default: int, *, minimum: int = 1) -> int:
    """``raw`` as an int of at least ``minimum``, else ``default`` (unset, blank or invalid)."""
    try:
        value = int(str(raw).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _is_sdk_incompat(exc: BaseException | None) -> bool:
    """True for #24851: ``websockets.connect`` is not an async CM for this dingtalk-stream.

    The SDK may surface it as the bare TypeError, or as an AttributeError (its ``except``
    clause touches the unloaded ``websockets.exceptions``) chained on that TypeError.
    """
    for _ in range(4):
        if exc is None:
            return False
        if isinstance(exc, TypeError) and "asynchronous context manager" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class _SdkLogGuard(logging.Filter):
    """Collapse repeated dingtalk_stream.client records; never raises on bad format args.

    dingtalk-stream 0.24.3 logs ``logger.exception('unknown exception', e)`` every 3 s from its
    retry loop — a malformed call that also triggers stdlib "--- Logging error ---" tracebacks.
    """

    def __init__(self, on_exception=None, window: float = _SDK_LOG_REPEAT_WINDOW):
        super().__init__()
        self._on_exception, self._window = on_exception, window
        self._seen: Dict[tuple, list] = {}  # key -> [last_emitted_monotonic, suppressed_count]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                msg = record.getMessage()
            except Exception:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                msg = " ".join(str(x) for x in (record.msg, *args))
                record.msg, record.args = msg, ()
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None and self._on_exception is not None:
                self._on_exception(exc)
            key = (record.levelno, msg[:500], type(exc).__name__ if exc is not None else None)
            now = time.monotonic()
            entry = self._seen.get(key)
            if entry is not None and now - entry[0] < self._window:
                entry[1] += 1
                return False
            if entry is not None and entry[1]:
                record.msg, record.args = f"{msg} (suppressed {entry[1]} identical repeats)", ()
            if len(self._seen) >= 256:
                self._seen.clear()
            self._seen[key] = [now, 0]
        except Exception:
            pass  # a logging filter must never break the caller
        return True

_SESSION_WEBHOOKS_MAX = 500
_DINGTALK_WEBHOOK_RE = re.compile(r'^https://(?:api|oapi)\.dingtalk\.com/')
_TRUTHY = {"true", "1", "yes", "on"}
_EMOTION_ID = "2659900"
_EMOTION_BG = "im_bg_1"
# recall? -> (TextEmotion model, Request model, Headers model, robot SDK method), resolved on ``dingtalk_robot_models`` at call time.
_EMOTION_SDK = {recall: (f"Robot{v}EmotionRequestTextEmotion", f"Robot{v}EmotionRequest", f"Robot{v}EmotionHeaders", f"robot_{v.lower()}_emotion_with_options_async")
                for recall, v in ((True, "Recall"), (False, "Reply"))}
_NUMBERED_RE = re.compile(r"^\d+\.\s")
_NO_LOCAL_UPLOAD = "DingTalk session webhook replies do not support local %s. Only markdown/text replies are supported without OpenAPI %s."


def _csv_set(raw: Any) -> Set[str]:
    """Split a list, JSON-list string or comma-separated string into a set of stripped, non-empty items."""
    raw = _decode_json_list_literal(raw)
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    return {str(part).strip() for part in parts if str(part).strip()}


def dingtalk_deps_present() -> bool:
    """PASSIVE registry ``check_fn`` — must never install; credentials are gated separately.

    Registry ``check_fn`` — called from status displays and config loading, so it must never install
    anything. The ACTIVE lazy-installer (``check_dingtalk_requirements``) is registered as
    ``ensure_deps_fn`` and runs from ``create_adapter()`` when this returns False (#79812).
    """
    return DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE


def ensure_dingtalk_deps() -> bool:
    """ACTIVE deps-only installer (registry ``ensure_deps_fn``); rebinds module globals. Deliberately does NOT
    check credentials: an ``extra``-configured platform would otherwise be vetoed before ever installing (deadlock).

    Lazy-installs dingtalk-stream/httpx and rebinds module globals. Deliberately does NOT check credentials
    — ``ensure_deps_fn``'s contract is deps-only ("Returns True once deps are importable"); credentials are
    gated by ``is_connected``/``validate_config``. Otherwise a platform configured via
    ``PlatformConfig.extra`` (which ``_is_connected`` accepts) would pass enablement, reach
    ``create_adapter()``, and have the installer veto on env-var grounds before ever installing —
    re-creating the #79812 deadlock for extra-configured setups.
    """
    global DINGTALK_STREAM_AVAILABLE, dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage, HTTPX_AVAILABLE, httpx
    if DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE:
        return True
    try:
        from pm.extras import ensure_import
        ensure_import("dingtalk")
        import dingtalk_stream as _ds, httpx as _httpx  # noqa: E401
        from dingtalk_stream import ChatbotMessage as _CM
        from dingtalk_stream.frames import CallbackMessage as _CBM, AckMessage as _AM
    except Exception:
        return False
    dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage, httpx = _ds, _CM, _CBM, _AM, _httpx
    DINGTALK_STREAM_AVAILABLE = HTTPX_AVAILABLE = True
    return True


def _credentials(extra: Optional[dict]) -> tuple:
    """(client_id, client_secret) from PlatformConfig.extra first, then env / scoped secret."""
    extra = extra or {}
    # client_id goes through the same scoped reader as the secret: os.environ holds the DEFAULT
    # profile's app id under multiplex, and pairing it with a secondary's secret authenticates as the wrong app.
    return (extra.get("client_id") or _get_scoped_secret("DINGTALK_CLIENT_ID", ""),
            extra.get("client_secret") or _get_scoped_secret("DINGTALK_CLIENT_SECRET", ""))


def check_dingtalk_requirements() -> bool:
    """Combined deps (lazy-installed) + credentials check for setup/status callers."""
    return ensure_dingtalk_deps() and all(_credentials(None))


class DingTalkAdapter(BasePlatformAdapter):
    """Stream Mode adapter: the SDK keeps a long-lived WebSocket and messages arrive via a ChatbotHandler
    callback; replies go through the message's session_webhook (httpx) or, with ``card_template_id``, AI Cards."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    @property
    def SUPPORTS_MESSAGE_EDITING(self) -> bool:  # noqa: N802
        """Edits only exist with AI Cards; the gateway gates streaming cursor/edit on this."""
        return bool(self._card_template_id and self._card_sdk)

    REQUIRES_EDIT_FINALIZE = SUPPORTS_MESSAGE_EDITING  # AI Cards need an explicit ``finalize=True`` edit to close the streaming indicator

    @property
    def SUPPORTS_TURN_STATUS_CARD(self) -> bool:  # noqa: N802
        """DingTalk AI Cards can keep one editable progress/status card per turn."""
        return bool(self._card_template_id and self._card_sdk)

    # -- Stage-aware emoji reaction labels ----------------------------------
    # Default Thinking / Done / Error / Interrupted reactions fired on the
    # original user message.  ``notify_tool_started`` swaps Thinking for a
    # category-specific label; ``_fire_done_reaction`` recalls whatever label
    # was last fired so the final reaction lands on the correct anchor.
    REACTION_THINKING = "🤔 想一想"
    REACTION_DONE = "✅ 搞定了"
    REACTION_ERROR = "😓 遇到麻烦了"
    REACTION_INTERRUPTED = "⏸️ 先停一下"

    # Tool -> broad category label. Categories are coarse on purpose:
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

    # Terminal command -> more specific reaction label.
    # Matched in order; first hit wins.
    _TERMINAL_STAGE_LABELS: List[Tuple[re.Pattern, str]] = [
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

    # One-shot text notice delivered when the editable AI Card path fails so the user
    # knows real-time progress is unavailable rather than seeing silence.
    _DEGRADED_PROGRESS_NOTICE = "⚠️ 实时进度暂不可用，答案稍后返回"

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)
        extra = config.extra or {}
        self._client_id, self._client_secret = _credentials(extra)
        # Group-chat gating; mention state is the SDK's structured ``is_in_at_list``, not text parsing.
        self._mention_patterns: List[re.Pattern] = self._compile_mention_patterns()
        self._allowed_users: Set[str] = {item.lower() for item in self._csv_setting("allowed_users", "DINGTALK_ALLOWED_USERS")}
        self._stream_client = self._stream_task = self._http_client = self._card_sdk = self._robot_sdk = None
        # Liveness watchdog for half-open Stream Mode connections; see ``STREAM_PING_INTERVAL``.
        self._watchdog_task: Optional[asyncio.Task] = None
        self._ping_interval: int = _positive_int(
            _extra_or_secret(extra, "stream_ping_interval", "DINGTALK_STREAM_PING_INTERVAL"), STREAM_PING_INTERVAL)
        self._ping_timeout: int = _positive_int(
            _extra_or_secret(extra, "stream_ping_timeout", "DINGTALK_STREAM_PING_TIMEOUT"), STREAM_PING_TIMEOUT)
        self._robot_code: str = extra.get("robot_code") or self._client_id
        # Robot-native OpenAPI routing: app_code for PrivateChatSend, corp_id/agent_id reserved for future.
        self._app_code: str = extra.get("app_code", "")
        self._corp_id: str = extra.get("corp_id", "")
        self._agent_id: str = extra.get("agent_id", "")
        # Whether to @ the group sender in final replies (DingTalk card delivery supports structured @).
        self._reply_at_sender: bool = is_truthy_value(
            _extra_or_secret(extra, "reply_at_sender", "DINGTALK_REPLY_AT_SENDER"), default=False)
        self._dedup = MessageDeduplicator(max_size=1000)
        self._session_webhooks: Dict[str, tuple[str, int]] = {}  # chat_id -> (webhook, expired_time_ms)
        self._message_contexts: Dict[str, Any] = {}  # chat_id -> last inbound ChatbotMessage (per-chat: no clobber)
        # AI Card template: use a SDK-proven default when none is configured so card delivery works out-of-box.
        configured_template = str(extra.get("card_template_id") or "").strip()
        self._card_template_id: Optional[str] = configured_template or DEFAULT_AI_CARD_TEMPLATE_ID
        self._card_uses_default_template: bool = not configured_template
        self._card_content_key_override: str = str(extra.get("card_content_key") or "").strip()
        self._done_emoji_fired: Set[str] = set()  # chats whose Done reaction fired this turn; reset per inbound
        # Per-chat reply state (success/error/interrupted) set by the gateway runner so the final
        # reaction matches the turn outcome.  Popped on use; defaults to "success" when unset.
        self._pending_reply_state: Dict[str, str] = {}
        # Stage-aware reaction state: the label currently rendered on the user message for each chat.
        self._current_stage_label: Dict[str, str] = {}
        # The outcome label last put on each chat's current message, so a late outcome can replace it.
        self._final_reaction_label: Dict[str, str] = {}
        # Per-chat lock that serializes stage-label swaps so parallel tool.started events don't race.
        self._stage_locks: Dict[str, asyncio.Lock] = {}
        # Open streaming cards: chat_id -> {out_track_id: last_content}. ``edit_message(finalize=False)``
        # re-opens a finalized card, so we track them and auto-close as siblings on the next ``send()``.
        self._streaming_cards: Dict[str, Dict[str, str]] = {}
        self._bg_tasks: Set[asyncio.Task] = set()  # fire-and-forget emoji tasks, kept referenced (GC) + cancellable
        # Tool-progress callbacks run on the agent's worker thread; their emoji swaps are handed to the
        # adapter's loop (captured in connect) instead of a create_task with no loop to run on.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bg_futures: Set[concurrent.futures.Future] = set()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to DingTalk via Stream Mode."""
        for ok, problem in ((DINGTALK_STREAM_AVAILABLE, "dingtalk-stream not installed. Run: pip install 'dingtalk-stream>=0.20'"),
                            (HTTPX_AVAILABLE, "httpx not installed. Run: pip install httpx"),
                            (self._client_id and self._client_secret, "DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required")):
            if not ok:
                logger.warning("[%s] " + problem, self.name)
                return False
        try:
            from gateway.platforms._http_client_limits import platform_httpx_limits  # tighter keepalive: idle CLOSE_WAIT drains promptly
            self._http_client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
            self._stream_client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(self._client_id, self._client_secret))
            if CARD_SDK_AVAILABLE:
                sdk_config = open_api_models.Config()
                sdk_config.protocol, sdk_config.region_id = "https", "central"
                if self._card_template_id:
                    self._card_sdk = dingtalk_card_client.Client(sdk_config)
                self._robot_sdk = dingtalk_robot_client.Client(sdk_config)  # needed for media download even without cards
                if self._card_template_id:
                    logger.info("[%s] Card SDK initialized with template: %s", self.name, self._card_template_id)
                else:
                    logger.info("[%s] Robot SDK initialized (media download)", self.name)
            self._stream_client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, _IncomingHandler(self, asyncio.get_running_loop()))
            self._loop = asyncio.get_running_loop()
            self._stream_task = asyncio.create_task(self._run_stream())
            self._watchdog_task = asyncio.create_task(self._run_watchdog())
            self._mark_connected()
            logger.info("[%s] Connected via Stream Mode", self.name)
            self._wire_plugin_handlers(self._stream_client)  # plugin-registered native handlers
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """Run the SDK stream client with auto-reconnection.

        dingtalk-stream's ``start()`` runs its own catch-all retry loop, so most
        errors never reach us: ``_SdkLogGuard`` collapses that loop's repeated
        log records, and a dingtalk-stream/websockets incompatibility (#24851)
        is handed to the gateway's reconnect watcher via ``_set_fatal_error``
        instead of retrying forever.  For errors that do escape ``start()``,
        exponential backoff (RECONNECT_BACKOFF) applies; after
        RECONNECT_CIRCUIT_BREAKER_TRIPS identical errors in a row the breaker
        trips and stays tripped (silent, RECONNECT_CIRCUIT_BREAKER_DELAY between
        attempts) until the error type changes.
        """
        self._sdk_loop_task = asyncio.current_task()
        sdk_logger = getattr(self._stream_client, "logger", None)
        if not isinstance(sdk_logger, logging.Logger):
            sdk_logger = logging.getLogger("dingtalk_stream.client")
        guard = _SdkLogGuard(self._on_sdk_error)
        sdk_logger.addFilter(guard)
        try:
            await self._run_stream_loop()
        finally:
            sdk_logger.removeFilter(guard)

    async def _run_stream_loop(self) -> None:
        backoff_idx = 0
        consecutive_same_error = 0
        last_error_type: type | None = None
        while self._running:
            try:
                logger.debug("[%s] Starting stream client...", self.name)
                await self._stream_client.start()
            except asyncio.CancelledError:
                if getattr(self, "_fatal_error_code", None) == "dingtalk_stream_error":
                    await self._notify_fatal_error()
                return
            except Exception as e:
                if not self._running:
                    return
                if _is_sdk_incompat(e):
                    self._on_sdk_error(e, cancel=False)
                    await self._notify_fatal_error()
                    return
                error_type = type(e)
                if error_type is last_error_type:
                    consecutive_same_error += 1
                else:
                    consecutive_same_error = 1
                    last_error_type = error_type
                if consecutive_same_error <= RECONNECT_CIRCUIT_BREAKER_TRIPS:
                    logger.warning("[%s] Stream client error: %s", self.name, e)
                elif consecutive_same_error == RECONNECT_CIRCUIT_BREAKER_TRIPS + 1:
                    logger.error(
                        "[%s] Stream client error repeated %d times: %s — circuit breaker tripped; "
                        "retrying every %ds silently until the error changes.",
                        self.name, consecutive_same_error, e, RECONNECT_CIRCUIT_BREAKER_DELAY,
                    )
            if not self._running:
                return
            if consecutive_same_error > RECONNECT_CIRCUIT_BREAKER_TRIPS:
                await asyncio.sleep(RECONNECT_CIRCUIT_BREAKER_DELAY)
            else:
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.info("[%s] Reconnecting in %ds...", self.name, delay)
                await asyncio.sleep(delay)
                backoff_idx += 1

    def _on_sdk_error(self, exc: BaseException, *, cancel: bool = True) -> None:
        """Called (sync, possibly from the SDK logger) with an SDK exception; hands off on incompat."""
        if not _is_sdk_incompat(exc) or getattr(self, "_fatal_error_code", None) == "dingtalk_stream_error":
            return
        msg = (f"dingtalk-stream cannot open its websocket with the installed websockets package ({exc}). "
               "Hermes pins dingtalk-stream==0.24.3 with websockets==15.0.1; reinstall the dingtalk extra "
               "so those versions are used (e.g. `pip install 'hermes-agent[dingtalk]'`).")
        logger.error("[%s] %s", self.name, msg)
        # Not retryable: only a reinstall + restart fixes it, and connect() returns True before the
        # socket exists, so a gateway reconnect would re-fail every watcher tick forever.
        self._set_fatal_error("dingtalk_stream_error", msg, retryable=False)
        task = getattr(self, "_sdk_loop_task", None)
        if cancel and task is not None and not task.done():
            task.cancel()  # SDK's own retry loop never exits; lands on its next await

    async def _run_watchdog(self) -> None:
        """Detect and recover half-open Stream connections.

        Periodically pings the live websocket and awaits the pong. A healthy (even if idle)
        connection returns the pong promptly; a half-open one never does. On timeout we force the
        websocket closed, which unblocks the SDK's inner ``async for`` and triggers a fresh
        reconnect with a new ticket. See ``STREAM_PING_INTERVAL`` for the full rationale.
        """
        while self._running:
            await asyncio.sleep(self._ping_interval)
            if not self._running:
                return
            websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
            if websocket is None:
                continue  # not connected yet (or mid-reconnect); nothing to probe
            try:
                # ws.ping() returns a future that resolves when the matching pong arrives. Awaiting
                # it under a timeout is the actual half-open detector (the SDK's own keepalive
                # never awaits it).
                pong_waiter = await websocket.ping()
                await asyncio.wait_for(pong_waiter, timeout=self._ping_timeout)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                # Includes ``asyncio.TimeoutError`` (pong never arrived) and any
                # ``ConnectionClosed*`` raised by ``ping()`` on an already-dead socket.
                if not self._running:
                    return
                logger.warning("[%s] Stream liveness check failed (%s: %s) — forcing reconnect on "
                               "suspected half-open connection", self.name, type(exc).__name__, exc)
                await self._quiet(websocket.close(), "[%s] watchdog websocket close failed: %s")

    async def _quiet(self, coro, debug_fmt: str = "", *args) -> None:
        """Await *coro*, swallowing any exception (logged at debug as ``debug_fmt % (name, *args, exc)`` when given)."""
        try:
            await coro
        except Exception as e:
            if debug_fmt:
                logger.debug(debug_fmt, self.name, *args, e)

    async def disconnect(self) -> None:
        """Disconnect from DingTalk."""
        self._running = False
        self._mark_disconnected()
        # Cancel the liveness watchdog first so it can't race the shutdown close() below into a
        # spurious "half-open" reconnect while we're actually tearing the socket down.
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("[%s] watchdog task did not exit cleanly during disconnect", self.name)
            self._watchdog_task = None
        # Close the websocket first so the stream task sees the disconnect instead of awaiting frames that never arrive.
        websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
        if websocket is not None:
            await self._quiet(websocket.close(), "[%s] websocket close during disconnect failed: %s")
        if self._stream_task:
            if hasattr(self._stream_client, "close"):
                await self._quiet(asyncio.to_thread(self._stream_client.close))  # sync close() may block on I/O
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("[%s] stream task did not exit cleanly during disconnect", self.name)
            self._stream_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        for fut in list(self._bg_futures):
            fut.cancel()
        self._bg_futures.clear()
        # Finalize open streaming cards BEFORE the HTTP client closes so they don't stay stuck
        # in streaming state after a gateway restart. Outer try guards the token fetch.
        for _chat_id in list(self._streaming_cards):
            await self._quiet(self._close_streaming_siblings(_chat_id), "[%s] Failed to finalize streaming card on disconnect for %s: %s", _chat_id)
        if self._http_client:
            await self._http_client.aclose()
        self._http_client = self._stream_client = None
        for store in (
            getattr(self, "_session_webhooks", None),
            getattr(self, "_message_contexts", None),
            getattr(self, "_streaming_cards", None),
            getattr(self, "_done_emoji_fired", None),
            getattr(self, "_pending_reply_state", None),
            getattr(self, "_current_stage_label", None),
            getattr(self, "_final_reaction_label", None),
            getattr(self, "_stage_locks", None),
            getattr(self, "_dedup", None),
            getattr(self, "_bg_tasks", None),
        ):
            if store is not None and hasattr(store, "clear"):
                store.clear()
        logger.info("[%s] Disconnected", self.name)

    def _csv_setting(self, key: str, env_name: str) -> Set[str]:
        """List/CSV setting from config.extra[key], falling back to the env var."""
        return _csv_set(_extra_or_secret(self.config.extra, key, env_name, blank_is_unset=False))

    def _dingtalk_require_mention(self) -> bool:
        """Whether group chats require an explicit bot trigger."""
        configured = _extra_or_secret(self.config.extra, "require_mention", "DINGTALK_REQUIRE_MENTION", "false", blank_is_unset=False)
        return configured.lower() in _TRUTHY if isinstance(configured, str) else bool(configured)

    def _dingtalk_allowed_chats(self) -> Set[str]:
        """Group chat whitelist; non-empty = hard gate even when @mentioned. DMs never filtered."""
        return self._csv_setting("allowed_chats", "DINGTALK_ALLOWED_CHATS")

    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns (config list, or env as JSON / lines / CSV)."""
        patterns = (self.config.extra or {}).get("mention_patterns")
        if patterns is None and (raw := str(_get_scoped_secret("DINGTALK_MENTION_PATTERNS", "") or "").strip()):
            try:
                patterns = json.loads(raw)
            except Exception:
                patterns = [part.strip() for part in raw.splitlines() if part.strip()]
                if not patterns:
                    patterns = [part.strip() for part in raw.split(",") if part.strip()]
        if patterns is None:  # return before touching ``self.name`` on the no-patterns path (historical parity)
            return []
        return compile_mention_patterns(patterns, log_prefix=self.name, platform_label="dingtalk", display_label="DingTalk", logger_=logger)

    def _is_user_allowed(self, sender_id: str, sender_staff_id: str) -> bool:
        if not self._allowed_users or "*" in self._allowed_users:
            return True
        return bool(({(sender_id or "").lower(), (sender_staff_id or "").lower()} - {""}) & self._allowed_users)

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return bool(text and self._mention_patterns) and any(p.search(text) for p in self._mention_patterns)

    def _should_process_message(self, message: "ChatbotMessage", text: str, is_group: bool, chat_id: str) -> bool:
        """Group trigger rules (DMs always pass; ``allowed_users`` is enforced earlier): ``allowed_chats`` is a hard
        gate, then any of free_response_chats / require_mention off / @mentioned (SDK ``is_in_at_list``) / wake-word."""
        if not is_group:
            return True
        allowed = self._dingtalk_allowed_chats()
        if allowed and chat_id and chat_id not in allowed:
            return False
        return (
            bool(chat_id and chat_id in self._csv_setting("free_response_chats", "DINGTALK_FREE_RESPONSE_CHATS"))
            or not self._dingtalk_require_mention()
            or bool(getattr(message, "is_in_at_list", False))
            or self._message_matches_mention_patterns(text)
        )

    def _spawn_bg(self, coro) -> None:
        """Start a fire-and-forget coroutine and track it for cleanup — from the adapter's loop or,
        for tool-progress callbacks, from the agent's worker thread (handed to the adapter's loop)."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        target = self._loop if self._loop is not None and self._loop.is_running() else None
        if target is not None and running is not target:
            self._bg_futures.add(fut := asyncio.run_coroutine_threadsafe(coro, target))
            fut.add_done_callback(self._bg_futures.discard)
        elif running is not None:
            self._bg_tasks.add(task := running.create_task(coro))
            task.add_done_callback(self._bg_tasks.discard)
        else:
            coro.close()
            logger.debug("[%s] Dropped background coroutine: no running event loop", self.name)

    async def _close_streaming_siblings(self, chat_id: str) -> None:
        """Finalize open streaming cards for this chat at the start of every ``send()`` — the gateway has no "turn end" signal, so this is what closes lingering tool-progress cards."""
        cards = self._streaming_cards.pop(chat_id, None)
        token = await self._get_access_token() if cards else None
        for out_track_id, last_content in list(cards.items()) if token else ():
            try:
                await self._stream_card_content(out_track_id, token, last_content, finalize=True)
                logger.debug("[%s] AI Card sibling closed: %s", self.name, out_track_id)
            except Exception as e:
                logger.debug("[%s] Sibling close failed for %s: %s", self.name, out_track_id, e)

    def _fire_done_reaction(self, chat_id: str) -> None:
        """Swap the in-flight reaction for the turn outcome.

        Reads the pending reply state set by the gateway runner via
        :meth:`set_pending_reply_state`. Defaults to "success" so this
        is safe to call even when nothing set the state.
        """
        if chat_id in self._done_emoji_fired:
            return
        self._done_emoji_fired.add(chat_id)
        self._swap_final_reaction(chat_id, self._pending_reply_state.pop(chat_id, "success"))

    def _swap_final_reaction(self, chat_id: str, state: str) -> None:
        """Replace the label currently on the user's message with the one for ``state``."""
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return
        final_label = {"error": self.REACTION_ERROR, "interrupted": self.REACTION_INTERRUPTED}.get(
            state, self.REACTION_DONE)

        async def _swap() -> None:
            lock = self._stage_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                current = self._final_reaction_label.pop(chat_id, None) or self._current_stage_label.pop(
                    chat_id, self.REACTION_THINKING,
                )
                await self._send_emotion(
                    msg_id, conversation_id, current, recall=True,
                )
                await self._send_emotion(
                    msg_id, conversation_id, final_label, recall=False,
                )
                self._final_reaction_label[chat_id] = final_label

        self._spawn_bg(_swap())

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
        if chat_id not in self._done_emoji_fired:
            self._pending_reply_state[chat_id] = state
        elif state != "success":
            # A streamed reply's final edit already fired the default Done before the runner knew
            # the outcome; correct it now rather than leave the state to mislabel the next turn.
            self._swap_final_reaction(chat_id, state)

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
        """
        if not chat_id:
            return
        new_label = self._stage_label_for_tool(tool_name, preview=preview)
        if new_label is None:
            return
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

    async def _send_degraded_progress_notice(
        self,
        chat_id: str,
        session_webhook: Optional[str],
    ) -> None:
        """Send a one-shot text notice when the editable AI Card path fails.

        The notice intentionally does NOT return a ``message_id`` to the caller —
        the outer ``send()`` still returns ``success=False`` so the turn-status
        coordinator disables itself. All errors are best-effort.
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

    def _begin_reply_cycle(self, chat_id: str, message: Any) -> None:
        """Make ``message`` the chat's current one with its own Thinking→outcome reaction cycle;
        nothing recorded for the previous message may label this one."""
        self._message_contexts[chat_id] = message
        self._done_emoji_fired.discard(chat_id)
        self._pending_reply_state.pop(chat_id, None)
        self._final_reaction_label.pop(chat_id, None)

    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            return logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
        conversation_id, sender_id, sender_nick, sender_staff_id = (getattr(message, k, "") or "" for k in ("conversation_id", "sender_id", "sender_nick", "sender_staff_id"))
        is_group = str(getattr(message, "conversation_type", "1")) == "2"
        sender_nick = sender_nick or sender_id
        chat_id = conversation_id or sender_id
        if not self._is_user_allowed(sender_id, sender_staff_id):
            return logger.debug("[%s] Dropping message from non-allowlisted user staff_id=%s sender_id=%s", self.name, sender_staff_id, sender_id)
        if not self._should_process_message(message, self._extract_text(message) or "", is_group, chat_id):  # wake-word gate needs text early
            return logger.debug("[%s] Dropping group message that failed mention gate message_id=%s chat_id=%s", self.name, msg_id, chat_id)
        if chat_id:
            self._begin_reply_cycle(chat_id, message)
        session_webhook = getattr(message, "session_webhook", None) or ""
        if session_webhook and chat_id and _DINGTALK_WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                self._session_webhooks.pop(next(iter(self._session_webhooks)))  # evict oldest (dict is non-empty here)
            self._session_webhooks[chat_id] = (session_webhook, getattr(message, "session_webhook_expired_time", 0) or 0)
        await self._resolve_media_codes(message)  # download codes -> URLs so vision tools can use them
        text = self._extract_text(message)
        msg_type, media_urls, media_types = self._extract_media(message)
        media_errors = self._extract_media_errors(message)
        if not text and not media_urls and not media_errors:
            return logger.debug("[%s] Empty message, skipping", self.name)
        source = self.build_source(chat_id=chat_id, chat_name=getattr(message, "conversation_title", None), chat_type="group" if is_group else "dm",
                                   user_id=sender_id, user_name=sender_nick, user_id_alt=sender_staff_id if sender_staff_id else None,
                                   message_id=msg_id)
        create_at = getattr(message, "create_at", None)
        try:
            timestamp = datetime.fromtimestamp(int(create_at) / 1000, tz=timezone.utc) if create_at else datetime.now(tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)
        logger.debug("[%s] Message from %s in %s: %s", self.name, sender_nick, chat_id[:20] if chat_id else "?", text[:80] if text else "(media)")
        await self.handle_message(MessageEvent(text=text, message_type=msg_type, source=source, message_id=msg_id, raw_message=message,
                                               media_urls=media_urls, media_types=media_types, media_errors=media_errors, timestamp=timestamp))

    _extract_text = staticmethod(extract_text)

    def _extract_media(self, message: "ChatbotMessage"):
        return extract_media(message)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send a markdown reply via DingTalk session webhook, AI Card, or robot-native proactive fallback."""
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
            await self._close_streaming_siblings(chat_id)  # close lingering tool-progress cards before creating a new one
            result = await self._create_and_stream_card(
                chat_id, current_message, content,
                finalize=finalize_on_create,
                at_users=at_users,
            )
            if result and result.success:
                self._fire_custom_reactions(chat_id, emotion_names)
                if fire_final_reaction:
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
                # not accept.
                #
                # But silent failure is its own bad UX — the user is
                # left staring at no progress for the rest of the turn.
                # As a one-shot notice, send a plain webhook line so
                # the user knows real-time progress is unavailable.
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
            # No valid session_webhook: fall back to the robot-native proactive
            # message path (_send_robot_native_message -> OrgGroupSend /
            # PrivateChatSend), which authenticates with the app access token
            # instead of the ephemeral webhook and can deliver at any time.
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
                if fire_final_reaction:
                    self._fire_done_reaction(chat_id)
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            body = resp.text
            logger.warning(
                "[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200]
            )
            # A webhook that DingTalk rejects (expired mid-flight -> 400
            # "expired", robot removed from group, etc.) is just as dead as a
            # missing one. Fall back to the proactive path rather than losing
            # the reply. We only retry on 4xx (the webhook itself is bad); 5xx
            # is a transient DingTalk server issue where a retry against the
            # SAME dead webhook is pointless.
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

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """DingTalk does not support typing indicators."""

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata=None) -> SendResult:
        """Render a remote image inline via markdown (session webhook has no native attachments)."""
        image_block = f"![image]({image_url})"
        return await self.send(chat_id=chat_id, content=f"{caption}\n\n{image_block}" if caption else image_block, reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata=None, **kwargs) -> SendResult:
        """Upload a local image and send it via DingTalk's card_1_0 image delivery path."""
        if image_path.startswith(("http://", "https://")):
            return await self.send_image(chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata)
        path = Path(image_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {image_path}")
        upload = await self._upload_robot_media(str(path), media_type="image")
        if not upload.success:
            return upload
        return await self._send_robot_card_1_0_image(
            chat_id, upload.message_id, caption=caption, metadata=metadata,
        )

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        """Upload a local file and send it as a DingTalk sampleFile robot message."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {file_path}")
        metadata = metadata or {}
        upload = await self._upload_robot_media(str(path), media_type="file")
        if not upload.success:
            return upload
        return await self._send_robot_native_message(
            chat_id=chat_id,
            msg_key="sampleFile",
            msg_param={"mediaId": upload.message_id, "fileName": file_name or path.name},
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

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a formatted approval-request card for a dangerous command.

        DingTalk does not yet support interactive callback buttons on AI Cards,
        so we fall back to a richly-formatted AI Card that clearly shows the
        command and the available text responses (approve / deny).  The gateway's
        existing plain-text approval resolver handles the user's reply.

        Reply keywords accepted by the gateway:
          approve / yes / ok / okay / confirm / y / thumbs-up  -> execute
          approve session  -> allow for this session
          approve always  -> allow permanently
          deny  -> cancel
        """
        cmd_preview = command[:400] + "..." if len(command) > 400 else command
        msg = (
            f"**Dangerous Command Approval**\n\n"
            f"**Reason:** {description}\n\n"
            f"```\n{cmd_preview}\n```\n\n"
            f"| Action | Reply |\n"
            f"|------|----------|\n"
            f"| Approve once | `approve` |\n"
            f"| Allow for session | `approve session` |\n"
            f"| Allow always | `approve always` |\n"
            f"| Deny | `deny` |\n"
        )
        return await self.send(chat_id, msg, metadata=metadata)

    # -- Robot-native proactive delivery (webhook-independent fallback) ----

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
        """Deliver a markdown reply without a session_webhook.

        A session_webhook is only valid for a short window after the user's
        inbound message; long agent turns and gateway restarts outlive it.
        Two webhook-independent transports exist, tried in order of reliability:

          1. **AI Card** (``_create_and_stream_card``) — the SDK-proven
             transport. It authenticates with the app access token and
             targets a group by ``dtv1.card//IM_GROUP.{conversation_id}``
             (or a DM robot open-space), so it works with no live webhook.
             Here we drive it explicitly for the resume case where the
             inbound context was lost on restart, by synthesizing a minimal
             message carrying ``conversation_id`` = ``chat_id``.
          2. **Robot-native ``sampleMarkdown``** (OrgGroupSend /
             PrivateChatSend) — only works for a *published org-internal
             robot*; a plain Stream app is rejected. Kept as a best-effort
             last resort.
        """
        normalized = self._normalize_markdown(content[: self.MAX_MESSAGE_LENGTH])
        normalized = self._prepend_mention_tokens(normalized, at_payload or {})
        if not normalized.strip():
            return SendResult(success=False, error="Empty content; nothing to send")

        # Transport 1: AI Card. Reuse a live inbound context if we still
        # have one; otherwise synthesize a group-targeted message from chat_id.
        card_result: Optional[SendResult] = None
        if self._card_template_id and self._card_sdk:
            current_message = self._message_contexts.get(chat_id)
            if current_message is None:
                current_message = SimpleNamespace(
                    conversation_id=chat_id,
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

    async def _upload_robot_media(self, file_path: str, media_type: str) -> SendResult:
        """Upload a local file to DingTalk's robot media endpoint for a temporary ``media_id``."""
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
        # accepted as a generic file. Upload file attachments as opaque bytes
        # so valid extensions are not blocked by Content-Type sniffing.
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
        """Send a DingTalk native robot message via OpenAPI (OrgGroupSend / PrivateChatSend / BatchSendOTO)."""
        metadata = metadata or {}
        if not self._robot_sdk or not dingtalk_robot_models or not tea_util_models:
            return SendResult(success=False, error="DingTalk robot SDK is unavailable")

        current_message = self._message_contexts.get(chat_id)
        open_conversation_id = (
            metadata.get("dingtalk_open_conversation_id")
            or metadata.get("open_conversation_id")
            or getattr(current_message, "conversation_id", None)
            or chat_id
        )
        if not open_conversation_id:
            return SendResult(success=False, error="DingTalk openConversationId is unavailable")

        robot_code = metadata.get("dingtalk_robot_code") or metadata.get("robot_code") or self._robot_code
        if not robot_code:
            robot_code = getattr(current_message, "robot_code", None)
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
                "(msg_key=%s chat=%s route=%s)",
                self.name,
                exc,
                msg_key,
                str(open_conversation_id)[:20],
                send_route,
            )
            return SendResult(success=False, error=f"DingTalk native send failed: {exc}")

    async def _send_robot_card_1_0_image(
        self,
        chat_id: str,
        media_id: str,
        *,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an uploaded image through DingTalk's card_1_0 delivery path for DM images."""
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
                "(chat=%s route=%s)",
                self.name,
                exc,
                str(open_conversation_id)[:20],
                route,
            )
            return SendResult(success=False, error=f"DingTalk card_1_0 image failed: {exc}")

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

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a DingTalk conversation."""
        return {"name": chat_id, "type": "group" if "group" in chat_id.lower() else "dm"}

    def _get_valid_webhook(self, chat_id: str) -> Optional[tuple[str, int]]:
        """Get a non-expired session webhook for chat_id (5-minute safety margin)."""
        info = self._session_webhooks.get(chat_id)
        expired_time_ms = info[1] if info else 0
        if expired_time_ms and expired_time_ms > 0 and int(datetime.now(tz=timezone.utc).timestamp() * 1000) + 5 * 60 * 1000 >= expired_time_ms:
            self._session_webhooks.pop(chat_id, None)
            return None
        return info or None

    async def _create_and_stream_card(
        self,
        chat_id: str,
        message: Any,
        content: str,
        *,
        finalize: bool = True,
        at_users: Optional[Dict[str, str]] = None,
    ) -> Optional[SendResult]:
        """Create, deliver and stream an AI Card; ``finalize=False`` leaves it open for ``edit_message`` by out_track_id.

        When *at_users* is provided, their IDs are passed as ``card_at_user_ids`` on card
        creation (DingTalk supports structured @ within card delivery, not just webhook text).
        """
        try:
            token = await self._get_access_token()
            if not token:
                return None
            out_track_id, models = f"hermes_{uuid.uuid4().hex[:12]}", dingtalk_card_models
            is_group = str(getattr(message, "conversation_type", "1")) == "2"
            sender_staff_id = getattr(message, "sender_staff_id", "") or ""
            create_kwargs: Dict[str, Any] = {
                "card_template_id": self._card_template_id,
                "out_track_id": out_track_id,
                "card_data": models.CreateCardRequestCardData(card_param_map=self._card_initial_param_map()),
                "callback_type": "STREAM",
                "im_group_open_space_model": models.CreateCardRequestImGroupOpenSpaceModel(support_forward=True),
                "im_robot_open_space_model": models.CreateCardRequestImRobotOpenSpaceModel(support_forward=True),
            }
            if at_users:
                create_kwargs["card_at_user_ids"] = list(at_users.keys())
            create_request = models.CreateCardRequest(**create_kwargs)
            await self._sdk_call(self._card_sdk.create_card_with_options_async, create_request, models.CreateCardHeaders, token)
            if is_group:
                open_space_id = f"dtv1.card//IM_GROUP.{getattr(message, 'conversation_id', '') or ''}"
                deliver_kwargs: Dict[str, Any] = {"robot_code": self._robot_code}
                if at_users:
                    deliver_kwargs["at_user_ids"] = at_users
                deliver_model = {"im_group_open_deliver_model": models.DeliverCardRequestImGroupOpenDeliverModel(**deliver_kwargs)}
            elif sender_staff_id:
                open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
                deliver_model = {"im_robot_open_deliver_model": models.DeliverCardRequestImRobotOpenDeliverModel(space_type="IM_ROBOT")}
            else:
                return logger.warning("[%s] AI Card skipped: missing sender_staff_id for DM", self.name)
            deliver_request = models.DeliverCardRequest(out_track_id=out_track_id, user_id_type=1, open_space_id=open_space_id, **deliver_model)
            await self._sdk_call(self._card_sdk.deliver_card_with_options_async, deliver_request, models.DeliverCardHeaders, token)
            await self._stream_card_content(out_track_id, token, content, finalize=finalize)
            logger.info("[%s] AI Card %s: %s", self.name, "created+finalized" if finalize else "created (streaming)", out_track_id)
            return SendResult(success=True, message_id=out_track_id)
        except Exception as e:
            logger.warning("[%s] AI Card create failed: %s\n%s", self.name, e, traceback.format_exc())
            return None

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        """Stream updated content to an AI Card; ``message_id`` is the creating ``send()``'s out_track_id (callers track their own ids so parallel flows on one chat don't interfere)."""
        if not self.SUPPORTS_MESSAGE_EDITING:
            return SendResult(success=False, error="AI Cards are not configured for message editing")
        token = await self._get_access_token() if message_id else None
        if not token:
            return SendResult(success=False, error="message_id required" if not message_id else "No access token")
        try:
            await self._stream_card_content(message_id, token, content, finalize=finalize)
            if finalize:  # canonical "response ended" signal from the stream consumer's final edit
                self._streaming_cards.get(chat_id, {}).pop(message_id, None)
                if not self._streaming_cards.get(chat_id):
                    self._streaming_cards.pop(chat_id, None)
                logger.debug("[%s] AI Card finalized (edit): %s", self.name, message_id)
                self._fire_done_reaction(chat_id)
            else:  # non-final edit reopens the card into streaming state — track for sibling close
                self._streaming_cards.setdefault(chat_id, {})[message_id] = content
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.warning("[%s] Card edit failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    @staticmethod
    async def _sdk_call(method, request, headers_cls, token: str):
        """``await method(request, headers_cls(token), RuntimeOptions())`` — the alibabacloud SDK call shape."""
        return await method(request, headers_cls(x_acs_dingtalk_access_token=token), tea_util_models.RuntimeOptions())

    async def _stream_card_content(self, out_track_id: str, token: str, content: str, finalize: bool = False) -> None:
        """Stream content to an existing AI Card."""
        card_content_key = self._current_card_content_key()
        stream_request = dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id, guid=str(uuid.uuid4()), key=card_content_key, content=content[: self.MAX_MESSAGE_LENGTH],
            is_full=True, is_finalize=finalize, is_error=False,
        )
        await self._sdk_call(self._card_sdk.streaming_update_with_options_async, stream_request, dingtalk_card_models.StreamingUpdateHeaders, token)

    async def _get_access_token(self) -> Optional[str]:
        """Get access token via the SDK's cached (sync, requests-based) getter."""
        if not self._stream_client:
            return None
        try:
            return await asyncio.to_thread(self._stream_client.get_access_token)
        except Exception as e:
            logger.error("[%s] Failed to get access token: %s", self.name, e)
            return None

    async def _send_emotion(self, open_msg_id: str, open_conversation_id: str, emoji_name: str, *, recall: bool = False) -> None:
        """Add (or recall) an emoji reaction on a message."""
        if not self._robot_sdk or not open_msg_id or not open_conversation_id:
            return
        action = "recall" if recall else "reply"
        try:
            token = await self._get_access_token()
            if not token:
                return
            text_emotion_cls, request_cls, headers_cls, sdk_method = _EMOTION_SDK[recall]
            text_emotion = getattr(dingtalk_robot_models, text_emotion_cls)(emotion_id=_EMOTION_ID, emotion_name=emoji_name, text=emoji_name, background_id=_EMOTION_BG)
            request = getattr(dingtalk_robot_models, request_cls)(robot_code=self._robot_code, open_msg_id=open_msg_id, open_conversation_id=open_conversation_id,
                                                                  emotion_type=2, emotion_name=emoji_name, text_emotion=text_emotion)
            await self._sdk_call(getattr(self._robot_sdk, sdk_method), request, getattr(dingtalk_robot_models, headers_cls), token)
            logger.info("[%s] _send_emotion: %s %s on msg=%s", self.name, action, emoji_name, open_msg_id[:24])
        except Exception:
            logger.debug("[%s] _send_emotion %s failed", self.name, action, exc_info=True)

    async def _resolve_media_codes(self, message: "ChatbotMessage") -> None:
        """Resolve download codes in the message to real URLs (in place, in parallel)."""
        token = await self._get_access_token()
        if not token:
            return
        robot_code = getattr(message, "robot_code", None) or self._client_id
        pairs = [(getattr(obj, key, None) if hasattr(obj, key) else obj.get(key), obj, key) for obj, key in collect_download_codes(message)]
        tasks = [self._fetch_download_url(code, robot_code, token, obj, key) for code, obj, key in pairs if code]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_download_url(self, code: str, robot_code: str, token: str, obj, key: str) -> None:
        """Fetch the download URL for one code via the robot SDK and write it back to ``obj[key]``."""
        if not self._robot_sdk:
            return logger.warning("[%s] Robot SDK not initialized, cannot resolve media code", self.name)
        try:
            response = await self._sdk_call(self._robot_sdk.robot_message_file_download_with_options_async,
                                            dingtalk_robot_models.RobotMessageFileDownloadRequest(download_code=code, robot_code=robot_code),
                                            dingtalk_robot_models.RobotMessageFileDownloadHeaders, token)
            body = response.body if response else None
            url = getattr(body, "download_url", None) if body else None
            if not body:
                logger.warning("[%s] Failed to download media: empty response for code %s", self.name, code)
            elif url and hasattr(obj, key):
                setattr(obj, key, url)
            elif url and isinstance(obj, dict):
                obj[key] = url
        except Exception as e:
            logger.error("[%s] Error resolving media code %s: %s", self.name, code, e)

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        """Work around DingTalk renderer quirks: blank line before numbered lists, dedent ``` fences."""
        lines = text.split("\n")
        out = []
        for i, line in enumerate(lines):
            prev = lines[i - 1].strip() if i > 0 else ""
            if prev and _NUMBERED_RE.match(line.strip()) and not _NUMBERED_RE.match(prev):
                out.append("")
            out.append(line.lstrip() if line.strip().startswith("```") else line)
        return "\n".join(out)

    # -- Config / metadata / rich-text helpers (ported from fork) -----------

    @staticmethod
    def _metadata_values(metadata: Dict[str, Any], *keys: str) -> List[str]:
        """Collect a list from one or more metadata keys (comma-separated or list values)."""
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
        """Read a bool from the first non-None metadata key among *keys*."""
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
        """Read a Path from the first metadata key whose value points to an existing file."""
        for key in keys:
            raw = metadata.get(key)
            if raw is None:
                continue
            path = Path(str(raw)).expanduser()
            if path.is_file():
                return path
        return None

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

    def _message_mentions_bot(self, message: "ChatbotMessage") -> bool:
        """True if the bot was @-mentioned in a group message.

        dingtalk-stream sets ``is_in_at_list`` on the incoming ChatbotMessage
        when the bot is addressed via @-mention.
        """
        return bool(getattr(message, "is_in_at_list", False))

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

    @staticmethod
    def _prepend_mention_tokens(
        content: str,
        at_payload: Optional[Dict[str, Any]],
    ) -> str:
        """Prepend DingTalk mention tokens required by webhook @ delivery.

        DingTalk webhook @ semantics require visible markdown/text to contain
        the same mobile or user ID listed in the ``at`` payload.  AI Cards use
        structured @ fields instead, so card content should stay clean.
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

        deduped = list(dict.fromkeys(mentions))
        if not deduped:
            return content
        prefix = " ".join(deduped)
        if content.lstrip().startswith(prefix):
            return content
        return f"{prefix}\n\n{content}" if content else prefix

    def _build_webhook_at_payload(
        self,
        metadata: Dict[str, Any],
        at_users: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        """Build the webhook ``at`` payload from metadata + collected at_users."""
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

    # -- Duration / media format probing -----------------------------------

    @staticmethod
    def _duration_ms_from_metadata(metadata: Dict[str, Any]) -> Optional[int]:
        """Read a media duration in ms from metadata (ms keys first, then seconds keys)."""
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
        """Probe media duration via ffprobe (best-effort; None on any failure)."""
        from tools.transcription_audio import _find_ffprobe_binary

        ffprobe = _find_ffprobe_binary()
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
                stdin=subprocess.DEVNULL,
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
    def _looks_like_mp4(path: Path) -> bool:
        """Quick magic-byte check: MP4 files contain ``ftyp`` in the first 16 bytes."""
        try:
            with path.open("rb") as fh:
                header = fh.read(32)
        except Exception:
            return False
        return len(header) >= 12 and b"ftyp" in header[:16]

    @staticmethod
    def _looks_like_native_audio(path: Path, ext: str) -> bool:
        """Quick magic-byte check for OGG / AMR headers DingTalk expects."""
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

    @staticmethod
    def _generate_video_cover(path: Path) -> Optional[Path]:
        """Generate a JPEG thumbnail from the first frame via ffmpeg (best-effort)."""
        from tools.transcription_audio import _find_ffmpeg_binary

        ffmpeg = _find_ffmpeg_binary()
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
                stdin=subprocess.DEVNULL,
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
        """Write a minimal dark-gray 320x180 PNG as a fallback video cover."""
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

    # -- Card content key / param maps ------------------------------------

    def _current_card_content_key(self) -> str:
        """The AI Card content variable: ``card_content_key`` from this adapter's own config
        (``extra``, seeded from the profile's ``dingtalk:`` block), else DingTalk's default."""
        return self._card_content_key_override or DEFAULT_AI_CARD_CONTENT_KEY

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

    @staticmethod
    def _image_card_param_map(media_id: str, caption: Optional[str]) -> Dict[str, str]:
        """Card param map for a DM image delivery via card_1_0."""
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

    # -- Per-item media metadata / error infrastructure ---------------------
    # DingTalk SDK message objects are either SimpleNamespace-like (attribute
    # access) or plain dicts depending on the SDK version and frame shape.
    # These helpers abstract over both so the same code path can read/write
    # download codes, resolved URLs, per-item media-type hints, and
    # per-item resolution-error markers uniformly.

    _MEDIA_CODE_KEYS = ("downloadCode", "pictureDownloadCode", "download_code")
    _MEDIA_URL_KEYS = ("downloadUrl", "download_url")
    _MEDIA_TYPE_KEYS = ("type", "msgtype", "msgType", "fileType", "file_type")
    _MEDIA_FILENAME_KEYS = ("fileName", "file_name", "filename", "name", "title")

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

    @classmethod
    def _first_media_ref(cls, item: Any) -> tuple[Optional[str], Optional[str], bool]:
        """Return ``(ref, key, is_download_code)`` for a DingTalk media item."""
        for key in cls._MEDIA_CODE_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, True
        for key in cls._MEDIA_URL_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, False
        return None, None, False

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

    @classmethod
    def _extension_for_media(
        cls,
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

    def _extract_media_errors(self, message: "ChatbotMessage") -> List[str]:
        """Collect per-attachment media-resolution failures attached by ``_resolve_media_codes``."""
        errors: List[str] = []
        image_content = getattr(message, "image_content", None)
        if image_content:
            error = self._media_error_for_item(image_content)
            if error:
                errors.append(error)
        for item in _rich_list(message) or ():
            error = self._media_error_for_item(item)
            if error:
                errors.append(error)
        return errors


class _IncomingHandler(dingtalk_stream.ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    """ChatbotHandler forwarding to the adapter (SDK >= 0.20: async ``process()`` gets a CallbackMessage ``.data`` dict)."""

    def __init__(self, adapter: DingTalkAdapter, loop: Optional[asyncio.AbstractEventLoop] = None):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._adapter, self._loop = adapter, loop

    def pre_start(self) -> None:
        """No-op hook the SDK calls on every handler before opening the WebSocket (missing → AttributeError)."""
        return

    async def process(self, message: "CallbackMessage"):
        """Convert to ChatbotMessage, dispatch as a background task, ACK immediately (blocking would stall SDK heartbeats)."""
        try:
            data = json.loads(message.data) if isinstance(message.data, str) else message.data
            chatbot_msg = ChatbotMessage.from_dict(data)
            data = data if isinstance(data, dict) else {}  # backfill fields from_dict() may not map (names vary across SDK versions)
            webhook = data.get("sessionWebhook") or data.get("session_webhook") or ""
            if webhook and not getattr(chatbot_msg, "session_webhook", None):
                chatbot_msg.session_webhook = webhook
            if not getattr(chatbot_msg, "is_in_at_list", False) and data.get("isInAtList"):
                chatbot_msg.is_in_at_list = True
            self._adapter._fill_missing_raw_fields(chatbot_msg, data)
            msg_id, conversation_id = getattr(chatbot_msg, "message_id", None) or "", getattr(chatbot_msg, "conversation_id", None) or ""
            if msg_id and conversation_id:
                self._adapter._spawn_bg(self._adapter._send_emotion(msg_id, conversation_id, self._adapter.REACTION_THINKING, recall=False))
            asyncio.create_task(self._safe_on_message(chatbot_msg))  # surfaces exceptions in logs instead of losing them
        except Exception:
            logger.exception("[%s] Error preparing incoming message", self._adapter.name)
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
        return AckMessage.STATUS_OK, "OK"

    async def _safe_on_message(self, chatbot_msg: "ChatbotMessage") -> None:
        try:
            await self._adapter._on_message(chatbot_msg)
        except Exception:
            logger.exception("[%s] Error processing incoming message", self._adapter.name)


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Out-of-process delivery (standalone_sender_fn) via the static robot webhook (DINGTALK_WEBHOOK_URL / extra
    ``webhook_url``) — per-session webhooks aren't available to cron jobs."""
    try:
        import httpx
    except ImportError:
        return send_error("httpx not installed")
    # Scoped: the webhook URL carries the robot's access_token and IS the delivery target — a raw
    # environ read would post a secondary profile's cron output to the default profile's robot.
    webhook_url = (getattr(pconfig, "extra", {}) or {}).get("webhook_url") or _get_scoped_secret("DINGTALK_WEBHOOK_URL", "")
    if not webhook_url:
        return send_error("DingTalk not configured. Set DINGTALK_WEBHOOK_URL env var or webhook_url in dingtalk platform extra config.")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(webhook_url, json={"msgtype": "text", "text": {"content": message}})
            resp.raise_for_status()
            data = resp.json()
        if data.get("errcode", 0) != 0:
            return send_error(f"DingTalk API error: {data.get('errmsg', 'unknown')}")
        return {"success": True, "platform": "dingtalk", "chat_id": chat_id}
    except Exception as e:
        try:  # send_message_tool._error redacts access_token from webhook URLs (lazy import avoids a circular)
            from tools.send_message_tool import _error as _redact_error
            return _redact_error(f"DingTalk send failed: {e}")
        except Exception:
            return send_error(f"DingTalk send failed: {e}")


def interactive_setup() -> None:
    """Configure DingTalk — QR scan (recommended) or manual credential entry."""
    from hermes_cli.config import save_env_value
    from hermes_cli.setup import prompt_choice
    from hermes_cli.cli_output import prompt, print_header, print_success, print_warning
    from hermes_cli.setup_platforms import declines_reconfigure
    print_header("DingTalk")
    if declines_reconfigure("DingTalk", "Reconfigure DingTalk?", "DINGTALK_CLIENT_ID"):
        return
    choices = ["QR Code Scan (Recommended, auto-obtain Client ID and Client Secret)", "Manual Input (Client ID and Client Secret)"]
    result = None
    if prompt_choice("Choose setup method", choices, default=0) == 0:
        try:
            from hermes_cli.dingtalk_auth import dingtalk_qr_auth
            result = dingtalk_qr_auth()
            if result is None:
                print_warning("QR auth incomplete, falling back to manual input.")
        except ImportError as exc:
            print_warning(f"QR auth module failed to load ({exc}), falling back to manual input.")
        if result is not None:
            for key, value in zip(("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"), result):
                save_env_value(key, value)
            return print_success("DingTalk configured via QR scan!")
    _manual_credential_entry(prompt, save_env_value, print_success)


def _manual_credential_entry(prompt, save_env_value, print_success) -> None:
    client_id = prompt("DingTalk Client ID (app key)")
    if not client_id:
        return
    save_env_value("DINGTALK_CLIENT_ID", client_id)
    if client_secret := prompt("DingTalk Client Secret", password=True):
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
    print_success("DingTalk credentials saved")


def _nested_allowed_users(yaml_cfg: dict, dingtalk_cfg: dict):
    """Allowlist from ``extra.allowed_users``: this block's own extra first, then ``gateway.platforms.dingtalk.extra`` and ``platforms.dingtalk.extra``."""
    _gw = yaml_cfg.get("gateway")
    containers = (_gw.get("platforms") if isinstance(_gw, dict) else None, yaml_cfg.get("platforms"))
    for holder in (dingtalk_cfg, *(c.get("dingtalk") if isinstance(c, dict) else None for c in containers)):
        _extra = holder.get("extra") if isinstance(holder, dict) else None
        if isinstance(_extra, dict) and _extra.get("allowed_users") is not None:
            return _extra.get("allowed_users")
    return None


_YAML_BRIDGE = (  # (yaml key, env var, kind) for apply_yaml_bridge
    ("require_mention", "DINGTALK_REQUIRE_MENTION", "lower"), ("mention_patterns", "DINGTALK_MENTION_PATTERNS", "json"),
    ("free_response_chats", "DINGTALK_FREE_RESPONSE_CHATS", "csv"), ("allowed_chats", "DINGTALK_ALLOWED_CHATS", "csv"),
    ("allowed_users", "DINGTALK_ALLOWED_USERS", "csv"),
    # ``dingtalk.allow_all_users`` in config.yaml needs to reach ``DINGTALK_ALLOW_ALL_USERS`` env so the
    # ``allow_all_env`` hook registered at register() consults it (authz_mixin reads the env, not YAML).
    # Without this bridge a YAML-only ``allow_all_users: true`` silently no-ops and every unrecognized DM
    # falls through to the pairing prompt under multiplex + installed secret-scope.
    ("allow_all_users", "DINGTALK_ALLOW_ALL_USERS", "lower"),
)


_CARD_EXTRA_KEYS = ("card_template_id", "card_content_key")


def _apply_yaml_config(yaml_cfg: dict, dingtalk_cfg: dict) -> dict | None:
    """``apply_yaml_config_fn`` (#24849): config.yaml dingtalk: keys → DINGTALK_* env (env wins; skipped under a
    multiplexed secondary profile's scope) + ``PlatformConfig.extra``. The docs put the allowlist at
    ``gateway.platforms.dingtalk.extra.allowed_users`` but gateway authz only consults DINGTALK_ALLOWED_USERS,
    so nested-only allowlists are bridged too."""
    cfg = dict(dingtalk_cfg)
    if cfg.get("allowed_users") is None:
        cfg["allowed_users"] = _nested_allowed_users(yaml_cfg, dingtalk_cfg)
    seeded = _apply_yaml_bridge(cfg, _YAML_BRIDGE) or {}
    # Card settings live in extra only (no env); the adapter reads them per profile from there.
    seeded.update({k: str(cfg[k]).strip() for k in _CARD_EXTRA_KEYS if str(cfg.get(k) or "").strip()})
    return seeded or None



def _is_connected(config) -> bool:
    """Connected when client_id + client_secret are present (PlatformConfig.extra first, then env)."""
    return all(_credentials(getattr(config, "extra", {})))



def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="dingtalk", label="DingTalk", adapter_factory=DingTalkAdapter, check_fn=dingtalk_deps_present,
        ensure_deps_fn=ensure_dingtalk_deps, is_connected=_is_connected, validate_config=_is_connected,
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"], install_hint="pip install 'dingtalk-stream>=0.20' httpx",
        setup_fn=interactive_setup, apply_yaml_config_fn=_apply_yaml_config, allowed_users_env="DINGTALK_ALLOWED_USERS",
        allow_all_env="DINGTALK_ALLOW_ALL_USERS", cron_deliver_env_var="DINGTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, emoji="🐳", allow_update_command=True,
    )
