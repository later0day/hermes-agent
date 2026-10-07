"""DingTalk reaction/stage labels follow display.language through the plugin's own language pack.

They were hardcoded Chinese. The plugin loads through real discovery plus the gateway's registry
lookup, so the pack is proven to register wherever the adapter does.
"""
import pytest

from agent import i18n, i18n_layers
from gateway.config import PlatformConfig
from hermes_cli.plugins import PluginManager


@pytest.fixture
def loaded_plugins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    i18n_layers._reset_registry_for_tests()
    i18n.reset_language_cache()
    PluginManager().discover_and_load()
    # The gateway's adapter lookup is what runs the deferred bundled-platform load.
    from gateway.platform_registry import platform_registry
    assert platform_registry.get("dingtalk") is not None
    yield
    i18n_layers._reset_registry_for_tests()
    i18n.reset_language_cache()


@pytest.mark.parametrize("lang, done, git_stage", [("en", "✅ Done", "🌳 Using git"), ("zh", "✅ 搞定了", "🌳 提交代码中")])
def test_labels_follow_display_language(loaded_plugins, monkeypatch, lang, done, git_stage):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()
    adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
    assert adapter.REACTION_DONE == done
    assert DingTalkAdapter._stage_label_for_tool("terminal", "git status") == git_stage
    assert DingTalkAdapter._stage_label_for_tool("read_file").startswith("👀")


def test_a_language_without_a_pack_falls_back_to_english(loaded_plugins, monkeypatch):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    assert DingTalkAdapter(PlatformConfig(enabled=True, extra={})).REACTION_THINKING == "🤔 Thinking"
