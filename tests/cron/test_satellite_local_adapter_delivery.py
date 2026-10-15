"""A credentialless satellite keeps borrowing the primary's bot for routed chats after it runs a LOCAL
session. Live 2026-10-15 (upstream merge): every cron run became a local session, which put a
LocalSessionAdapter in the satellite's own adapter map; the drain keyed "credentialless" on an EMPTY map,
stopped lending the default DingTalk bot, and xcx's pushes failed "platform 'dingtalk' not
configured/enabled"."""
from types import SimpleNamespace

from gateway.config import Platform
from gateway.profile_routing import ProfileRoute

DING = Platform("dingtalk")
ROUTES = [ProfileRoute(name="binding:c1", platform="dingtalk", profile="sat", chat_id="c1")]


def _patch_routes(monkeypatch):
    from cron import scheduler_preflight as sp
    monkeypatch.setattr(sp, "_satellite_routes_for_current_home", lambda profile=None: list(ROUTES))
    return sp


def test_satellite_with_only_a_local_adapter_borrows_the_primary_for_routed_chats(monkeypatch):
    sp = _patch_routes(monkeypatch)
    local, ding = object(), object()
    view = sp.satellite_delivery_adapters("sat", {Platform.LOCAL: local}, {DING: ding})
    assert view.get(DING, {"chat_id": "c1"}) is ding
    assert view.get(DING, {"chat_id": "other"}) is None  # unrouted chat: fail closed, never the default bot
    assert view.get(Platform.LOCAL, {"chat_id": "x"}) is local
    assert view.get(Platform.LOCAL) is local


def test_a_profile_with_its_own_messaging_bot_keeps_it(monkeypatch):
    sp = _patch_routes(monkeypatch)
    own = {Platform.LOCAL: object(), Platform.TELEGRAM: object()}
    assert sp.satellite_delivery_adapters("sat", own, {DING: object()}) is own


def test_gateway_drain_lends_the_primary_to_a_satellite_holding_a_local_adapter(monkeypatch, tmp_path):
    _patch_routes(monkeypatch)
    from gateway import run
    from cron import scheduler as cron_scheduler

    drained = []
    monkeypatch.setattr(cron_scheduler, "drain_delivery_queue", lambda adapters, loop: drained.append(adapters))
    monkeypatch.setattr(run, "_handoff_watch_scopes", lambda runner: [("sat", tmp_path)])
    ding = object()
    runner = SimpleNamespace(_profile_adapters={"sat": {Platform.LOCAL: object()}})
    run._drain_restart_safe_cron_deliveries({DING: ding}, loop=None, runner=runner)
    (adapters,) = drained
    assert adapters.get(DING, {"chat_id": "c1"}) is ding
