"""Tests for gateway/agent_room_observer_parse.py.

The observer runs on qwen3.7-max via dashscope's OpenAI-compat endpoint,
which is unreliable about emitting native tool calls — it variously returns
a real tool result, an assistant tool_calls array, a bare JSON blob, a
python-style call, or an xml-ish <tool>{...}</tool> wrapper. The parser has
to decode all of these into a RoutingDecision, else the router falls back to
the default member and the decompose/route intent is lost.
"""

from __future__ import annotations

import json

from gateway.agent_room_observer_parse import parse_observer_decision


def _rr(messages):
    return {"messages": messages}


# ── Path A: native tool result row ──────────────────────────────────────

def test_native_tool_result_route():
    rr = _rr([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "route_to_member", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": json.dumps({
            "action": "route_to_member", "member": "finance",
            "reason": "billing", "is_new_topic": True,
        })},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == "finance"
    assert d.is_decompose is False
    assert d.is_new_topic is True


def test_native_tool_result_decompose():
    tasks = [
        {"title": "draft", "body": "draft contract", "assignee": "customer_service", "parents": []},
        {"title": "cost", "body": "calc cost", "assignee": "finance", "parents": [0]},
    ]
    rr = _rr([
        {"role": "tool", "content": json.dumps({
            "action": "decompose_and_route", "tasks": tasks, "reason": "multi-step",
        })},
    ])
    d = parse_observer_decision(rr)
    assert d.is_decompose is True
    assert len(d.decompose_tasks) == 2
    assert d.decompose_tasks[1]["assignee"] == "finance"


# ── Path A2: assistant tool_calls array ─────────────────────────────────

def test_assistant_tool_calls_decompose():
    tasks = [{"title": "t", "body": "b", "assignee": "finance", "parents": []}]
    rr = _rr([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {
                "name": "decompose_and_route",
                "arguments": json.dumps({"tasks": tasks, "reason": "x"}),
            }}
        ]},
    ])
    d = parse_observer_decision(rr)
    assert d.is_decompose is True
    assert d.decompose_tasks[0]["assignee"] == "finance"


# ── Path B: JSON blob in assistant content ──────────────────────────────

def test_text_json_blob_route():
    rr = _rr([
        {"role": "assistant", "content": 'Sure. {"member": "tech_support", "reason": "bug"}'},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == "tech_support"


# ── Path C: python-call form ────────────────────────────────────────────

def test_python_call_route_with_js_bool():
    # qwen frequently emits JS-style lowercase booleans inside a python call.
    rr = _rr([
        {"role": "assistant", "content":
            "route_to_member(member=['finance'], reason='billing', is_new_topic=true)"},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == ["finance"]
    assert d.is_new_topic is True


def test_python_call_decompose_multiline():
    content = (
        "I'll decompose this.\n"
        "decompose_and_route(tasks=[\n"
        "  {'title': 'a', 'body': 'aa', 'assignee': 'finance', 'parents': []},\n"
        "  {'title': 'b', 'body': 'bb', 'assignee': 'customer_service', 'parents': [0]}\n"
        "], reason='two step', is_new_topic=false)"
    )
    d = parse_observer_decision(_rr([{"role": "assistant", "content": content}]))
    assert d.is_decompose is True
    assert len(d.decompose_tasks) == 2
    assert d.decompose_tasks[0]["assignee"] == "finance"


# ── Path D: <tool>{...}</tool> wrapper ──────────────────────────────────

def test_xml_tool_wrapper_route():
    rr = _rr([
        {"role": "assistant", "content":
            '<tool>{"name": "route_to_member", "arguments": {"member": "finance", "reason": "r"}}</tool>'},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == "finance"


# ── Fallback: nothing parseable ─────────────────────────────────────────

def test_pure_prose_falls_back_to_empty_target():
    rr = _rr([
        {"role": "assistant", "content": "I'll break this into a multi-step workflow."},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == ""
    assert d.is_decompose is False


def test_empty_messages_falls_back():
    d = parse_observer_decision({"messages": []})
    assert d.target_member == ""


def test_native_tool_result_wins_over_trailing_prose():
    # A real tool result present alongside a later prose row: the tool
    # result is authoritative (Path A runs before the text fallbacks).
    rr = _rr([
        {"role": "tool", "content": json.dumps({
            "action": "route_to_member", "member": "finance", "reason": "r",
        })},
        {"role": "assistant", "content": "Let me think about this some more..."},
    ])
    d = parse_observer_decision(rr)
    assert d.target_member == "finance"