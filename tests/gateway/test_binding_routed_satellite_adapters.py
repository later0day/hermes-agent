"""A credentialless profile reached only through ``/agent use`` bindings borrows the primary's bot.

``_is_shared_bot_satellite`` counted only static ``profile_routes``; a binding-only satellite (the usual
DingTalk setup) resolved ``{}`` for every send made outside a live turn — restored-session notices,
completions, cron run-now — while cron's ticker already lent it the primary for its bound chats.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig


@pytest.fixture
def runner(tmp_path, monkeypatch):
    from agent import secret_scope
    from gateway.run import GatewayRunner
    from gateway.source_agent_binding import SourceAgentBindingStore

    home = tmp_path / "hh"
    for name in ("bound", "unrouted", "own_bot"):
        (home / "profiles" / name).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)

    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(multiplex_profiles=True)
    r.config.platforms = {Platform.DINGTALK: PlatformConfig(enabled=True, extra={})}
    r.config.profile_routes = []  # no static routes at all
    r._primary_profile_name = "default"
    primary, own = object(), object()
    r.adapters = {Platform.DINGTALK: primary}
    r._profile_adapters = {"bound": {}, "unrouted": {}, "own_bot": {Platform.DINGTALK: own}}
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    store.set_binding("source:dingtalk:group:cidGROUP==", "bound")
    store.set_binding("source:dingtalk:group:cidOTHER==", "own_bot")
    r._source_agent_binding_store = store
    served = [("default", home)] + [(n, home / "profiles" / n) for n in ("bound", "unrouted", "own_bot")]
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served):
        yield SimpleNamespace(runner=r, primary=primary, own=own)
    store.close()
    secret_scope.set_multiplex_active(prev)


def test_binding_only_satellite_borrows_primary_and_others_stay_closed(runner):
    r = runner.runner
    assert r._adapters_for_profile("bound").get(Platform.DINGTALK) is runner.primary
    assert r._adapters_for_profile("unrouted") == {}  # no route, no binding: fail closed
    assert r._adapters_for_profile("own_bot").get(Platform.DINGTALK) is runner.own  # own bot wins


def test_kanban_notifier_honours_bindings_with_inbound_precedence(runner):
    """A kanban notify subscription for a bound chat delivers through the primary for the bound profile
    only — the static-route-only gate dropped every such ping silently."""
    from gateway.kanban_watchers_notifier import _adapter_for_subscription

    r = runner.runner
    sub = {"chat_id": "cidGROUP==", "chat_type": "group"}
    assert _adapter_for_subscription(r, Platform.DINGTALK, sub, "bound") is runner.primary
    # The chat is bound to `bound`; a subscription stamped with another credentialless profile stays closed.
    assert _adapter_for_subscription(r, Platform.DINGTALK, sub, "unrouted") is None
