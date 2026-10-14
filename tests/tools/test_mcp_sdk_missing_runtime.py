"""Configured MCP servers in a runtime without the ``mcp`` SDK: say so once at WARNING and stop claiming a
connect. Live 2026-10-15 the services ran from an environment synced without the extra; every profile's
servers were dead for a day while the reconcile logged "added" each minute and the cause sat at DEBUG."""

import logging


def test_reconcile_without_sdk_reports_nothing_added_and_warns_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool
    from tools import mcp_tool_config as _config
    from tools import mcp_tool_discovery as disc

    monkeypatch.setattr(_config, "_load_mcp_config", lambda: {"isoecho": {"command": "python3", "args": ["s.py"]}})
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", False)
    monkeypatch.setattr(disc, "_sdk_missing_warned", False, raising=False)

    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool_discovery"):
        first = disc.reconcile_mcp_servers_with_config()
        second = disc.reconcile_mcp_servers_with_config()
        assert disc.discover_mcp_tools() == []

    assert first["added"] == [] and second["added"] == []
    warnings = [r.getMessage() for r in caplog.records if "Python SDK is not installed" in r.getMessage()]
    assert len(warnings) == 1 and "isoecho" in warnings[0]
