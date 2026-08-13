"""Tests for gateway/agent_room_projection.py — M3.3."""

from __future__ import annotations

import time

import pytest

from gateway.agent_room_messages_store import RoomMessage
from gateway.agent_room_projection import (
    ProjectedMessage,
    project_for_member,
    project_for_observer,
    _MAX_CONTENT_CHARS,
)


def _mk(seq, kind, name, content, tool_calls=None, tool_call_id=None):
    return RoomMessage(
        room_id="r1", sequence=seq, sender_kind=kind, sender_name=name,
        content=content, tool_calls=tool_calls, tool_call_id=tool_call_id,
        timestamp=float(seq),
    )


# ---------------------------------------------------------------------------
# project_for_member — core cases
# ---------------------------------------------------------------------------

def test_empty_input_yields_empty_output():
    assert project_for_member([], "any") == []


def test_user_message_becomes_prefixed_user_role():
    msgs = [_mk(1, "user", "alice", "hello")]
    out = project_for_member(msgs, "client_svc")
    assert len(out) == 1
    assert out[0].role == "user"
    assert "[user]" in out[0].content
    assert "hello" in out[0].content


def test_own_message_becomes_assistant_without_prefix():
    msgs = [_mk(1, "member", "client_svc", "how can I help?")]
    out = project_for_member(msgs, "client_svc")
    assert out[0].role == "assistant"
    assert out[0].content == "how can I help?"
    assert "[" not in out[0].content


def test_other_member_becomes_prefixed_user_role():
    msgs = [_mk(1, "member", "finance", "the bill is $50")]
    out = project_for_member(msgs, "client_svc")
    assert out[0].role == "user"
    assert "[finance]" in out[0].content
    assert "the bill is $50" in out[0].content


def test_observer_becomes_prefixed_user_role():
    msgs = [_mk(1, "observer", "room_x_obs", "chose client_svc")]
    out = project_for_member(msgs, "client_svc")
    assert out[0].role == "user"
    assert "[observer]" in out[0].content


def test_tool_result_folded_into_user():
    msgs = [_mk(1, "tool_result", "route_to_member", '{"member":"x"}',
                tool_call_id="c1")]
    out = project_for_member(msgs, "any")
    assert out[0].role == "user"
    assert "[tool: route_to_member]" in out[0].content


def test_multiparty_ordering_preserved():
    """Interleaved messages from user/member A/member B/observer keep order."""
    msgs = [
        _mk(1, "user", "alice", "help"),
        _mk(2, "observer", "obs", "routing to A"),
        _mk(3, "member", "A", "hello alice"),
        _mk(4, "user", "alice", "and I need billing"),
        _mk(5, "observer", "obs", "routing to B"),
        _mk(6, "member", "B", "your bill is $50"),
        _mk(7, "user", "alice", "thanks"),
    ]

    out_A = project_for_member(msgs, "A")
    # Order preserved
    assert len(out_A) == 7
    # A's own past turn → assistant
    assert out_A[2].role == "assistant"
    assert out_A[2].content == "hello alice"
    # B's turn → user with [B] prefix
    assert out_A[5].role == "user"
    assert "[B]" in out_A[5].content


def test_content_truncation_over_limit():
    huge = "x" * (_MAX_CONTENT_CHARS + 500)
    msgs = [_mk(1, "member", "other", huge)]
    out = project_for_member(msgs, "me")
    assert len(out[0].content) < len(huge)
    assert "[truncated]" in out[0].content


def test_content_no_truncation_below_limit():
    normal = "x" * 100
    msgs = [_mk(1, "member", "other", normal)]
    out = project_for_member(msgs, "me")
    assert "[truncated]" not in out[0].content
    assert normal in out[0].content


def test_observer_route_to_member_toolcall_surfaced():
    tc = [{"id": "1", "function": {"name": "route_to_member",
                                     "arguments": '{"member":"finance"}'}}]
    msgs = [_mk(1, "observer", "obs", "", tool_calls=tc)]
    out = project_for_member(msgs, "finance")
    assert out[0].role == "user"
    assert "routed to member" in out[0].content
    assert "finance" in out[0].content


def test_observer_decompose_toolcall_surfaced():
    """M4-B8: the observer's decompose_and_route decision must render as
    meaningful text (not a blank [observer] row) so the observer's own
    session history shows what it decomposed. route and decompose calls
    interleave in the SAME observer session."""
    tc = [{"id": "1", "function": {"name": "decompose_and_route",
                                    "arguments": '{"tasks":[{"title":"draft","assignee":"legal"}]}'}}]
    msgs = [_mk(1, "observer", "obs", "", tool_calls=tc)]
    out = project_for_member(msgs, "legal")
    assert out[0].role == "user"
    assert "decomposed into subtasks" in out[0].content
    assert "draft" in out[0].content


def test_observer_route_and_decompose_interleave_in_observer_view():
    """M4-B8: an observer session that alternated between route_to_member
    and decompose_and_route must project BOTH as non-empty assistant rows,
    so neither mode's history is lost when the other runs later."""
    route_tc = [{"id": "1", "function": {"name": "route_to_member",
                                         "arguments": '{"member":"finance"}'}}]
    decomp_tc = [{"id": "2", "function": {"name": "decompose_and_route",
                                          "arguments": '{"tasks":[{"title":"step1","assignee":"legal"}]}'}}]
    msgs = [
        _mk(1, "user", "alice", "simple question"),
        _mk(2, "observer", "obs", "", tool_calls=route_tc),
        _mk(3, "user", "alice", "complex multi-step request"),
        _mk(4, "observer", "obs", "", tool_calls=decomp_tc),
    ]
    out = project_for_observer(msgs)
    assert out[1].role == "assistant"
    assert "routed to member" in out[1].content
    assert "finance" in out[1].content
    assert out[3].role == "assistant"
    assert "decomposed into subtasks" in out[3].content
    assert "step1" in out[3].content


def test_empty_content_handled():
    msgs = [_mk(1, "user", "alice", "")]
    out = project_for_member(msgs, "me")
    assert out[0].role == "user"
    assert "[user]" in out[0].content


def test_to_openai_format():
    m = ProjectedMessage(role="user", content="hi")
    assert m.to_openai() == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# project_for_observer
# ---------------------------------------------------------------------------

def test_observer_view_own_turns_are_assistant():
    msgs = [
        _mk(1, "user", "alice", "help"),
        _mk(2, "observer", "obs", "routed"),
        _mk(3, "member", "A", "hi"),
    ]
    out = project_for_observer(msgs)
    assert out[0].role == "user"
    assert out[1].role == "assistant"  # observer's own row
    assert out[2].role == "user"


def test_observer_view_empty_input():
    assert project_for_observer([]) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic_output():
    msgs = [
        _mk(1, "user", "a", "x"),
        _mk(2, "member", "m1", "y"),
        _mk(3, "member", "m2", "z"),
    ]
    a = project_for_member(msgs, "m1")
    b = project_for_member(msgs, "m1")
    assert [x.role for x in a] == [x.role for x in b]
    assert [x.content for x in a] == [x.content for x in b]


# ---------------------------------------------------------------------------
# Performance guard (M3 spike 2 result: <100ms for 500 msgs)
# ---------------------------------------------------------------------------

def test_projection_performance_500_messages():
    msgs = [
        _mk(i, "member" if i % 2 else "user",
            "m1" if i % 3 == 0 else "m2" if i % 3 == 1 else "alice",
            "x" * 100)
        for i in range(1, 501)
    ]
    t0 = time.monotonic()
    out = project_for_member(msgs, "m1")
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert len(out) == 500
    assert elapsed_ms < 100, f"projection took {elapsed_ms:.1f}ms, should be <100ms"
