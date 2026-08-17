"""Tests for gateway/agent_room_handoff.py — handoff depth policy."""

from __future__ import annotations

from gateway.agent_room_handoff import (
    DEFAULT_HANDOFF_DEPTH,
    next_mention_depth,
    recommended_handoff_depth,
    resolve_handoff_policy,
    should_route_handoff,
)


def test_default_policy_allows_up_to_max_depth():
    p = resolve_handoff_policy()
    assert p.enabled and p.max_depth == DEFAULT_HANDOFF_DEPTH
    assert [should_route_handoff(d, p) for d in range(6)] == [True, True, True, True, False, False]


def test_disabled_policy_never_routes():
    p = resolve_handoff_policy(enabled=False)
    assert should_route_handoff(0, p) is False


def test_unlimited_policy_always_routes():
    p = resolve_handoff_policy(unlimited=True)
    assert should_route_handoff(999, p) is True


def test_room_max_depth_override():
    p = resolve_handoff_policy(max_depth=2)
    assert [should_route_handoff(d, p) for d in range(4)] == [True, True, False, False]


def test_server_default_used_when_no_room_depth():
    p = resolve_handoff_policy(server_default=1)
    assert p.max_depth == 1


def test_max_depth_floored_at_one():
    p = resolve_handoff_policy(max_depth=0)
    assert p.max_depth == 1


def test_next_mention_depth_increments():
    assert next_mention_depth(0) == 1
    assert next_mention_depth(3) == 4
    assert next_mention_depth(None) == 1
    assert next_mention_depth(-5) == 1


def test_recommended_depth():
    assert recommended_handoff_depth(2) == DEFAULT_HANDOFF_DEPTH
    assert recommended_handoff_depth(10) == 11
