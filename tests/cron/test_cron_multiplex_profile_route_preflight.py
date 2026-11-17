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
import hermes_yaml as yaml

from cron.scheduler_preflight import (
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
    """The runtime binding store ALSO rescues preflight — a profile can serve
    dozens of bound DingTalk/etc. chats via ``/agent use`` without any static
    route, and cron must not false-block those jobs. See fix db16a085b0."""

    def _seed_primary_binding(self, monkeypatch, root: Path, platform: str,
                              profile: str, chat_id: str = "chat-abc") -> None:
        """Seed the binding store at the REAL default filename
        (``gateway_source_agent_bindings.sqlite``) and patch the module
        constant so the code under test — which reads
        ``source_agent_bindings_db_path()`` directly — sees this isolated
        store rather than the real one."""
        from gateway.source_agent_binding import SourceAgentBindingStore

        db_path = root / "gateway_source_agent_bindings.sqlite"
        monkeypatch.setattr("gateway.source_agent_binding.source_agent_bindings_db_path", lambda: db_path)
        store = SourceAgentBindingStore(db_path=db_path)
        try:
            store.set_binding(
                f"source:{platform}:group:{chat_id}", profile,
                fallback_target={"platform": platform, "chat_id": chat_id,
                                 "chat_type": "group"},
            )
        finally:
            store.close()

    def test_bound_platform_passes_preflight(self, tmp_path, monkeypatch):
        """xcx has 21 dingtalk bindings, 0 static routes → dingtalk cron passes."""
        root = tmp_path / "root"
        xcx_home = root / "profiles" / "xcx"
        xcx_home.mkdir(parents=True)
        # No config.yaml at all — pure binding-based routing.
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        self._seed_primary_binding(monkeypatch, root, "dingtalk", "xcx")
        token = set_hermes_home_override(str(xcx_home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is True
        finally:
            reset_hermes_home_override(token)

    def test_wrong_platform_binding_does_not_rescue(self, tmp_path, monkeypatch):
        """A dingtalk binding does not un-block a telegram cron job."""
        root = tmp_path / "root"
        xcx_home = root / "profiles" / "xcx"
        xcx_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        self._seed_primary_binding(monkeypatch, root, "dingtalk", "xcx")
        token = set_hermes_home_override(str(xcx_home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("telegram") is False
        finally:
            reset_hermes_home_override(token)

    def test_wrong_profile_binding_does_not_rescue(self, tmp_path, monkeypatch):
        """A binding to some OTHER profile is not this home's lifeline."""
        root = tmp_path / "root"
        xcx_home = root / "profiles" / "xcx"
        xcx_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        self._seed_primary_binding(monkeypatch, root, "dingtalk", "different-profile")
        token = set_hermes_home_override(str(xcx_home))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is False
        finally:
            reset_hermes_home_override(token)

    def test_primary_home_skips_binding_lookup(self, tmp_path, monkeypatch):
        """The primary running as itself doesn't consult its own bindings."""
        root = tmp_path / "root"
        root.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        # Seed a binding that WOULD match — but we're the primary, so ignored.
        self._seed_primary_binding(monkeypatch, root, "dingtalk", "default")
        token = set_hermes_home_override(str(root))
        try:
            assert _delivery_platform_routed_from_primary_gateway("dingtalk") is False
        finally:
            reset_hermes_home_override(token)

    def test_binding_rescue_composes_with_deliver_check(self, tmp_path, monkeypatch):
        """The full preflight path: a bound dingtalk profile whose config.yaml
        reports dingtalk unconnected still passes because the primary's live
        adapter delivers it."""
        root = tmp_path / "root"
        xcx_home = root / "profiles" / "xcx"
        xcx_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: root
        )
        self._seed_primary_binding(monkeypatch, root, "dingtalk", "xcx", chat_id="g1")
        token = set_hermes_home_override(str(xcx_home))
        try:
            with patch("gateway.config.load_gateway_config",
                       return_value=_gateway_config(set())):
                assert _preflight_check_delivery(
                    {"deliver": "dingtalk:g1"}) is None
        finally:
            reset_hermes_home_override(token)
