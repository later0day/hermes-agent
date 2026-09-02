"""Multiplex profile_routes must rescue cron preflight from false blocks.

Under ``gateway.multiplex_profiles`` the primary gateway's in-process ticker
fires satellite-profile jobs and delivers them through the PRIMARY gateway's
live adapters (#69377) — the satellite home intentionally holds no platform
credentials of its own (a second token is a ``duplicate_credential`` fatal).
``_preflight_check_delivery`` loads the gateway config of the job's OWN home,
so the routed platform reads as unconnected there and the job was permanently
blocked before any LLM call (#97476). The guard: when the primary home's
``profile_routes`` routes the platform to the profile being served, the
delivery is the primary gateway's to make — pass it through.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from cron.scheduler import (
    _delivery_platform_routed_from_primary_gateway,
    _preflight_check_delivery,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


PRIMARY_YAML = {
    "gateway": {
        "multiplex_profiles": True,
        "profile_routes": [
            {
                "name": "grant-topic",
                "platform": "telegram",
                "chat_id": "-1004306455751",
                "thread_id": "14",
                "profile": "grant",
            }
        ],
    }
}


def _gateway_config(connected_values):
    config = MagicMock()
    config.get_connected_platforms.return_value = [
        MagicMock(value=v) for v in connected_values
    ]
    return config


@pytest.fixture
def multiplex_homes(tmp_path, monkeypatch):
    """A primary root whose config routes telegram→grant, plus the grant home.

    ``get_default_hermes_root`` is patched so the primary config, the profiles
    root, and ``get_profile_dir`` all resolve inside ``tmp_path``; the home
    override reproduces exactly what the multiplex ticker does per profile.
    """
    root = tmp_path / "root"
    grant_home = root / "profiles" / "grant"
    grant_home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(PRIMARY_YAML), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda: root
    )
    token = set_hermes_home_override(str(grant_home))
    yield root, grant_home
    reset_hermes_home_override(token)


class TestRoutedSatellitePreflight:
    def test_routed_platform_passes_preflight(self, multiplex_homes):
        """telegram→grant route: a grant-profile telegram deliver passes."""
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config(set())):
            assert _preflight_check_delivery(
                {"deliver": "telegram:-1004306455751:14"}) is None

    def test_unrouted_platform_still_blocked(self, multiplex_homes):
        """The route rescues telegram only — discord stays blocked."""
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config(set())):
            reason = _preflight_check_delivery({"deliver": "discord:12345"})
            assert reason is not None
            assert "discord" in reason

    def test_route_for_another_profile_does_not_rescue(self, tmp_path, monkeypatch):
        """A route naming a DIFFERENT profile is not this home's lifeline."""
        root = tmp_path / "root"
        other_home = root / "profiles" / "other"
        other_home.mkdir(parents=True)
        (root / "config.yaml").write_text(yaml.safe_dump(PRIMARY_YAML),
                                          encoding="utf-8")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        token = set_hermes_home_override(str(other_home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("telegram") is False
        finally:
            reset_hermes_home_override(token)

    def test_primary_home_itself_skips_route_lookup(self, multiplex_homes):
        """Running as the primary home: no primary/secondary split to consult."""
        root, _ = multiplex_homes
        token = set_hermes_home_override(str(root))
        try:
            assert _delivery_platform_routed_from_primary_gateway("telegram") is False
        finally:
            reset_hermes_home_override(token)

    def test_missing_primary_config_fails_closed(self, tmp_path, monkeypatch):
        """No primary config.yaml readable: the rescue stays off (blocked)."""
        root = tmp_path / "root"
        grant_home = root / "profiles" / "grant"
        grant_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        token = set_hermes_home_override(str(grant_home))
        try:
            with patch("gateway.config.load_gateway_config",
                       return_value=_gateway_config(set())):
                reason = _preflight_check_delivery(
                    {"deliver": "telegram:-1004306455751:14"})
                assert reason is not None
                assert "telegram" in reason
        finally:
            reset_hermes_home_override(token)

    def test_disabled_route_does_not_rescue(self, tmp_path, monkeypatch):
        """``enabled: false`` routes are inert — the block stands."""
        root = tmp_path / "root"
        grant_home = root / "profiles" / "grant"
        grant_home.mkdir(parents=True)
        cfg = yaml.safe_load(yaml.safe_dump(PRIMARY_YAML))
        cfg["gateway"]["profile_routes"][0]["enabled"] = False
        (root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        token = set_hermes_home_override(str(grant_home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("telegram") is False
        finally:
            reset_hermes_home_override(token)


class TestBoundSatellitePreflight:
    """Dynamic source_agent_bindings (runtime ``/agent use``) must ALSO rescue
    cron preflight, not just static ``profile_routes``. A profile can serve many
    bound dingtalk/weixin groups with ZERO static routes (observed: 21 xcx
    dingtalk bindings, 0 routes), so a config-only check false-blocks every one
    of that profile's cron jobs while the primary's live adapter is connected.
    """

    def _seed_binding(self, tmp_path, monkeypatch, *, key, profile,
                      routes=None):
        """A primary root with an empty (or ``routes``) config.yaml plus a
        source_agent_bindings.sqlite holding ``key`` -> ``profile``.  Serves
        the bound profile's home.  Returns (root, profile_home)."""
        from gateway.source_agent_binding import SourceAgentBindingStore

        root = tmp_path / "root"
        profile_home = root / "profiles" / profile
        profile_home.mkdir(parents=True)
        cfg = {"gateway": {"multiplex_profiles": True}}
        if routes is not None:
            cfg["gateway"]["profile_routes"] = routes
        (root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

        db_path = root / "gateway_source_agent_bindings.sqlite"
        store = SourceAgentBindingStore(db_path=db_path)
        store.set_binding(key, profile_name=profile, agent_id="main")
        store.close()

        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        # The binding-store constant is import-time; point it at our db.
        monkeypatch.setattr(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            db_path,
        )
        return root, profile_home

    def test_bound_platform_passes_preflight(self, tmp_path, monkeypatch):
        """dingtalk group bound to xcx (no static route) → xcx cron passes."""
        _, home = self._seed_binding(
            tmp_path, monkeypatch,
            key="source:dingtalk:group:cidABC==:433670", profile="xcx",
        )
        token = set_hermes_home_override(str(home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is True
            with patch("gateway.config.load_gateway_config",
                       return_value=_gateway_config(set())):
                assert _preflight_check_delivery({"deliver": "dingtalk"}) is None
        finally:
            reset_hermes_home_override(token)

    def test_binding_for_other_platform_does_not_rescue(self, tmp_path, monkeypatch):
        """A weixin binding must not rescue a dingtalk deliver."""
        _, home = self._seed_binding(
            tmp_path, monkeypatch,
            key="source:weixin:dm:openid@im.wechat", profile="xcx",
        )
        token = set_hermes_home_override(str(home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is False
        finally:
            reset_hermes_home_override(token)

    def test_binding_for_other_profile_does_not_rescue(self, tmp_path, monkeypatch):
        """A dingtalk binding to profile B must not rescue profile A's home."""
        root, _ = self._seed_binding(
            tmp_path, monkeypatch,
            key="source:dingtalk:group:cidABC==:1", profile="other",
        )
        home_a = root / "profiles" / "xcx"
        home_a.mkdir(parents=True)
        token = set_hermes_home_override(str(home_a))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is False
        finally:
            reset_hermes_home_override(token)

    def test_primary_home_skips_binding_lookup(self, tmp_path, monkeypatch):
        """Running as the primary home: no satellite rescue to consider."""
        root, _ = self._seed_binding(
            tmp_path, monkeypatch,
            key="source:dingtalk:group:cidABC==:1", profile="xcx",
        )
        token = set_hermes_home_override(str(root))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is False
        finally:
            reset_hermes_home_override(token)
