"""A credentialless satellite's cron output rides the primary bot only for chats it is routed to.

``/agent use`` bindings authorize exactly the bound chat, like a static ``profile_routes`` entry —
never the whole platform — and both kinds of route coexist in one ``SharedRouteAdapters`` map.
Driven through the real binding store, route loaders and delivery-target resolver.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import Platform, load_gateway_config

PRIMARY = {Platform.DINGTALK: "DT_PRIMARY", Platform.TELEGRAM: "TG_PRIMARY"}


@pytest.fixture
def satellite(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text(
        "profile_routes:\n  - platform: telegram\n    chat_id: 'X'\n    profile: sat\n")
    sat = root / "profiles" / "sat"
    sat.mkdir(parents=True)
    (sat / "config.yaml").write_text("model: x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from gateway.source_agent_binding import SourceAgentBindingStore
    store = SourceAgentBindingStore(db_path=root / "gateway_source_agent_bindings.sqlite")
    store.set_binding("source:dingtalk:group:A", "sat")
    store.close()
    monkeypatch.setattr("gateway.source_agent_binding.source_agent_bindings_db_path", lambda: root / "gateway_source_agent_bindings.sqlite")

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(sat))
    yield
    reset_hermes_home_override(token)


def _deliver_via(platform: Platform, chat_id: str):
    from cron.scheduler_delivery import _resolve_target_transport
    from cron.scheduler_preflight import SharedRouteAdapters, _satellite_routes_for_current_home
    # The same map the multiplex ticker hands a credentialless satellite.
    adapters = SharedRouteAdapters(PRIMARY, _satellite_routes_for_current_home())
    resolved, err = _resolve_target_transport(
        {"id": "job"}, platform, platform.value, {"platform": platform.value, "chat_id": chat_id},
        adapters, load_gateway_config())
    return resolved[2] if resolved else None


def test_a_binding_lends_the_primary_bot_for_the_bound_chat_only(satellite):
    assert _deliver_via(Platform.DINGTALK, "A") == "DT_PRIMARY"
    assert _deliver_via(Platform.DINGTALK, "Z-not-bound") is None


def test_static_routes_keep_working_next_to_bindings(satellite):
    assert _deliver_via(Platform.TELEGRAM, "X") == "TG_PRIMARY"
    assert _deliver_via(Platform.TELEGRAM, "Y") is None
