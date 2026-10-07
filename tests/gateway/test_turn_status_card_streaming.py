"""The turn status card follows the gateway's streaming switch.

Display settings used a default ``StreamingConfig()`` (always off) instead of the runner's
``streaming`` config, so turning ``streaming.enabled`` on never reached the card on platforms
whose display tier follows the global switch; ``display.platforms.<p>.streaming`` narrows it,
as everywhere else.
"""
import importlib
from types import SimpleNamespace

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.parametrize("platform_override, expected", [(None, True), (False, False)])
def test_card_streaming_follows_the_gateway_switch(monkeypatch, tmp_path, platform_override, expected):
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    if platform_override is not None:
        (tmp_path / "config.yaml").write_text(
            f"display:\n  platforms:\n    telegram:\n      streaming: {str(platform_override).lower()}\n")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(
        streaming=StreamingConfig(enabled=True),
        thread_sessions_per_user=False, group_sessions_per_user=False, stt_enabled=False,
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm")

    assert runner._run_agent_display_settings(source)._streaming_enabled is expected
