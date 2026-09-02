"""Dashboard Weixin QR onboarding: real routes, fake iLink transport, token never leaves the server."""

from __future__ import annotations

import time

import pytest

import gateway.platforms.weixin as weixin
from hermes_cli.web_routers import weixin_onboarding as wx

TOKEN = "BOT-TOKEN-SHOULD-NEVER-LEAK"


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


@pytest.fixture()
def ilink(monkeypatch):
    """Scripted iLink: each poll pops the next status; create_qr counts refreshes."""
    state = {"statuses": [], "creates": 0, "restarts": []}

    async def create_qr(*, bot_type="3"):
        state["creates"] += 1
        return {"qrcode": f"qr{state['creates']}", "qrcode_url": f"https://liteapp/qr{state['creates']}",
                "scan_data": f"https://liteapp/qr{state['creates']}", "base_url": "https://ilink.test"}

    async def poll_qr(qrcode, *, base_url="https://ilink.test"):
        return dict(state["statuses"].pop(0)) if state["statuses"] else {"status": "wait"}

    monkeypatch.setattr(weixin, "weixin_create_qr", create_qr)
    monkeypatch.setattr(weixin, "weixin_poll_qr", poll_qr)
    monkeypatch.setattr(weixin, "check_weixin_requirements", lambda: True)
    monkeypatch.setattr(wx, "_restart_gateway_after", lambda profile, **kw: (
        state["restarts"].append(profile) or {"restart_started": True, "restart_action": "gateway-restart"}))
    monkeypatch.setattr(wx.time, "sleep", lambda _s: None)
    wx._weixin_onboarding_sessions.clear()
    yield state
    wx._weixin_onboarding_sessions.clear()


def _wait_status(client, pairing_id, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/messaging/weixin/onboarding/{pairing_id}")
        if response.status_code != 200 or response.json()["status"] == want:
            return response
        time.sleep(0.02)
    raise AssertionError(f"status never became {want}: {response.json()}")


def test_full_flow_links_account_and_never_exposes_the_token(ilink):
    from hermes_constants import get_hermes_home

    ilink["statuses"] = [{"status": "scaned"}, {"status": "confirmed", "ilink_bot_id": "bot@im",
                                                "bot_token": TOKEN, "baseurl": "https://shard.test",
                                                "ilink_user_id": "u1"}]
    client = _client()
    start = client.post("/api/messaging/weixin/onboarding/start", json={})
    assert start.status_code == 200 and start.json()["qr_payload"] == "https://liteapp/qr1"
    pairing_id = start.json()["pairing_id"]

    connected = _wait_status(client, pairing_id, "connected")
    assert connected.json()["account_id"] == "bot@im"
    assert TOKEN not in start.text + connected.text

    applied = client.post(f"/api/messaging/weixin/onboarding/{pairing_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    assert TOKEN not in applied.text
    assert applied.json()["account_id"] == "bot@im" and applied.json()["needs_restart"] is False
    assert ilink["restarts"] == [None]

    env = (get_hermes_home() / ".env").read_text()
    assert "WEIXIN_ACCOUNT_ID=bot@im" in env and f"WEIXIN_TOKEN={TOKEN}" in env
    assert "WEIXIN_BASE_URL=https://shard.test" in env
    assert list((get_hermes_home() / "weixin").rglob("*")), "account credential file was not written"
    # The channel list reports only that an account is linked.
    platforms = client.get("/api/messaging/platforms").json()
    platforms = platforms if isinstance(platforms, list) else platforms.get("platforms", [])
    row = next(p for p in platforms if p["id"] == "weixin")
    assert row["weixin_setup"]["account_set"] is True
    assert TOKEN not in client.get("/api/messaging/platforms").text
    # The session is consumed by apply.
    assert client.get(f"/api/messaging/weixin/onboarding/{pairing_id}").status_code == 404


def test_apply_before_confirmation_is_rejected_and_cancel_ends_the_session(ilink):
    client = _client()
    pairing_id = client.post("/api/messaging/weixin/onboarding/start", json={}).json()["pairing_id"]
    assert client.post(f"/api/messaging/weixin/onboarding/{pairing_id}/apply", json={}).status_code == 409
    assert client.delete(f"/api/messaging/weixin/onboarding/{pairing_id}").json() == {"ok": True}
    assert client.get(f"/api/messaging/weixin/onboarding/{pairing_id}").status_code == 404
    assert client.get("/api/messaging/weixin/onboarding/nope").status_code == 404


def test_expired_qr_refreshes_then_gives_up(ilink):
    ilink["statuses"] = [{"status": "expired"}] * (wx._WEIXIN_QR_MAX_REFRESH + 1)
    client = _client()
    pairing_id = client.post("/api/messaging/weixin/onboarding/start", json={}).json()["pairing_id"]
    final = _wait_status(client, pairing_id, "error")
    assert final.json()["status"] == "error" and "repeatedly" in final.json()["error"]
    assert ilink["creates"] == 1 + wx._WEIXIN_QR_MAX_REFRESH


def test_session_past_its_ttl_reports_gone(ilink):
    client = _client()
    pairing_id = client.post("/api/messaging/weixin/onboarding/start", json={}).json()["pairing_id"]
    with wx._weixin_onboarding_lock:
        wx._weixin_onboarding_sessions[pairing_id].expires_at_ts = time.time() - 1
    assert client.get(f"/api/messaging/weixin/onboarding/{pairing_id}").status_code == 410
