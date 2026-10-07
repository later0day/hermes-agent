"""Every built-in dashboard theme is selectable from the Config page.

The ``dashboard.theme`` select had its own hand-written list, which had already dropped
``default-large`` and never gained the Pure Ink themes.
"""
from hermes_cli.web_server_config import _SCHEMA_OVERRIDES
from hermes_cli.web_server_dashboard import _BUILTIN_DASHBOARD_THEMES


def test_config_theme_select_offers_every_builtin_theme():
    offered = _SCHEMA_OVERRIDES["dashboard.theme"]["options"]
    assert offered == [theme["name"] for theme in _BUILTIN_DASHBOARD_THEMES]
    assert {"pure-ink-light", "pure-ink-dark"} <= set(offered)
