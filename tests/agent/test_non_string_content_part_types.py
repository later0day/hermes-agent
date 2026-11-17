"""Arbitrary JSON part types must not crash multimodal message projections."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.codex_runtime import make_codex_app_server_event_bridge
from agent.context_compressor import ContextCompressor, _is_text_only_content
from agent.image_replay import replay_recent_images
from agent.message_sanitization import _messages_carry_images, _strip_images_from_messages
from agent.model_metadata import _count_image_tokens, _wire_message_shadow
from agent.session_persistence import _durable_content
from agent.tool_dispatch_helpers import _trajectory_normalize_msg
from agent.trace_upload import _content_to_blocks
from agent.transports.codex_app_server_session import _build_turn_input
from agent.vision_message_prep import _is_image_part, _salvage_text_parts


@pytest.mark.parametrize("part_type", [{"slug": "full-time"}, ["string", "null"]])
def test_arbitrary_json_part_type_survives_message_projections(part_type) -> None:
    part = {"type": part_type, "nested": {"value": "preserved"}}
    content = [part]

    assert _is_text_only_content(content) is False
    assert ContextCompressor._is_blank_user_turn({"role": "user", "content": content}) is False
    assert replay_recent_images([{"role": "user", "content": content}], keep=1) == 0

    messages = [{"role": "tool", "tool_call_id": "call-1", "content": content}]
    assert _strip_images_from_messages(messages) is False
    assert messages[0]["content"] == content
    assert _messages_carry_images(messages) is False
    assert _trajectory_normalize_msg(messages[0])["content"] == content
    assert _count_image_tokens(messages[0], 100) == 0
    assert _wire_message_shadow(messages[0])["content"] == content
    assert _durable_content(content) is None
    assert _content_to_blocks(content, False) == [{"type": "text", "text": json.dumps(part)}]
    assert _build_turn_input(content) == ([{"type": "text", "text": ""}], "")
    assert _is_image_part(part) is False
    assert _salvage_text_parts(content, any_dict_text=False) == []

    touched = []
    agent = SimpleNamespace(_touch_activity=touched.append)
    bridge = make_codex_app_server_event_bridge(agent)
    bridge({"method": "item/started", "params": {"item": part}})
    assert touched == []
