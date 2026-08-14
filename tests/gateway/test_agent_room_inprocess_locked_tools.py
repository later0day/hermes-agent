"""Regression test for the observer tool-lockdown in
gateway/agent_room_inprocess_runner._lock_agent_tools.

Root cause this guards against: the lockdown installs a ``list`` subclass
(``_LockedTools``) whose append/extend/__setitem__ are no-ops, so the
gateway's mid-run tool_search rebuild can't re-add tools the observer must
not see. But the prompt-cache plan deep-copies ``agent.tools`` before every
API request (agent/prompt_caching.strip_anthropic_tool_cache_control). If
_LockedTools has no __deepcopy__, copy.deepcopy rebuilds a fresh instance
via extend/append — both no-ops — yielding an EMPTY tools list on the wire.
tool_choice then has nothing to bind to and qwen emits prose instead of a
route/decompose call. __deepcopy__/__copy__ must return a populated copy.
"""

from __future__ import annotations

import copy

from gateway.agent_room_inprocess_runner import _lock_agent_tools


class _FakeAgent:
    def __init__(self):
        self.tools = []
        self.valid_tool_names = set()


def test_locked_tools_survive_deepcopy():
    agent = _FakeAgent()
    _lock_agent_tools(agent, ["room_observer"])

    # Lockdown resolved the observer's single routing tool.
    names = {t["function"]["name"] for t in agent.tools}
    assert names == {"route_to_member"}

    # The prompt-cache plan deep-copies tools before every request; the copy
    # must retain all tools (this is the actual bug that emptied the wire).
    dc = copy.deepcopy(agent.tools)
    assert len(dc) == len(agent.tools)
    assert {t["function"]["name"] for t in dc} == names

    # Shallow copy likewise preserves contents.
    sc = copy.copy(agent.tools)
    assert {t["function"]["name"] for t in sc} == names


def test_locked_tools_reject_readd():
    agent = _FakeAgent()
    _lock_agent_tools(agent, ["room_observer"])
    before = len(agent.tools)

    # tool_search's mid-run rebuild tries to append/extend — must be no-ops
    # so the observer never regains the broader toolset.
    agent.tools.append({"function": {"name": "shell"}})
    agent.tools.extend([{"function": {"name": "write_file"}}])
    assert len(agent.tools) == before
    assert {t["function"]["name"] for t in agent.tools} == {
        "route_to_member",
    }