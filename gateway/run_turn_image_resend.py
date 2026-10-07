"""DingTalk "send me that image again" shortcut (fork).

A user asking for the image they sent earlier gets the file re-delivered straight from the
session transcript's ``[Image attached at: …]`` marker, without an agent turn. The intent match is
deliberately tight — a resend verb next to an image word plus a "just now / last one" reference —
so ordinary requests that merely mention an image ("redesign this image and send it", "describe
the image from before") still reach the agent.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from agent.i18n import t
from gateway.config import Platform

logger = logging.getLogger("gateway.run")

# Returned when the message is not a resend request (or the platform has no support), so the
# normal turn proceeds.
RECENT_IMAGE_RESEND_NOT_HANDLED = object()

_RECENT = r"(?:刚才|刚刚|上一张|最近|之前)"
_IMAGE = r"(?:图片|照片|图)"
_LINK = r"(?:的|那张|那个)?"
_RESEND_IMAGE_PATTERNS = re.compile(
    r"(?:"
    rf"{_RECENT}{_LINK}{_IMAGE}(?:重新|再)?发"           # 刚才的图片重新发一下 / 刚才那张图片发过来
    rf"|(?:重新|再)发(?:一下|一次|一遍)?{_LINK}{_IMAGE}"  # 重新发一下那个图片
    rf"|发(?:一下|给我)?{_RECENT}{_LINK}{_IMAGE}"         # 发一下刚才的图片
    rf"|{_IMAGE}(?:重新|再)发"                            # 图片重新发
    r"|(?:re)?send\s+(?:me\s+)?(?:the\s+)?(?:last|latest|recent)\s+image"
    r")",
    re.IGNORECASE,
)

_IMAGE_ATTACHED_RE = re.compile(r"\[Image attached at:\s*(.+?)\]")


def wants_recent_image_resend(text: Optional[str]) -> bool:
    """True when *text* asks to get the last image sent again."""
    return bool(_RESEND_IMAGE_PATTERNS.search(text or ""))


def find_latest_attached_image_path(messages: list) -> Optional[str]:
    """Newest-first: the path of the most recent attached image that still exists, or None."""
    for msg in reversed(messages):
        content = msg.get("content") or ""
        for m in reversed(list(_IMAGE_ATTACHED_RE.finditer(content))):
            path = m.group(1).strip()
            if os.path.isfile(path):
                return path
    return None


async def maybe_resend_recent_image(runner, event, source, session_id: str):
    """Re-deliver the session's most recent image when asked to.

    Returns ``RECENT_IMAGE_RESEND_NOT_HANDLED`` when this is not a resend request or the platform
    lacks support; ``None`` after a successful re-delivery; otherwise a notice to send back.
    """
    if source.platform != Platform.DINGTALK or not wants_recent_image_resend(event.text):
        return RECENT_IMAGE_RESEND_NOT_HANDLED
    adapter = runner._delivery_adapter_for(source)
    session_db = getattr(runner, "_session_db", None)
    if adapter is None or not hasattr(adapter, "send_image_file") or session_db is None:
        return RECENT_IMAGE_RESEND_NOT_HANDLED

    try:
        messages = await session_db.get_messages(session_id) or []
    except Exception:
        logger.warning("image resend: could not read session %s", session_id, exc_info=True)
        messages = []
    path = find_latest_attached_image_path(messages)
    if path is None:
        return t("gateway.image_resend.not_found")

    try:
        await adapter.send_image_file(
            chat_id=source.chat_id, image_path=path, caption=t("gateway.image_resend.caption"),
            reply_to=event.message_id, metadata=None,
        )
    except Exception:
        logger.warning("image resend: send_image_file failed", exc_info=True)
        return t("gateway.image_resend.failed")

    try:
        await session_db.append_message(
            session_id, "user", event.text or "",
            platform_message_id=str(event.message_id) if event.message_id else None,
        )
        await session_db.append_message(session_id, "assistant", f"[resent image attachment: {path}]")
    except Exception:
        logger.debug("image resend: could not record the exchange", exc_info=True)
    return None
