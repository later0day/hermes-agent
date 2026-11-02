"""Fork: a chat bound with ``/agent use`` counts as a default-bot route for the per-destination borrow.

xcx has no bot of its own and borrows the primary DingTalk bot for the chats bound to it with
``/agent use`` (not static ``profile_routes``). Upstream's destination gate (``_default_bot_routes_to``)
only read ``profile_routes``, so after a restart a restored source of a bound chat (heartbeat, async
completion, auto-resume) got no transport. The binding outranks routes at ingress; the gate agrees.
"""

from unittest.mock import patch

from gateway.config import Platform
from gateway.profile_routing import parse_profile_routes
from tests.gateway.test_satellite_per_platform_borrow import _restored, rig  # noqa: F401  (fixture)


def _bind(runner, mapping):
    runner._binding_profile_for_source = lambda source: mapping.get(source.chat_id)
    store = type("Store", (), {"list_bindings": lambda self, profile_name=None: [
        chat for chat, profile in mapping.items() if profile == profile_name]})()
    runner._source_agent_binding_store = store


def test_a_bound_chat_borrows_the_primary_like_a_routed_one(rig):  # noqa: F811
    r = rig.runner
    r.config.profile_routes = parse_profile_routes([])  # no static route at all
    _bind(r, {})
    assert r._delivery_adapter_for(_restored("ops", chat_id="555")) is None
    _bind(r, {"555": "ops"})
    assert r._is_shared_bot_satellite("ops", Platform.DISCORD) is True
    assert r._delivery_adapter_for(_restored("ops", chat_id="555")) is rig.primary_discord
    assert r._resolve_injection_adapter("discord", _restored("ops", chat_id="555")) is rig.primary_discord


def test_a_binding_never_lends_the_primary_for_another_profile_or_an_unbound_chat(rig):  # noqa: F811
    r = rig.runner
    r.config.profile_routes = parse_profile_routes([])
    _bind(r, {"555": "team_b"})
    assert r._delivery_adapter_for(_restored("ops", chat_id="555")) is None
    assert r._delivery_adapter_for(_restored("ops", chat_id="777")) is None
    # Own bot on the platform stays the boundary even when the chat is bound.
    _bind(r, {"555": "ops"})
    assert r._resolve_injection_adapter("telegram", _restored("ops", Platform.TELEGRAM, "555")) is rig.ops_tg


def test_a_binding_lookup_failure_fails_closed(rig):  # noqa: F811
    r = rig.runner
    r.config.profile_routes = parse_profile_routes([])

    def boom(source):
        raise RuntimeError("binding store unavailable")

    r._binding_profile_for_source = boom
    with patch("gateway.run_source_binding.logger"):
        assert r._delivery_adapter_for(_restored("ops", chat_id="555")) is None
