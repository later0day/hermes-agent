"""A failed relay cron delivery must fail closed — never retry natively.

Relay owns the logical destination and its connector owns the platform
credential, so a native retry could double-deliver and cannot be
authenticated correctly.  ``_deliver_result`` therefore stops at the relay
branch instead of falling through to the standalone ``_send_to_platform``
path.

Upstream documents this in a comment above the branch; the fork's diff
dropped that comment while keeping the code, so nothing explained (or
guarded) the invariant.  These tests pin the behaviour, including the fork's
own DingTalk session-webhook fallback, which is inserted *before* the relay
branch and so has to be checked for a bypass.
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import _deliver_result


def _fake_run_coro(coro, _loop):
    """Run a coroutine inline, mimicking asyncio.run_coroutine_threadsafe."""
    import asyncio as _asyncio

    future: Future = Future()
    try:
        future.set_result(_asyncio.run(coro))
    except BaseException as exc:  # noqa: BLE001
        future.set_exception(exc)
    return future


def _relay_config(platform):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig

    return GatewayConfig(
        platforms={
            Platform.RELAY: PlatformConfig(enabled=True),
            platform: PlatformConfig(
                enabled=False,
                home_channel=HomeChannel(
                    platform=platform,
                    chat_id="D123",
                    name="Owner DM",
                    user_id="U123",
                ),
            ),
        },
    )


def _failing_relay(platform):
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda p: p == platform
    relay.send_for_platform = AsyncMock(return_value=MagicMock(success=False))
    relay.send_voice = AsyncMock(return_value=MagicMock(success=False))
    relay.supports_inchannel_continuable = False
    return relay


def test_failed_relay_delivery_does_not_retry_standalone(monkeypatch):
    """Relay send returns success=False -> no native fallback send."""
    from gateway.config import Platform

    relay = _failing_relay(Platform.SLACK)
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock(return_value={"success": True})
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "D123")

    with (
        patch("gateway.config.load_gateway_config", return_value=_relay_config(Platform.SLACK)),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_run_coro),
        patch("tools.send_message_tool._send_to_platform", new=standalone_send),
    ):
        result = _deliver_result(
            {"id": "relay-fail", "deliver": "slack"},
            "scheduled result",
            adapters={Platform.RELAY: relay},
            loop=loop,
        )

    relay.send_for_platform.assert_awaited_once()
    standalone_send.assert_not_awaited(), (
        "relay delivery failed but cron retried natively — double-delivery risk "
        "with the wrong credential"
    )
    assert result is not None, "a failed relay delivery must be reported, not swallowed"
    assert "relay" in result.lower() or "slack" in result.lower()


def test_raising_relay_delivery_does_not_retry_standalone(monkeypatch):
    """Relay send raises -> still fail closed (the except-branch path)."""
    from gateway.config import Platform

    relay = _failing_relay(Platform.SLACK)
    relay.send_for_platform = AsyncMock(side_effect=RuntimeError("socket closed"))
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock(return_value={"success": True})
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "D123")

    with (
        patch("gateway.config.load_gateway_config", return_value=_relay_config(Platform.SLACK)),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_run_coro),
        patch("tools.send_message_tool._send_to_platform", new=standalone_send),
    ):
        result = _deliver_result(
            {"id": "relay-raise", "deliver": "slack"},
            "scheduled result",
            adapters={Platform.RELAY: relay},
            loop=loop,
        )

    standalone_send.assert_not_awaited(), (
        "a raising relay send fell through to the native standalone path"
    )
    assert result is not None


def test_relay_fronted_dingtalk_does_not_bypass_via_session_webhook(monkeypatch):
    """The fork's DingTalk webhook fallback must not undercut fail-closed.

    ``_resolve_dingtalk_origin_session_webhook_status`` keys off the job's
    ORIGIN, not the transport, and its fallback block sits *above* the relay
    branch in ``_deliver_result``.  A relay-fronted DingTalk job whose relay
    send fails must therefore still fail closed rather than re-delivering
    through a captured session webhook the connector doesn't own.
    """
    from gateway.config import Platform

    relay = _failing_relay(Platform.DINGTALK)
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock(return_value={"success": True})
    webhook_send = AsyncMock(return_value={"success": True})
    monkeypatch.setenv("DINGTALK_HOME_CHANNEL", "D123")

    job = {
        "id": "relay-dingtalk",
        "deliver": "dingtalk",
        "origin": {"platform": "dingtalk", "chat_id": "D123"},
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=_relay_config(Platform.DINGTALK)),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_run_coro),
        patch("tools.send_message_tool._send_to_platform", new=standalone_send),
        patch(
            "cron.scheduler._resolve_dingtalk_origin_session_webhook_status",
            return_value=("https://oapi.dingtalk.com/robot/sendBySession?x=1", None),
        ),
        patch("cron.scheduler._send_dingtalk_session_webhook", new=webhook_send),
    ):
        result = _deliver_result(
            job,
            "scheduled result",
            adapters={Platform.RELAY: relay},
            loop=loop,
        )

    standalone_send.assert_not_awaited(), (
        "relay-fronted DingTalk fell through to the native standalone path"
    )
    assert not webhook_send.await_count, (
        "relay-fronted DingTalk delivery failed but cron re-delivered through a "
        "captured session webhook — this bypasses the relay fail-closed guard "
        "and can double-deliver with a credential the connector does not own"
    )
    assert result is not None


def test_native_dingtalk_still_uses_the_session_webhook_fallback(monkeypatch):
    """The relay gate must not disable the fork's normal webhook fallback.

    With no relay in play (standalone cron, no live adapter) a captured
    session webhook is the *only* way to reach the origin chat, and it is a
    credential this process legitimately owns.  It must still fire.
    """
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig

    config = GatewayConfig(
        platforms={
            Platform.DINGTALK: PlatformConfig(
                enabled=True,
                home_channel=HomeChannel(
                    platform=Platform.DINGTALK,
                    chat_id="cidAAA",
                    name="Owner Group",
                ),
            ),
        },
    )
    standalone_send = AsyncMock(return_value={"success": True})
    webhook_send = AsyncMock(return_value={"success": True})

    job = {
        "id": "native-dingtalk",
        "deliver": "dingtalk",
        "origin": {"platform": "dingtalk", "chat_id": "cidAAA"},
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("tools.send_message_tool._send_to_platform", new=standalone_send),
        patch(
            "cron.scheduler._resolve_dingtalk_origin_session_webhook_status",
            return_value=("https://oapi.dingtalk.com/robot/sendBySession?x=1", None),
        ),
        patch("cron.scheduler._send_dingtalk_session_webhook", new=webhook_send),
    ):
        result = _deliver_result(job, "scheduled result", adapters=None, loop=None)

    assert webhook_send.await_count == 1, (
        "the relay gate broke the normal (non-relay) DingTalk session_webhook "
        "delivery path"
    )
    assert result is None, f"native webhook delivery should succeed, got {result!r}"


def test_relay_fail_closed_comment_documents_the_invariant():
    """The upstream rationale comment must stay next to the branch it explains."""
    import inspect

    import cron.scheduler as scheduler

    src = inspect.getsource(scheduler._deliver_result)
    idx = src.index('f"relay delivery to {platform_name}:{chat_id} failed"')
    preamble = src[:idx]
    branch = preamble[preamble.rindex("if transport is not None and transport.is_relay:") :]
    assert "fail closed" in branch, (
        "the relay fail-closed rationale comment was dropped; without it the "
        "next reader sees an unexplained early `continue` and may 'fix' it into "
        "a native retry"
    )
