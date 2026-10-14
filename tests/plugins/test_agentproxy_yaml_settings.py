"""``terminal.ap_*`` keys in a profile's config.yaml configure the AgentProxy backend.

The fork mapped them through TERMINAL_CONFIG_ENV_MAP; after the backend moved into a plugin nothing
bridged them, so editing ``terminal.ap_container`` in the reverse profile silently had no effect.
"""

import yaml

from hermes_constants import get_hermes_home


def _write_terminal(cfg: dict) -> None:
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"terminal": cfg}), encoding="utf-8")


def test_yaml_ap_keys_drive_the_backend_settings(monkeypatch):
    from plugins.terminal.agentproxy.provider import agentproxy_settings

    monkeypatch.delenv("TERMINAL_AP_CONTAINER", raising=False)
    _write_terminal({"backend": "agentproxy", "ap_container": "hermes-from-yaml", "ap_agent": "lab"})
    settings = agentproxy_settings()
    assert settings["container"] == "hermes-from-yaml"
    assert settings["agent_id"] == "lab"
    assert settings["path_prefix"] == "/usr/local/bin"  # unset keys keep their defaults


def test_env_still_applies_when_yaml_omits_the_key(monkeypatch):
    from plugins.terminal.agentproxy.provider import agentproxy_settings

    monkeypatch.setenv("TERMINAL_AP_IMAGE", "custom/image:1")
    _write_terminal({"backend": "agentproxy"})
    assert agentproxy_settings()["image"] == "custom/image:1"
