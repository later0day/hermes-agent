"""Satellite cron delivery through the primary gateway's shared adapter.

Under ``gateway.multiplex_profiles`` a satellite profile (e.g. ``xcx``) serves
many dingtalk groups bound at runtime via ``/agent use`` (dynamic
``source_agent_bindings``) but holds NO dingtalk credential of its own — a
second copy of the primary's token is a ``duplicate_credential`` fatal, so the
satellite's own ``platforms.dingtalk`` config reads as disabled.

The primary gateway's in-process ticker fires the satellite's jobs with the
primary's LIVE dingtalk adapter. Two gates used to defeat that delivery:

1. The tick's per-profile adapter map excluded the shared adapter
   (scheduler_provider now lends it in — ``_augment_secondary_adapters_from
   _shared``).
2. Even with the adapter present, ``resolve_delivery_transport`` rejected it
   because THIS home's own dingtalk config is disabled, and the
   ``pconfig.enabled`` gate then logged "platform 'dingtalk' not
   configured/enabled" and skipped delivery.

This module covers gate (2): the satellite-borrow escape hatch in
``_deliver_result`` — symmetric to the relay escape hatch — that accepts the
borrowed live adapter when the primary gateway routes the platform to this
profile (static route OR dynamic binding), and fails closed otherwise.
"""

import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig


def _dingtalk_adapter():
    """A live native dingtalk adapter (no relay fronting)."""
    adapter = AsyncMock()
    # A native adapter must NOT advertise relay fronting.
    if hasattr(adapter, "fronts_platform"):
        del adapter.fronts_platform
    adapter.supports_inchannel_continuable = False
    return adapter


def _satellite_config(*, dingtalk_enabled=False):
    """A satellite home config: dingtalk present but DISABLED (no credential)."""
    config = MagicMock()
    config.platforms = {
        Platform.DINGTALK: PlatformConfig(enabled=dingtalk_enabled),
    }
    config.get_home_channel = lambda p: None
    return config


def _job():
    return {
        "id": "xcx-job",
        "name": "xcx daily report",
        "deliver": "dingtalk",
        "origin": {"platform": "dingtalk", "chat_id": "cidABC=="},
    }


def _run(adapters, gateway_config, *, routed):
    """Drive ``_deliver_result`` with a live loop; ``routed`` stubs the
    primary-routing predicate (static route OR dynamic binding for this home)."""
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future

    router = MagicMock()

    async def _deliver_to_platform(target, content, metadata):
        return {"success": True, "raw_response": None}

    router._deliver_to_platform = _deliver_to_platform

    with patch("gateway.config.load_gateway_config",
               return_value=gateway_config), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False}}), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("cron.scheduler._delivery_platform_routed_from_primary_gateway",
               return_value=routed), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        return _deliver_result(_job(), "Daily report.",
                               adapters=adapters, loop=loop)


class TestSatelliteSharedAdapterBorrow:
    def test_borrowed_adapter_delivers_when_primary_routes_platform(self):
        """The exact xcx case: own dingtalk disabled, live shared adapter
        present, primary routes dingtalk→this profile → delivers (no error)."""
        result = _run(
            {Platform.DINGTALK: _dingtalk_adapter()},
            _satellite_config(dingtalk_enabled=False),
            routed=True,
        )
        assert result is None  # None == delivered without errors

    def test_no_borrow_when_not_routed_fails_closed(self):
        """A live shared adapter is present but the primary does NOT route this
        platform to this profile → the native enabled gate stands (blocked)."""
        result = _run(
            {Platform.DINGTALK: _dingtalk_adapter()},
            _satellite_config(dingtalk_enabled=False),
            routed=False,
        )
        assert result is not None
        assert "not configured/enabled" in result

    def test_no_borrow_without_live_adapter_fails_closed(self):
        """Routed but NO live shared adapter to borrow → still blocked (there is
        nothing to deliver through; e.g. manual `hermes cron run`)."""
        result = _run(
            {},
            _satellite_config(dingtalk_enabled=False),
            routed=True,
        )
        assert result is not None
        assert "not configured/enabled" in result

    def test_own_enabled_config_uses_native_path_not_borrow(self):
        """Forward-compat: a tenant that DOES enable its own dingtalk resolves
        natively (resolve_delivery_transport wins); the borrow hatch is not even
        consulted. Delivers regardless of the routing predicate."""
        result = _run(
            {Platform.DINGTALK: _dingtalk_adapter()},
            _satellite_config(dingtalk_enabled=True),
            routed=False,  # would block IF the borrow path were taken
        )
        assert result is None


class TestBorrowSurvivesRealDeliveryRouter:
    """Regression: the live-send path rebuilds ``DeliveryRouter(config,
    adapters)`` and re-runs ``resolve_delivery_transport`` against the SAME
    ``config``. If the borrow only patched a local ``pconfig`` (not
    ``config.platforms``), the router raised "No adapter configured for
    dingtalk" and fell through to the credential-less standalone path (observed
    in production: "live adapter delivery ... failed: No adapter configured for
    dingtalk, falling back to standalone"). This drives the REAL DeliveryRouter
    (not a stub) so the router-resolution leg is actually exercised."""

    def _run_with_real_router(self, adapters, gateway_config, *, routed):
        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            try:
                future.set_result(asyncio.run(coro))
            except BaseException as e:  # noqa: BLE001
                future.set_exception(e)
            return future

        with patch("gateway.config.load_gateway_config",
                   return_value=gateway_config), \
             patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False}}), \
             patch("cron.scheduler._delivery_platform_routed_from_primary_gateway",
                   return_value=routed), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            return _deliver_result(_job(), "Daily report.",
                                   adapters=adapters, loop=loop)

    def test_router_resolves_borrowed_adapter_no_standalone_fallthrough(self):
        """With the real router, the borrowed adapter must resolve and
        ``adapter.send`` must actually be called — no "No adapter configured"
        error, no standalone fall-through."""
        adapter = _dingtalk_adapter()
        # A SendResult-like confirmation so _confirm_adapter_delivery passes.
        adapter.send.return_value = {"success": True, "raw_response": None}
        result = self._run_with_real_router(
            {Platform.DINGTALK: adapter},
            _satellite_config(dingtalk_enabled=False),
            routed=True,
        )
        assert result is None
        assert adapter.send.await_count >= 1, "borrowed adapter.send was never called"

