"""A binding-routed satellite keeps riding the primary's bot after it has run a LOCAL session.

Upstream's local sessions put a LocalSessionAdapter into the satellite's own adapter map; keyed on an
empty map, ``_adapters_for_profile`` then returned that map alone, so every send made outside a live turn
(completions, restored-session notices, cron run-now) found no DingTalk adapter (live 2026-10-15)."""
from __future__ import annotations

from gateway.config import Platform
from tests.gateway import test_binding_routed_satellite_adapters as _binding_tests

satellite_runner = _binding_tests.runner  # the shared fixture, registered under this name


def test_satellite_with_a_local_session_adapter_still_borrows_the_primary(satellite_runner):
    r = satellite_runner.runner
    local = object()
    r._profile_adapters["bound"] = {Platform.LOCAL: local}
    adapters = r._adapters_for_profile("bound")
    assert adapters.get(Platform.DINGTALK) is satellite_runner.primary
    assert adapters.get(Platform.LOCAL) is local  # its own local session, never the primary's

    r._profile_adapters["unrouted"] = {Platform.LOCAL: local}
    assert r._adapters_for_profile("unrouted") == {Platform.LOCAL: local}  # no route: still fail closed
