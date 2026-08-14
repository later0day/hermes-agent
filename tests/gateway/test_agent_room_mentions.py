"""Tests for gateway/agent_room_mentions.py — deterministic @mention routing."""

from __future__ import annotations

from gateway.agent_room_mentions import (
    is_agent_mentioned,
    is_all_agents_mentioned,
    resolve_mention_targets,
    strip_mention_tokens,
)

MEMBERS = ("customer_service", "finance", "tech_support")


def test_single_mention_routes():
    assert resolve_mention_targets(MEMBERS, "ask @finance please", "customer_service") == ["finance"]


def test_all_mention_routes_everyone_but_sender():
    out = resolve_mention_targets(MEMBERS, "hey @all", "customer_service")
    assert out == ["finance", "tech_support"]


def test_sender_excluded_even_if_self_mentioned():
    assert resolve_mention_targets(MEMBERS, "@customer_service go", "customer_service") == []


def test_email_does_not_match():
    assert resolve_mention_targets(("finance",), "reach bob@finance.com", "x") == []


def test_prefix_boundary_no_partial_match():
    # @financely must not match member "finance"
    assert resolve_mention_targets(("finance",), "ping @financely", "x") == []


def test_quoted_block_masked():
    content = "<quoted_message>@finance said hi</quoted_message> thanks"
    assert resolve_mention_targets(("finance",), content, "x") == []


def test_multiple_distinct_mentions_order_and_dedup():
    content = "@finance and @tech_support and @finance again"
    out = resolve_mention_targets(MEMBERS, content, "customer_service")
    assert out == ["finance", "tech_support"]


def test_cjk_boundary_after_name():
    # @finance followed by CJK punctuation is a valid boundary
    assert is_agent_mentioned("请 @finance，谢谢", "finance")


def test_case_insensitive():
    assert is_agent_mentioned("ping @Finance", "finance")
    assert is_all_agents_mentioned("@ALL hello")


def test_no_mention_empty():
    assert resolve_mention_targets(MEMBERS, "just a normal message", "x") == []


def test_strip_removes_own_and_all_tokens():
    assert strip_mention_tokens("@tech_support 请看下", "tech_support") == "请看下"
    assert strip_mention_tokens("@all everyone", "finance") == "everyone"


def test_strip_leaves_other_mentions():
    # stripping for tech_support should not remove @finance
    out = strip_mention_tokens("@tech_support ask @finance too", "tech_support")
    assert "@finance" in out
