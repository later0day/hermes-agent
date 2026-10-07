"""The AI Card content key comes from the adapter's own profile config.

The adapter used to re-read ``load_config_readonly()`` on every card, which under a multiplexed
gateway is the LAUNCH profile's config: a secondary profile's bot rendered cards into the launch
profile's template variable (blank cards on a custom template).
"""
from pathlib import Path

import hermes_yaml as yaml

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.dingtalk.adapter import DingTalkAdapter
from plugins.platforms.dingtalk.adapter_cards import DEFAULT_AI_CARD_CONTENT_KEY


def _write_config(home: Path, block: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"dingtalk": block}))


def test_card_content_key_follows_the_adapters_profile_not_the_launch_profile(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    _write_config(launch, {"card_template_id": "tpl.launch", "card_content_key": "launchKey"})
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "id")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "secret")

    loaded = load_gateway_config().platforms[Platform.DINGTALK]
    assert DingTalkAdapter(loaded)._current_card_content_key() == "launchKey"

    # Another profile's adapter, built while the launch profile's config is still the ambient one.
    other = DingTalkAdapter(PlatformConfig(enabled=True, extra={"card_template_id": "tpl.other"}))
    assert other._current_card_content_key() == DEFAULT_AI_CARD_CONTENT_KEY
