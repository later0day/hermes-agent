"""Regression tests for the satellite adapter-borrow escape hatch (Phase E5,
port of fork af1247d9f5) in ``_resolve_target_transport``.

Under ``gateway.multiplex_profiles`` a satellite profile served via a dynamic
``/agent use`` binding (not a static ``profile_routes`` entry) holds NO
credential of its own for the bound platform — a second credential is a
``duplicate_credential`` fatal. The multiplex ticker
(``scheduler_provider._augment_secondary_adapters_from_shared``) lends the
shared PRIMARY adapter into this profile's per-tick adapter dict for exactly
the platforms it has a binding for. Without this escape hatch,
``resolve_delivery_transport`` still vetoes the borrowed adapter because the
satellite's OWN ``platforms.<p>`` config correctly reads that platform as
disabled (it has no credential) — so every delivery fell through to a
standalone (credentialless) send and failed.

The escape hatch authorizes the borrow with the SAME primary-routing
predicate preflight uses (``_delivery_platform_routed_from_primary_gateway``:
static ``profile_routes`` OR a dynamic source binding for this profile), so
an unrouted/unbound platform still fails closed.
"""
import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from cron.scheduler_delivery import _resolve_target_transport
from gateway.config import Platform, PlatformConfig


def _disabled_config():
    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=False)}
    return config


def test_borrowed_adapter_delivers_when_primary_routes_platform():
    """Own config disabled (no credential) + a borrowed adapter present in
    the dict-augmented tick map + the primary routes this platform to this
    profile → the borrow is authorized and the transport resolves to the
    SHARED adapter, with the per-call config mutated so the live-send path's
    re-resolve (a fresh ``DeliveryRouter`` over the same config+adapters)
    also succeeds."""
    borrowed = object()
    config = _disabled_config()
    with patch("cron.scheduler_preflight._delivery_platform_routed_from_primary_gateway",
               return_value=True):
        resolved, err = _resolve_target_transport(
            {"id": "j"}, Platform.DISCORD, "discord",
            {"platform": "discord", "chat_id": "C1"},
            {Platform.DISCORD: borrowed}, config)
    assert err is None, err
    transport, pconfig, runtime_adapter, target_adapters = resolved
    assert runtime_adapter is borrowed
    assert pconfig.enabled is True
    assert config.platforms[Platform.DISCORD].enabled is True


def test_no_borrow_when_not_routed_fails_closed():
    """Same setup, but the primary does NOT route this platform to this
    profile (no static route, no binding) → fails closed exactly as before
    the escape hatch existed; the satellite's own disabled reading stands
    unmutated."""
    borrowed = object()
    config = _disabled_config()
    with patch("cron.scheduler_preflight._delivery_platform_routed_from_primary_gateway",
               return_value=False):
        resolved, err = _resolve_target_transport(
            {"id": "j"}, Platform.DISCORD, "discord",
            {"platform": "discord", "chat_id": "C1"},
            {Platform.DISCORD: borrowed}, config)
    assert resolved is None
    assert "not configured/enabled" in err
    assert config.platforms[Platform.DISCORD].enabled is False


def test_no_borrow_without_live_adapter_fails_closed():
    """Own config disabled AND no adapter for this platform in the tick map
    at all (even though the primary routes it) → nothing to borrow, fails
    closed rather than fabricating a transport."""
    config = _disabled_config()
    with patch("cron.scheduler_preflight._delivery_platform_routed_from_primary_gateway",
               return_value=True):
        resolved, err = _resolve_target_transport(
            {"id": "j"}, Platform.DISCORD, "discord",
            {"platform": "discord", "chat_id": "C1"}, {}, config)
    assert resolved is None
    assert "not configured/enabled" in err


def test_own_enabled_config_uses_native_path_not_borrow():
    """A profile with its OWN enabled config + adapter resolves via the
    ordinary native path and never even consults the borrow predicate — the
    escape hatch only fires when the native resolve has already failed."""
    own_adapter = object()
    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
    with patch(
        "cron.scheduler_preflight._delivery_platform_routed_from_primary_gateway",
        side_effect=AssertionError("borrow predicate must not be consulted"),
    ):
        resolved, err = _resolve_target_transport(
            {"id": "j"}, Platform.DISCORD, "discord",
            {"platform": "discord", "chat_id": "C1"},
            {Platform.DISCORD: own_adapter}, config)
    assert err is None, err
    assert resolved[2] is own_adapter


def test_end_to_end_delivery_borrows_shared_adapter_no_standalone_fallthrough():
    """``_deliver_result`` end-to-end: own config disabled, adapter borrowed
    via a plain dict (as the multiplex ticker hands a bound secondary
    profile), primary routes the platform → the live send succeeds through
    the borrowed adapter and NEVER falls through to the satellite's own
    (credentialless) standalone path."""
    from cron.scheduler import _deliver_result

    job = {"id": "a7ae1520356c", "name": "brief", "deliver": "discord:C1"}
    borrowed = MagicMock()
    borrowed.sent = []

    async def send(chat_id, content, metadata=None):
        borrowed.sent.append(chat_id)
        return {"success": True, "message_id": "m1"}
    borrowed.send = send

    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    standalone_calls = []

    async def _fake_send_to_platform(platform, pconfig, chat_id, text, **kwargs):
        standalone_calls.append(chat_id)
        return {"success": False, "error": "DISCORD_BOT_TOKEN is not set"}

    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=False)}
    config.get_home_channel = lambda p: None

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform", _fake_send_to_platform), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
         patch("cron.scheduler_preflight._delivery_platform_routed_from_primary_gateway",
               return_value=True):
        error = _deliver_result(job, "hello", adapters={Platform.DISCORD: borrowed}, loop=loop)

    assert error is None, error
    assert borrowed.sent == ["C1"]
    assert standalone_calls == []
