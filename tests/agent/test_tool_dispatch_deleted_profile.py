"""A profile deleted while its agent still runs (a room round, the post-turn skill review) must not act: live
2026-10-15 a deleted profile's review called skills_list, whose mkdir(parents=True) rebuilt the removed home."""

import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_constants import get_hermes_home, mark_named_profile_deleted, reset_hermes_home_override, \
    set_hermes_home_override
from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call


def _run_one(agent, name: str) -> tuple[list, list]:
    calls, messages = [], []

    def fake_dispatch(function_name, *_a, **_k):  # what skills_list did: create <home>/skills
        calls.append(function_name)
        (get_hermes_home() / "skills").mkdir(parents=True, exist_ok=True)
        return json.dumps({"success": True})

    msg = SimpleNamespace(content="", tool_calls=[_mock_tool_call(name, "{}", f"c-{name}")])
    with patch("model_tools.handle_function_call", side_effect=fake_dispatch):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
    return calls, messages


@pytest.mark.parametrize("deletion", ["tombstoned", "directory_removed"])
def test_tools_refused_after_the_profile_is_deleted_mid_turn(tmp_path, deletion):
    home = tmp_path / ".hermes" / "profiles" / "doomed"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    agent = _make_agent("skills_list")
    token = set_hermes_home_override(home)
    try:
        calls, _ = _run_one(agent, "skills_list")
        assert calls == ["skills_list"]  # a live profile is unaffected

        shutil.rmtree(home)
        if deletion == "tombstoned":
            mark_named_profile_deleted(home)
        calls, messages = _run_one(agent, "skills_list")
    finally:
        reset_hermes_home_override(token)

    assert calls == []
    assert not home.exists()
    assert "profile has been deleted" in messages[-1]["content"]
