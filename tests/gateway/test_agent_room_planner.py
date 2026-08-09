"""Tests for gateway/agent_room_planner.py — M2.1 + M2.2.

Covers:
  - JSON parsing (lenient, tolerates code fences, malformed input)
  - Hallucination guard (M2-B6: LLM claims existing profile not in roster)
  - Member cap enforcement (M2-B4)
  - Empty/vague requirement handling (M2-B1)
  - Long requirement truncation (M2-B2)
  - Aux LLM unavailable → clear error (M2-B12 cousin)
  - Schema validation of each member field
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from gateway.agent_room_planner import (
    MAX_ROOM_MEMBERS,
    PlannedMember,
    RoomPlan,
    _extract_json_blob,
    _format_roster,
    _validate_member,
    plan_room,
)


# ---------------------------------------------------------------------------
# _extract_json_blob
# ---------------------------------------------------------------------------


def test_extract_json_blob_plain():
    result = _extract_json_blob('{"key": "value"}')
    assert result == {"key": "value"}


def test_extract_json_blob_with_code_fence():
    result = _extract_json_blob('```json\n{"key": "value"}\n```')
    assert result == {"key": "value"}


def test_extract_json_blob_with_surrounding_text():
    result = _extract_json_blob('Here is the plan:\n{"key": "value"}\nDone.')
    assert result == {"key": "value"}


def test_extract_json_blob_empty():
    assert _extract_json_blob("") is None
    assert _extract_json_blob(None) is None


def test_extract_json_blob_malformed():
    assert _extract_json_blob("not json at all") is None
    assert _extract_json_blob("{broken") is None


# ---------------------------------------------------------------------------
# _format_roster
# ---------------------------------------------------------------------------


def test_format_roster_empty():
    assert _format_roster([]) == "(no existing profiles)"


def test_format_roster_with_profiles():
    result = _format_roster([("client_svc", "客服"), ("finance", "财务")])
    assert "client_svc: 客服" in result
    assert "finance: 财务" in result


def test_format_roster_missing_description():
    result = _format_roster([("empty", "")])
    assert "empty: (no description)" in result


# ---------------------------------------------------------------------------
# _validate_member
# ---------------------------------------------------------------------------


def test_validate_member_existing():
    m = _validate_member(
        {"profile": "client_svc", "is_new": False, "name": "Client", "description": "客服"},
        existing_names={"client_svc"},
    )
    assert m is not None
    assert m.profile == "client_svc"
    assert m.is_new is False


def test_validate_member_new():
    m = _validate_member(
        {"profile": None, "is_new": True, "name": "legal", "description": "法务"},
        existing_names={"client_svc"},
    )
    assert m is not None
    assert m.profile is None
    assert m.is_new is True


def test_validate_member_hallucination_guard():
    """M2-B6: LLM says is_new=false but profile not in roster → treat as new."""
    m = _validate_member(
        {"profile": "ghost", "is_new": False, "name": "Ghost", "description": "???"},
        existing_names={"client_svc"},
    )
    assert m is not None
    assert m.is_new is True  # forced to new
    assert m.profile is None  # no existing reference


def test_validate_member_missing_name():
    m = _validate_member({"profile": "x", "is_new": False, "name": ""}, set())
    assert m is None


def test_validate_member_not_dict():
    assert _validate_member("not a dict", set()) is None


# ---------------------------------------------------------------------------
# plan_room — mocked LLM responses
# ---------------------------------------------------------------------------


def _mock_response(content: str):
    """Build a fake call_llm response with .choices[0].message.content."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


_VALID_PLAN = json.dumps({
    "rationale": "Client support needs both customer service and billing",
    "members": [
        {"profile": "client_svc", "is_new": False, "name": "client_svc",
         "description": "Handles customer inquiries", "reason": "matches"},
        {"profile": None, "is_new": True, "name": "billing",
         "description": "Handles billing and payments", "reason": "no existing billing profile"},
    ],
    "room_description": "Customer support room",
})


def test_plan_room_valid_output():
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(_VALID_PLAN)):
        plan = plan_room("I need customer support and billing", [
            ("client_svc", "Handles customer inquiries"),
            ("finance", "Handles financial analysis"),
        ])

    assert plan.is_actionable
    assert len(plan.members) == 2
    assert plan.members[0].profile == "client_svc"
    assert plan.members[0].is_new is False
    assert plan.members[1].is_new is True
    assert plan.members[1].name == "billing"
    assert plan.room_description == "Customer support room"


def test_plan_room_empty_requirement():
    plan = plan_room("", [("x", "y")])
    assert not plan.is_actionable
    assert "empty" in plan.rationale.lower()


def test_plan_room_too_vague():
    vague_response = json.dumps({
        "rationale": "requirement too vague",
        "members": [],
        "room_description": "",
    })
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(vague_response)):
        plan = plan_room("stuff", [("x", "y")])
    assert not plan.is_actionable
    assert "vague" in plan.rationale.lower()


def test_plan_room_truncates_long_requirement():
    long_req = "A" * 3000
    captured_req = []

    def fake_call_llm(**kwargs):
        for msg in kwargs.get("messages", []):
            if msg["role"] == "user":
                captured_req.append(msg["content"])
        return _mock_response(_VALID_PLAN)

    with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm):
        plan_room(long_req, [("x", "y")])

    assert len(captured_req) == 1
    assert len(captured_req[0]) < 3000  # was truncated


def test_plan_room_too_many_members_capped():
    too_many = json.dumps({
        "rationale": "test",
        "members": [
            {"profile": f"p{i}", "is_new": False, "name": f"p{i}", "description": "x"}
            for i in range(MAX_ROOM_MEMBERS + 2)
        ],
        "room_description": "test",
    })
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(too_many)):
        plan = plan_room("test", [(f"p{i}", "x") for i in range(MAX_ROOM_MEMBERS + 2)])

    assert len(plan.members) == MAX_ROOM_MEMBERS


def test_plan_room_aux_unavailable():
    with patch("agent.auxiliary_client.call_llm",
               side_effect=ImportError("no module")):
        plan = plan_room("need help", [("x", "y")])
    assert not plan.is_actionable
    assert "error" in plan.rationale.lower() or "unavailable" in plan.rationale.lower()


def test_plan_room_llm_error():
    with patch("agent.auxiliary_client.call_llm",
               side_effect=RuntimeError("API timeout")):
        plan = plan_room("need help", [("x", "y")])
    assert not plan.is_actionable
    assert "error" in plan.rationale.lower() or "RuntimeError" in plan.rationale


def test_plan_room_malformed_json():
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response("this is not json at all")):
        plan = plan_room("need help", [("x", "y")])
    assert not plan.is_actionable
    assert "json" in plan.rationale.lower()


def test_plan_room_hallucination_filtered():
    """M2-B6: LLM proposes 'ghost' as existing but it's not in roster."""
    hallucinated = json.dumps({
        "rationale": "test",
        "members": [
            {"profile": "ghost", "is_new": False, "name": "Ghost",
             "description": "ghost profile"},
        ],
        "room_description": "test",
    })
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(hallucinated)):
        plan = plan_room("test", [("real", "real profile")])

    assert len(plan.members) == 1
    # Hallucination guard converts it to "new"
    assert plan.members[0].is_new is True
    assert plan.members[0].profile is None


def test_plan_room_to_dict():
    plan = RoomPlan(
        rationale="test",
        members=[PlannedMember(profile="x", is_new=False, name="X", description="desc")],
        room_description="room desc",
    )
    d = plan.to_dict()
    assert d["rationale"] == "test"
    assert len(d["members"]) == 1
    assert d["members"][0]["profile"] == "x"
    assert d["room_description"] == "room desc"


def test_plan_room_new_and_existing_split():
    plan = RoomPlan(
        rationale="test",
        members=[
            PlannedMember(profile="existing", is_new=False, name="E", description="d"),
            PlannedMember(profile=None, is_new=True, name="new", description="d"),
        ],
    )
    assert len(plan.existing_profiles) == 1
    assert plan.existing_profiles[0].name == "E"
    assert len(plan.new_profiles) == 1
    assert plan.new_profiles[0].name == "new"


# ═════════════════════════════════════════════════════════════════════════════
# Live bug regression: profile name sanitization
# ═════════════════════════════════════════════════════════════════════════════
# During M2 live DingTalk test, LLM generated Chinese profile names like
# '客服专员' which the create_profile validator rejected (must match
# [a-z0-9][a-z0-9_-]{0,63}). Fix: server-side sanitizer + SYSTEM_PROMPT rule.

def test_sanitize_valid_name_unchanged():
    from gateway.agent_room_planner import _sanitize_profile_name
    assert _sanitize_profile_name("client_service") == "client_service"
    assert _sanitize_profile_name("finance-1") == "finance-1"
    assert _sanitize_profile_name("a") == "a"


def test_sanitize_chinese_falls_back():
    from gateway.agent_room_planner import _sanitize_profile_name
    # Pure Chinese cannot map to ASCII → fallback
    assert _sanitize_profile_name("客服专员") == "member"


def test_sanitize_mixed_ascii_and_chinese():
    from gateway.agent_room_planner import _sanitize_profile_name
    # ASCII portion is extracted, Chinese dropped
    assert _sanitize_profile_name("财务-finance") == "finance"


def test_sanitize_spaces_and_case():
    from gateway.agent_room_planner import _sanitize_profile_name
    assert _sanitize_profile_name("Client Service") == "client_service"
    assert _sanitize_profile_name("   Sales  Lead  ") == "sales_lead"


def test_sanitize_empty_falls_back():
    from gateway.agent_room_planner import _sanitize_profile_name
    assert _sanitize_profile_name("") == "member"
    assert _sanitize_profile_name("   ") == "member"


def test_sanitize_custom_fallback():
    from gateway.agent_room_planner import _sanitize_profile_name
    assert _sanitize_profile_name("!!!", fallback="bot") == "bot"


def test_plan_room_sanitizes_new_member_names():
    """M2 live bug: LLM output Chinese names → sanitized to ASCII."""
    hallucinated = json.dumps({
        "rationale": "test with Chinese names",
        "members": [
            {"profile": None, "is_new": True, "name": "客服专员",
             "description": "客服支持"},
            {"profile": None, "is_new": True, "name": "财务-finance",
             "description": "财务"},
        ],
        "room_description": "test",
    })
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(hallucinated)):
        plan = plan_room("test", [])

    # 1st member: pure Chinese → "member" fallback
    # 2nd member: has "finance" → sanitized to "finance"
    assert len(plan.members) == 2
    assert plan.members[0].name == "member"
    assert plan.members[1].name == "finance"
    # Neither should be Chinese
    import re
    for m in plan.members:
        assert re.match(r"^[a-z0-9][a-z0-9_-]*$", m.name), f"invalid: {m.name}"


def test_plan_room_dedupes_collided_sanitized_names():
    """Two Chinese names both fall back to 'member' → second gets 'member_1'."""
    all_chinese = json.dumps({
        "rationale": "test",
        "members": [
            {"profile": None, "is_new": True, "name": "客服", "description": "d1"},
            {"profile": None, "is_new": True, "name": "财务", "description": "d2"},
            {"profile": None, "is_new": True, "name": "技术", "description": "d3"},
        ],
        "room_description": "test",
    })
    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_response(all_chinese)):
        plan = plan_room("test", [])

    names = [m.name for m in plan.members]
    # All different
    assert len(names) == len(set(names))
    # All start with "member"
    assert all(n.startswith("member") for n in names)
