"""Relay egress discriminators a cron fire carries from its persisted job origin."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger("cron.scheduler")


def stamp_origin_discriminators(t: Any, route_metadata: dict, media_metadata: dict) -> None:
    """Stamp the origin's ``scope_id`` / ``user_id`` / ``chat_type`` / ``user_id_alt`` onto a live send's metadata.

    Relay egress is fail-closed on a discriminator and the RelayAdapter's caches are cold after every
    boot, so the persisted origin supplies them. Origin targets only (a fan-out target's recipient is not
    the origin's author); ``setdefault`` never overrides router or home stamping; ``user_id`` is read by
    relay transports only.
    """
    origin = t.origin if t.origin_target else {}
    discriminators = (
        ("scope_id", origin.get("scope_id")),
        ("user_id", t.origin_user_id if t.is_relay else None),
        # Conversation kind + alternate sender id: an adapter whose send API differs for DMs (DingTalk
        # 1:1 vs group robot send) otherwise guesses "group" once a restart cleared its message cache.
        ("chat_type", origin.get("chat_type")),
        ("user_id_alt", origin.get("user_id_alt")),
    )
    for key, value in discriminators:
        if value:
            route_metadata.setdefault(key, str(value))
            media_metadata.setdefault(key, str(value))


def _binding_session_webhook_status(extra: dict | None) -> tuple[str | None, str | None]:
    """Return a valid DingTalk session webhook, or why it cannot be used."""
    if not isinstance(extra, dict):
        return None, "DingTalk fallback webhook is missing. Send `/agent webhook` from this chat."
    webhook = str(extra.get("session_webhook") or "").strip()
    if not webhook.startswith(("http://", "https://")):
        return None, "DingTalk fallback webhook is missing. Send `/agent webhook` from this chat."
    try:
        expires_ms = int(extra.get("session_webhook_expired_time") or 0)
    except (TypeError, ValueError):
        expires_ms = 0
    if expires_ms and expires_ms <= int(time.time() * 1000):
        return None, "DingTalk session_webhook is expired. Send `/agent webhook` from this chat again."
    return webhook, None


def _job_profile_candidates(job: dict) -> list[str]:
    """Return possible profile names for matching source-agent bindings."""
    from cron.scheduler_delivery import _resolve_origin

    origin = _resolve_origin(job) or {}
    candidates: list[str] = []
    value = str(origin.get("profile_name") or "").strip()
    if value:
        candidates.append(value)
    for key in ("run_profile", "profile", "owner_profile"):
        value = str(job.get(key) or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _resolve_dingtalk_origin_session_webhook_status(
    job: dict, chat_id: str,
) -> tuple[str | None, str | None]:
    """Resolve a DingTalk session webhook captured by ``/agent webhook``."""
    from cron.scheduler_delivery import _resolve_origin

    origin = _resolve_origin(job) or {}
    if str(origin.get("platform") or "").lower() != "dingtalk":
        return None, None
    target_chat_id = str(chat_id or origin.get("chat_id") or "").strip()
    if not target_chat_id:
        return None, None

    try:
        from gateway.source_agent_binding import SourceAgentBindingStore
    except ImportError as exc:
        logger.debug("DingTalk origin webhook lookup unavailable: %s", exc)
        return None, None

    store = None
    try:
        store = SourceAgentBindingStore()
        profile_candidates = _job_profile_candidates(job)
        bindings = []
        if profile_candidates:
            seen_keys = set()
            for profile_name in profile_candidates:
                for binding in store.list_bindings(profile_name=profile_name):
                    key = getattr(binding, "source_binding_key", None)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        bindings.append(binding)
        else:
            bindings = store.list_bindings()

        matched_issue = None
        for binding in bindings:
            target = getattr(binding, "fallback_target", None) or {}
            if not isinstance(target, dict):
                continue
            if str(target.get("platform") or "").lower() != "dingtalk":
                continue
            if str(target.get("chat_id") or "").strip() != target_chat_id:
                continue
            webhook, issue = _binding_session_webhook_status(
                getattr(binding, "fallback_extra", None))
            if webhook:
                return webhook, None
            matched_issue = issue or matched_issue
        if matched_issue:
            return None, matched_issue
    except (OSError, sqlite3.Error) as exc:
        logger.warning(
            "Job '%s': failed to resolve DingTalk origin session_webhook: %s",
            job.get("id", "?"), exc)
        return None, "failed to resolve DingTalk origin session_webhook"
    finally:
        if store is not None:
            try:
                store.close()
            except sqlite3.Error:
                logger.debug("Failed to close DingTalk origin binding store", exc_info=True)
    return None, None


async def _send_dingtalk_session_webhook(session_webhook: str, content: str) -> dict:
    """Send cron output through a captured DingTalk session webhook."""
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": "Hermes", "text": str(content or "")},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.post(session_webhook, json=payload)
        if resp.status_code >= 300:
            return {"error": (
                f"DingTalk session_webhook send failed HTTP {resp.status_code}: {resp.text[:200]}")}
        try:
            data = resp.json()
        except ValueError:
            return {"error": "DingTalk session_webhook returned a non-JSON success response"}
        if not isinstance(data, dict):
            return {"error": "DingTalk session_webhook returned an invalid success response"}
        if data.get("errcode") != 0:
            detail = str(data.get("errmsg") or "unknown")[:200]
            return {"error": f"DingTalk session_webhook API error {data.get('errcode')}: {detail}"}
        return {"success": True, "platform": "dingtalk"}
    except (httpx.HTTPError, ValueError) as exc:
        detail = str(exc).replace(session_webhook, "<redacted>")[:300]
        return {"error": f"DingTalk session_webhook send failed: {detail}"}
