"""CLI multimodal projections tolerate arbitrary JSON part types."""

from __future__ import annotations

import pytest

from hermes_cli.cli_agent_setup_mixin import _user_display_text
from hermes_cli.cli_loops_mixin import CLILoopsMixin


@pytest.mark.parametrize("part_type", [{"slug": "full-time"}, ["string", "null"]])
def test_cli_text_projections_tolerate_non_string_part_types(part_type) -> None:
    content = [{"type": part_type, "text": "not a provider text block"}]

    assert _user_display_text(content) == ""

    cli = CLILoopsMixin.__new__(CLILoopsMixin)
    cli.conversation_history = [{"role": "assistant", "content": content}]
    assert cli._last_assistant_response_text() == ""
