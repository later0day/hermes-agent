"""The turn status card follows the gateway's streaming switch.

Display settings used a default ``StreamingConfig()`` (always off) instead of the runner's
``streaming`` config, so turning ``streaming.enabled`` on never reached the card on platforms
whose display tier follows the global switch; ``display.platforms.<p>.streaming`` narrows it,
as everywhere else.
"""
import dataclasses
import importlib
from types import SimpleNamespace

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_status_card import maybe_start_turn_status_card


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
    runner._delivery_adapter_for = lambda source: SimpleNamespace(SUPPORTS_TURN_STATUS_CARD=True)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm")
    # Streaming is the only progress surface left on, so it alone decides whether the card starts.
    disp = dataclasses.replace(runner._run_agent_display_settings(source), tool_progress_enabled=False,
                               _thinking_enabled=False, interim_assistant_messages_enabled=False)
    turn_ctx = SimpleNamespace(turn_status_card_holder=[None], _progress_metadata={})

    maybe_start_turn_status_card(runner, turn_ctx, disp, source)

    assert (turn_ctx.turn_status_card_holder[0] is not None) is expected
