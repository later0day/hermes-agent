"""Weixin (iLink) QR onboarding dashboard routes (fork).

Dashboard equivalent of ``hermes gateway setup``'s interactive Weixin QR login, shaped like the upstream
WhatsApp/Telegram onboarding in ``web_routers/messaging.py``. The CLI flow
(``gateway.platforms.weixin.qr_login``) prints an ASCII QR and blocks in a poll loop, which cannot drive a
browser, so the same iLink protocol is decomposed into a stateless create/poll pair (``weixin_create_qr`` /
``weixin_poll_qr``) plus a background poller that advances a server-side session record.

SECURITY: the confirmed bot token lives ONLY in the server-side record and is never returned by any payload --
the browser sees the scannable QR string, a coarse status and, after apply, the linked account id.
"""

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_deps import late
from hermes_cli.web_routers.messaging import _multiplex_port_binding_conflict, _onboarding_save_errors
from hermes_cli.web_server_gateway import _restart_gateway_after

router = APIRouter()
_log = logging.getLogger("hermes_cli.web_server")

_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
_write_platform_enabled = late("_write_platform_enabled", "hermes_cli.web_server_messaging")
save_env_value = late("save_env_value", "hermes_cli.config")
get_env_value = late("get_env_value", "hermes_cli.config")

_WEIXIN_ONBOARDING_TTL_SECONDS = 480
_WEIXIN_ONBOARDING_TERMINAL_STATUSES = {"connected", "error", "expired", "cancelled"}
_WEIXIN_QR_MAX_REFRESH = 3
_WEIXIN_SESSION_NOT_FOUND = "Weixin setup session was not found. Start a new setup."
_WEIXIN_EXPIRED = "Weixin QR setup expired. Start a new QR setup."
ILINK_BASE_URL_FALLBACK = "https://ilinkai.weixin.qq.com"
_DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


class WeixinOnboardingStart(BaseModel):
    profile: Optional[str] = None


class WeixinOnboardingApply(BaseModel):
    profile: Optional[str] = None


@dataclass
class _WeixinOnboardingSession:
    qrcode: str
    base_url: str
    expires_at: str
    expires_at_ts: float
    profile: str | None = None
    status: str = "starting"
    scan_state: str = "wait"  # wait | scaned (informational sub-state)
    qr_payload: str | None = None  # scannable QR string (safe to expose)
    refresh_count: int = 0
    account_id: str | None = None
    error: str | None = None
    # Credentials captured on confirm -- SERVER-SIDE ONLY, never serialized.
    _token: str | None = None
    _cred_base_url: str | None = None
    _user_id: str | None = None


_weixin_onboarding_sessions: dict[str, _WeixinOnboardingSession] = {}
_weixin_onboarding_lock = threading.RLock()


def _prune_weixin_onboarding_sessions() -> None:
    """Call with ``_weixin_onboarding_lock`` held."""
    now = time.time()
    for pairing_id, record in list(_weixin_onboarding_sessions.items()):
        if record.expires_at_ts <= now and record.status not in _WEIXIN_ONBOARDING_TERMINAL_STATUSES:
            record.status, record.error = "expired", _WEIXIN_EXPIRED
        if record.status in _WEIXIN_ONBOARDING_TERMINAL_STATUSES and record.expires_at_ts + 300 <= now:
            _weixin_onboarding_sessions.pop(pairing_id, None)


def _weixin_onboarding_payload(pairing_id: str, record: _WeixinOnboardingSession) -> dict[str, Any]:
    # Deliberately omits _token/_cred_base_url/_user_id.
    return {
        "pairing_id": pairing_id, "status": record.status, "scan_state": record.scan_state,
        "qr_payload": record.qr_payload, "expires_at": record.expires_at, "account_id": record.account_id,
        "error": record.error,
    }


def _live_record(pairing_id: str) -> _WeixinOnboardingSession | None:
    """Call with ``_weixin_onboarding_lock`` held: the record if it is still advancing."""
    record = _weixin_onboarding_sessions.get(pairing_id)
    return record if record and record.status not in _WEIXIN_ONBOARDING_TERMINAL_STATUSES else None


def _run_weixin_qr_poll(pairing_id: str) -> None:
    """Background poll loop advancing one onboarding session.

    Mirrors ``qr_login``'s state machine but writes into the shared record instead of printing. On ``expired``
    it refreshes the QR up to ``_WEIXIN_QR_MAX_REFRESH`` times (like the CLI), following any
    ``scaned_but_redirect`` host change so later polls hit the right shard.
    """
    from gateway.platforms.weixin import weixin_create_qr, weixin_poll_qr

    while True:
        with _weixin_onboarding_lock:
            record = _live_record(pairing_id)
            if record is None:
                return
            if record.expires_at_ts <= time.time():
                record.status, record.error = "expired", _WEIXIN_EXPIRED
                return
            qrcode, base_url = record.qrcode, record.base_url

        try:
            status_resp = asyncio.run(weixin_poll_qr(qrcode, base_url=base_url))
        except Exception as exc:  # transient transport error -- keep polling
            _log.debug("weixin onboarding poll error: %s", exc)
            time.sleep(1.5)
            continue
        status = str(status_resp.get("status") or "wait")

        if status in {"scaned", "scaned_but_redirect"}:
            redirect_host = str(status_resp.get("redirect_host") or "") if status == "scaned_but_redirect" else ""
            with _weixin_onboarding_lock:
                if record := _live_record(pairing_id):
                    record.scan_state = "scaned"
                    if redirect_host:
                        record.base_url = f"https://{redirect_host}"
            time.sleep(1.0 if redirect_host else 1.5)
            continue

        if status == "expired":
            with _weixin_onboarding_lock:
                if (record := _live_record(pairing_id)) is None:
                    return
                record.refresh_count += 1
                if record.refresh_count > _WEIXIN_QR_MAX_REFRESH:
                    record.status, record.error = "error", "Weixin QR expired repeatedly. Start a new QR setup."
                    return
            try:
                fresh = asyncio.run(weixin_create_qr())
            except Exception as exc:
                with _weixin_onboarding_lock:
                    if record := _live_record(pairing_id):
                        record.status, record.error = "error", f"Failed to refresh Weixin QR: {exc}"
                return
            with _weixin_onboarding_lock:
                if (record := _live_record(pairing_id)) is None:
                    return
                record.qrcode = fresh["qrcode"]
                record.base_url = fresh.get("base_url") or ILINK_BASE_URL_FALLBACK
                record.qr_payload = fresh.get("scan_data") or fresh["qrcode"]
                record.scan_state = "wait"
            time.sleep(1.0)
            continue

        if status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            token = str(status_resp.get("bot_token") or "")
            with _weixin_onboarding_lock:
                if (record := _live_record(pairing_id)) is None:
                    return
                if not account_id or not token:
                    record.status, record.error = "error", "Weixin login confirmed but credentials were incomplete."
                    return
                record.account_id, record._token = account_id, token
                record._cred_base_url = str(status_resp.get("baseurl") or "") or record.base_url
                record._user_id = str(status_resp.get("ilink_user_id") or "")
                record.scan_state, record.status = "scaned", "connected"
            return

        time.sleep(1.5)  # "wait" or an unknown status: keep polling until the TTL


@router.post("/api/messaging/weixin/onboarding/start")
async def start_weixin_onboarding(body: WeixinOnboardingStart):
    # A secondary profile can't own the shared listener under multiplexing.
    if conflict := _multiplex_port_binding_conflict("weixin", body.profile):
        raise HTTPException(status_code=409, detail=conflict)
    try:
        from gateway.platforms.weixin import check_weixin_requirements, weixin_create_qr
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Weixin adapter import failed: {exc}") from exc
    if not check_weixin_requirements():
        raise HTTPException(status_code=500, detail="Weixin setup needs aiohttp and cryptography installed.")
    try:
        qr = await weixin_create_qr()
    except Exception as exc:
        _log.exception("Weixin onboarding: QR create failed")
        raise HTTPException(status_code=502, detail=f"Could not fetch a Weixin QR code: {exc}") from exc

    pairing_id = secrets.token_urlsafe(16)
    expires_at_ts = time.time() + _WEIXIN_ONBOARDING_TTL_SECONDS
    record = _WeixinOnboardingSession(
        qrcode=qr["qrcode"], base_url=qr.get("base_url") or ILINK_BASE_URL_FALLBACK,
        expires_at=datetime.fromtimestamp(expires_at_ts, UTC).isoformat(), expires_at_ts=expires_at_ts,
        profile=body.profile, status="waiting", qr_payload=qr.get("scan_data") or qr["qrcode"])
    with _weixin_onboarding_lock:
        _prune_weixin_onboarding_sessions()
        _weixin_onboarding_sessions[pairing_id] = record
    threading.Thread(target=_run_weixin_qr_poll, args=(pairing_id,), daemon=True).start()
    return _weixin_onboarding_payload(pairing_id, record)


@router.get("/api/messaging/weixin/onboarding/{pairing_id}")
async def get_weixin_onboarding_status(pairing_id: str):
    with _weixin_onboarding_lock:
        _prune_weixin_onboarding_sessions()
        record = _weixin_onboarding_sessions.get(pairing_id)
        if not record:
            raise HTTPException(status_code=404, detail=_WEIXIN_SESSION_NOT_FOUND)
        if record.status == "expired":
            raise HTTPException(status_code=410, detail=record.error or "Weixin setup expired.")
        return _weixin_onboarding_payload(pairing_id, record)


@router.post("/api/messaging/weixin/onboarding/{pairing_id}/apply")
async def apply_weixin_onboarding(pairing_id: str, body: WeixinOnboardingApply, profile: Optional[str] = None):
    with _weixin_onboarding_lock:
        _prune_weixin_onboarding_sessions()
        record = _weixin_onboarding_sessions.get(pairing_id)
        if not record:
            raise HTTPException(status_code=404, detail=_WEIXIN_SESSION_NOT_FOUND)
        if record.status != "connected":
            raise HTTPException(status_code=409, detail="Weixin setup is not confirmed yet.")
        account_id, token = record.account_id or "", record._token or ""
        cred_base_url, user_id, record_profile = record._cred_base_url or "", record._user_id or "", record.profile
    if not account_id or not token:
        raise HTTPException(status_code=409, detail="Weixin credentials are incomplete.")

    effective_profile = body.profile or profile or record_profile

    def _apply():
        from gateway.platforms.weixin import save_weixin_account
        from hermes_constants import get_hermes_home

        with _profile_scope(effective_profile):
            save_env_value("WEIXIN_ACCOUNT_ID", account_id)
            save_env_value("WEIXIN_TOKEN", token)
            if cred_base_url:
                save_env_value("WEIXIN_BASE_URL", cred_base_url)
            save_env_value("WEIXIN_CDN_BASE_URL", get_env_value("WEIXIN_CDN_BASE_URL") or _DEFAULT_CDN_BASE_URL)
            # Mirror the CLI: persist the account credential file too so the adapter's context-token store keys
            # off a known account_id. get_hermes_home() honours the profile scope's HERMES_HOME override.
            try:
                save_weixin_account(str(get_hermes_home()), account_id=account_id, token=token,
                                    base_url=cred_base_url, user_id=user_id)
            except Exception:
                _log.warning("weixin onboarding: account credential file write skipped", exc_info=True)
            _write_platform_enabled("weixin", True)

    with _onboarding_save_errors("Weixin onboarding apply failed", "Failed to save Weixin setup."):
        await asyncio.to_thread(_apply)

    with _weixin_onboarding_lock:
        _weixin_onboarding_sessions.pop(pairing_id, None)

    # Best-effort restart, like Telegram onboarding: the save stays authoritative; a failed restart is reported.
    restart_result = _restart_gateway_after(effective_profile, what="Weixin onboarding", label="Weixin onboarding")
    return {
        "ok": True, "platform": "weixin", "account_id": account_id,
        "needs_restart": not restart_result["restart_started"], **restart_result,
    }


@router.delete("/api/messaging/weixin/onboarding/{pairing_id}")
async def cancel_weixin_onboarding(pairing_id: str):
    with _weixin_onboarding_lock:
        record = _weixin_onboarding_sessions.pop(pairing_id, None)
    if record:
        record.status = "cancelled"
    return {"ok": True}
