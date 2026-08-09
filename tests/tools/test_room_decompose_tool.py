"""Tests for tools/room_decompose_tool.py — M4.2.

Covers the decompose_and_route tool's behavior AND the loop
termination side effect (same §9.2 patch A pattern as route_to_member).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.room_decompose_tool import (
    DECOMPOSE_ACTION,
    DECOMPOSE_AND_ROUTE_SCHEMA,
    decompose_and_route,
)


# ─── Return format ──────────────────────────────────────────────────────

def test_returns_json_string_with_action_sentinel():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "Draft contract", "assignee": "legal", "parents": []},
            ],
            reason="single-step task",
        )
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["action"] == DECOMPOSE_ACTION
    assert isinstance(parsed["tasks"], list)
    assert len(parsed["tasks"]) == 1
    assert parsed["reason"] == "single-step task"


def test_output_utf8_safe_for_chinese():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "起草合同", "body": "客户是 A 公司", "assignee": "legal"},
            ],
            reason="拆分为 3 步：起草→算账→发送",
        )
    parsed = json.loads(result)
    assert parsed["tasks"][0]["title"] == "起草合同"
    assert "\\u" not in result  # ensure_ascii=False honored


# ─── Input normalization ────────────────────────────────────────────────

def test_drops_malformed_task_entries():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "Valid", "assignee": "legal"},
                "not-a-dict",
                {"title": "", "assignee": "finance"},   # empty title dropped
                {"assignee": "x"},                       # missing title
                {"title": "OK2", "assignee": "finance"},
            ],
            reason="test",
        )
    parsed = json.loads(result)
    titles = [t["title"] for t in parsed["tasks"]]
    assert titles == ["Valid", "OK2"]


def test_parents_indices_cleaned():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "T0", "assignee": "a"},
                {"title": "T1", "assignee": "b", "parents": [0]},
                {"title": "T2", "assignee": "c", "parents": [0, 99, "bogus", 2, True]},
                {"title": "T3", "assignee": "d", "parents": "not-a-list"},
            ],
            reason="test",
        )
    parsed = json.loads(result)
    # T2's parents: 99 out of range → drop, "bogus" → drop,
    # 2 == self index → drop, True is bool → drop, only [0] remains
    assert parsed["tasks"][2]["parents"] == [0]
    # T3's parents "not-a-list" → empty
    assert parsed["tasks"][3]["parents"] == []


def test_self_parent_rejected():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "loops", "assignee": "a", "parents": [0]},
            ],
            reason="test self-loop",
        )
    parsed = json.loads(result)
    # Task at index 0 with parents=[0] → self-parent stripped
    assert parsed["tasks"][0]["parents"] == []


def test_title_truncation_to_200_chars():
    long_title = "A" * 500
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[{"title": long_title, "assignee": "a"}],
            reason="test",
        )
    parsed = json.loads(result)
    assert len(parsed["tasks"][0]["title"]) == 200


def test_empty_tasks_list_yields_empty_output():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(tasks=[], reason="not actually complex")
    parsed = json.loads(result)
    assert parsed["tasks"] == []


def test_non_list_tasks_coerced_to_empty():
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(tasks="not-a-list", reason="test")  # type: ignore[arg-type]
    parsed = json.loads(result)
    assert parsed["tasks"] == []


# ─── §9.2 Loop termination ──────────────────────────────────────────────

def test_calls_request_hard_interrupt_on_active_agent():
    """Same interrupt requirement as route_to_member."""
    fake_agent = MagicMock()
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=fake_agent,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        return_value=True,
    ) as m_interrupt:
        decompose_and_route(
            tasks=[{"title": "x", "assignee": "y"}],
            reason="test",
        )
    m_interrupt.assert_called_once()
    called_agent = m_interrupt.call_args[0][0]
    assert called_agent is fake_agent


def test_returns_decision_even_when_interrupt_fails():
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=MagicMock(),
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        return_value=False,
    ):
        result = decompose_and_route(
            tasks=[{"title": "x", "assignee": "y"}],
            reason="test",
        )
    parsed = json.loads(result)
    assert parsed["action"] == DECOMPOSE_ACTION
    assert len(parsed["tasks"]) == 1


def test_returns_decision_even_when_interrupt_raises():
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=MagicMock(),
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
        side_effect=RuntimeError("simulated ABI failure"),
    ):
        result = decompose_and_route(
            tasks=[{"title": "x", "assignee": "y"}],
            reason="test",
        )
    parsed = json.loads(result)
    assert parsed["action"] == DECOMPOSE_ACTION


def test_does_not_interrupt_when_no_active_agent():
    with patch(
        "agent.subagent_lifecycle.get_active_subagent_parent",
        return_value=None,
    ), patch(
        "agent.interrupt_compat.request_hard_interrupt",
    ) as m_interrupt:
        decompose_and_route(tasks=[{"title": "x", "assignee": "y"}], reason="test")
    m_interrupt.assert_not_called()


# ─── Schema ─────────────────────────────────────────────────────────────

def test_schema_required_matches_function_signature():
    params = DECOMPOSE_AND_ROUTE_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"tasks", "reason"}

    props = params["properties"]
    assert set(props.keys()) == {"tasks", "reason", "is_new_topic"}


def test_schema_tasks_is_bounded_array():
    tasks_prop = DECOMPOSE_AND_ROUTE_SCHEMA["parameters"]["properties"]["tasks"]
    assert tasks_prop["type"] == "array"
    assert tasks_prop.get("minItems") == 1
    assert tasks_prop.get("maxItems") == 10
    assert tasks_prop["items"]["type"] == "object"


def test_schema_task_item_has_correct_fields():
    task_item = DECOMPOSE_AND_ROUTE_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    props = task_item["properties"]
    assert "title" in props
    assert "body" in props
    assert "assignee" in props
    assert "parents" in props
    assert props["title"]["type"] == "string"
    assert props["assignee"]["type"] == "string"
    assert props["parents"]["type"] == "array"
    assert props["parents"]["items"]["type"] == "integer"


def test_registered_in_tool_registry():
    """decompose_and_route MUST be registered so _compute_tool_definitions
    finds it when observer's toolsets=[room_observer]."""
    from tools.registry import registry as _reg_singleton
    from toolsets import resolve_toolset
    import tools.room_decompose_tool  # noqa: F401 — triggers registry.register
    import tools.room_router_tool  # noqa: F401 — sibling tool in the same toolset

    tool_names = set(resolve_toolset("room_observer"))
    defs = _reg_singleton.get_definitions(tool_names, quiet=True)
    schema_names = {d.get("function", {}).get("name") for d in defs}
    assert "decompose_and_route" in schema_names
    assert "route_to_member" in schema_names


# ─── LLM output shapes ──────────────────────────────────────────────────

def test_full_dag_roundtrip():
    """Complex DAG: draft → cost + review → send (3 root, then 2, then 1)."""
    with patch("agent.subagent_lifecycle.get_active_subagent_parent", return_value=None):
        result = decompose_and_route(
            tasks=[
                {"title": "起草合同", "body": "客户是 A 公司", "assignee": "legal"},
                {"title": "算成本", "body": "基于合同金额", "assignee": "finance", "parents": [0]},
                {"title": "内部审阅", "body": "法务和技术过一遍", "assignee": "legal", "parents": [0]},
                {"title": "发给客户", "body": "汇总最终版本", "assignee": "client_svc", "parents": [1, 2]},
            ],
            reason="4-step DAG with parallel middle layer",
            is_new_topic=True,
        )
    parsed = json.loads(result)
    assert parsed["action"] == DECOMPOSE_ACTION
    assert len(parsed["tasks"]) == 4
    assert parsed["tasks"][3]["parents"] == [1, 2]  # both parallel dependencies preserved
    assert parsed["is_new_topic"] is True


def test_toolset_registration():
    """room_observer toolset must include both route_to_member AND
    decompose_and_route (M4.2)."""
    from toolsets import resolve_toolset
    tool_names = resolve_toolset("room_observer")
    assert "route_to_member" in tool_names
    assert "decompose_and_route" in tool_names
