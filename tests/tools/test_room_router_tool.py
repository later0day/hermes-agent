"""Tests for tools/room_router_tool.py — M1.3.

Covers the route_to_member function's behavior AND the crucial Loop
Termination side effect (§9.2 patch A). Verifies:

  - Return format is JSON with the ROUTE_ACTION sentinel + resolved
    fields
  - request_hard_interrupt gets called on the active agent (this is
    the whole point of the milestone — without this, observer's LLM
    happily wraps up with a text turn after the tool result and
    doubles the per-message cost)
  - Failure to interrupt does NOT lose the routing decision itself
  - Member/reason whitespace normalization
  - M1-B4: empty member string is passed through (routing side handles
    default_member fallback)
  - JSON schema is well-formed and required fields match the function signature
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.room_router_tool import (
    ROUTE_ACTION,
    ROUTE_TO_MEMBER_SCHEMA,
    route_to_member,
)


# ---------------------------------------------------------------------------
# Return format
# ---------------------------------------------------------------------------


def test_returns_json_string_with_route_action():
    """Tool return type is a JSON-encoded string, not a dict — agent tool
    machinery treats tool returns as text."""
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = route_to_member(member="alice", reason="best fit")

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["action"] == ROUTE_ACTION
    assert parsed["member"] == "alice"
    assert parsed["reason"] == "best fit"
    assert parsed["is_new_topic"] is False


def test_result_includes_is_new_topic_flag():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = route_to_member(member="alice", reason="new inquiry", is_new_topic=True)

    parsed = json.loads(result)
    assert parsed["is_new_topic"] is True


def test_json_output_is_utf8_safe_for_chinese():
    """SOUL.md's §8 Rule A instructs the observer to write reasons in
    Chinese (上一位处理人 ...). ensure_ascii=False must be honored so the
    router can pattern-match on the raw Chinese prefix, not escaped."""
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = route_to_member(
            member="finance",
            reason="上一位处理人 client_svc 的回复摘要: 用户询问了退款流程",
        )

    parsed = json.loads(result)
    assert "上一位处理人" in parsed["reason"]
    # Also confirm the raw string is not \u-escaped
    assert "\\u" not in result


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def test_strips_whitespace_from_member_and_reason():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = route_to_member(member="  alice  ", reason="  fit  ")

    parsed = json.loads(result)
    assert parsed["member"] == "alice"
    assert parsed["reason"] == "fit"


def test_m1_b4_empty_member_passes_through_as_empty_string():
    """M1-B4: observer emits member="" (or whitespace) — the tool does NOT
    itself route to a fallback. It hands the empty string back to the
    router, which owns the default_member fallback logic (see
    AgentRoom.resolve_default_member). This division of responsibility
    keeps the fallback rule in one place (the store) instead of
    duplicating it in the tool."""
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = route_to_member(member="   ", reason="ambiguous")

    parsed = json.loads(result)
    assert parsed["member"] == ""


def test_coerces_non_string_arguments():
    """LLM sometimes emits arguments with non-string types even when
    schema says string. Coerce rather than crash."""
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        # None → treated as empty
        result = route_to_member(member=None, reason=None)  # type: ignore[arg-type]
    parsed = json.loads(result)
    assert parsed["member"] == ""
    assert parsed["reason"] == ""


# ---------------------------------------------------------------------------
# §9.2 Loop Termination (THE hard patch A this milestone implements)
# ---------------------------------------------------------------------------


def test_calls_request_hard_interrupt_on_active_agent():
    """The whole point of M1.3. When there's an active parent agent
    (bound by AIAgent.run_conversation via bind_subagent_parent — Spike
    4 confirmed this happens on every agent turn, not just delegate
    subagents), the observer's loop MUST be interrupted after this
    tool call to prevent a wrap-up text turn."""
    fake_agent = MagicMock()
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=fake_agent,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        return_value=True,
    ) as m_interrupt:
        route_to_member(member="alice", reason="fit")

    m_interrupt.assert_called_once()
    called_agent = m_interrupt.call_args[0][0]
    assert called_agent is fake_agent
    # The interrupt message should mention the routing decision for
    # debug clarity — but we don't over-constrain the exact wording.
    called_msg = m_interrupt.call_args[0][1] if len(m_interrupt.call_args[0]) > 1 else ""
    assert "alice" in called_msg


def test_returns_decision_even_when_interrupt_fails():
    """If request_hard_interrupt returns False (agent doesn't implement
    the interrupt ABI), the routing decision is STILL delivered. Degraded
    behavior (observer runs one more iteration) is acceptable; losing
    the decision is not."""
    fake_agent = MagicMock()
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=fake_agent,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        return_value=False,
    ):
        result = route_to_member(member="alice", reason="fit")

    parsed = json.loads(result)
    assert parsed["member"] == "alice"
    assert parsed["action"] == ROUTE_ACTION


def test_returns_decision_even_when_interrupt_raises():
    """If the interrupt path raises (import error, ABI mismatch,
    whatever), still return the routing decision."""
    fake_agent = MagicMock()
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=fake_agent,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        side_effect=RuntimeError("simulated ABI failure"),
    ):
        result = route_to_member(member="alice", reason="fit")

    parsed = json.loads(result)
    assert parsed["member"] == "alice"


def test_does_not_call_interrupt_when_no_active_agent():
    """When called outside an agent turn (test harness, direct import,
    etc.), get_active_subagent_parent returns None. Tool should NOT
    attempt to interrupt None."""
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=None,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
    ) as m_interrupt:
        result = route_to_member(member="alice", reason="fit")

    m_interrupt.assert_not_called()
    parsed = json.loads(result)
    assert parsed["member"] == "alice"


# ---------------------------------------------------------------------------
# Schema validity
# ---------------------------------------------------------------------------


def test_schema_top_level_fields():
    assert ROUTE_TO_MEMBER_SCHEMA["name"] == "route_to_member"
    assert "description" in ROUTE_TO_MEMBER_SCHEMA
    assert "parameters" in ROUTE_TO_MEMBER_SCHEMA


def test_schema_required_matches_function_signature():
    """Function signature has member + reason as positional-required,
    is_new_topic as keyword-with-default. Schema should reflect that."""
    params = ROUTE_TO_MEMBER_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"member", "reason"}
    assert "is_new_topic" not in params["required"]

    props = params["properties"]
    assert set(props.keys()) == {"member", "reason", "is_new_topic"}
    assert props["member"]["type"] == "string"
    assert props["reason"]["type"] == "string"
    assert props["is_new_topic"]["type"] == "boolean"


def test_schema_description_documents_soul_md_summary_convention():
    """§8 Rule A: the schema description shown to the LLM must mention
    the '上一位处理人' summary prefix convention, or the observer won't
    produce cross-member summaries from context alone."""
    text = ROUTE_TO_MEMBER_SCHEMA["description"]
    assert "上一位处理人" in text
    assert "reason" in text
