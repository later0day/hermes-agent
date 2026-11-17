"""With a turn status card, streaming must not open a second card for the same answer.

The card is itself a delta sink (live preview). The fork skipped the stream consumer while the card
was active (0fd90da47b); the port always built one, so AI Cards + streaming showed the answer twice.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import StreamingConfig
from gateway.run_turn_runner import TurnRunner


class _Consumer:
    def __init__(self, **kwargs):
        self.deltas = []

    def on_delta(self, text):
        self.deltas.append(text)


class _Card:
    def __init__(self):
        self.deltas = []

    def on_delta(self, text):
        self.deltas.append(text)


def _runner(card):
    runner = object.__new__(TurnRunner)
    adapter = SimpleNamespace(SUPPORTS_MESSAGE_EDITING=True)
    runner._runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig(enabled=True)),
        _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *a, **k: (SimpleNamespace(), None),
    )
    runner._ctx = SimpleNamespace(
        mute_notification_reply=False, streaming_tts_consumer_holder=[None], scheduled_heartbeat=False,
        resolve_display_setting=lambda *a: None, user_config={}, interim_assistant_messages_enabled=False,
        source=SimpleNamespace(chat_id="c1"), _status_thread_metadata={}, progress_queue=None,
        event_message_id="m1", _run_still_current=lambda: True, stream_consumer_holder=[None],
        turn_status_card_holder=[card],
    )
    return runner


@pytest.mark.parametrize("with_card", [True, False])
def test_stream_consumer_only_without_a_status_card(monkeypatch, with_card):
    monkeypatch.setattr("gateway.stream_consumer.GatewayStreamConsumer", _Consumer)
    card = _Card() if with_card else None
    runner = _runner(card)

    stream_consumer, stream_delta_cb, *_ = runner._setup_stream_consumer("dingtalk")
    stream_delta_cb("hello")

    if with_card:
        assert stream_consumer is None and card.deltas == ["hello"]
    else:
        assert stream_consumer.deltas == ["hello"]
