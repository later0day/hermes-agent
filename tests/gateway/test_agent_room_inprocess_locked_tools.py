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

from gateway.agent_room_inprocess_runner import (
    _lock_agent_tools,
    _lock_agent_tools_to_none,
    _strip_toolcode_text,
)


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


# ---------------------------------------------------------------------------
# _lock_agent_tools_to_none — a group-chat member is a conversation role.
# Live testing showed members inheriting the default file/terminal/
# execute_code toolset actually ran host commands and leaked raw <tool_code>
# blocks into the room. A member turn must ship ZERO tools so the model can
# only reply in prose.
# ---------------------------------------------------------------------------


class _MemberAgent:
    def __init__(self):
        # start with a broad default toolset like a real member profile would
        self.tools = [
            {"function": {"name": "terminal"}},
            {"function": {"name": "read_file"}},
            {"function": {"name": "write_file"}},
            {"function": {"name": "execute_code"}},
        ]
        self.valid_tool_names = {"terminal", "read_file", "write_file", "execute_code"}
        self.enabled_toolsets = ["file_tools", "terminal"]


def test_member_lockdown_strips_every_tool():
    agent = _MemberAgent()
    _lock_agent_tools_to_none(agent)
    assert list(agent.tools) == []
    assert agent.valid_tool_names == set()
    assert agent.enabled_toolsets == []


def test_member_lockdown_rejects_readd():
    """tool_search's mid-run rebuild must NOT be able to smuggle tools back
    onto a conversation-only member turn."""
    agent = _MemberAgent()
    _lock_agent_tools_to_none(agent)
    agent.tools.append({"function": {"name": "terminal"}})
    agent.tools.extend([{"function": {"name": "execute_code"}}])
    agent.tools[0:0] = [{"function": {"name": "shell"}}]  # __setitem__ path
    assert list(agent.tools) == []


def test_member_lockdown_stays_empty_through_copy():
    """The prompt-cache plan deep/shallow-copies agent.tools before each
    request; an empty lockdown must stay empty on the wire (the copy must NOT
    resurrect tools, and must NOT raise)."""
    agent = _MemberAgent()
    _lock_agent_tools_to_none(agent)
    assert copy.deepcopy(agent.tools) == []
    assert copy.copy(agent.tools) == []

# ---------------------------------------------------------------------------
# _strip_toolcode_text — defensive scrub of text-form tool-call markup a
# conversation-only member should never emit. Uses the ACTUAL leaked payload
# shape observed in live dashboard testing (backend_engineer typing a
# <tool_code>{"name":"write_file",...}</tool_code> block as prose).
# ---------------------------------------------------------------------------


def test_strip_toolcode_removes_tagged_block():
    reply = (
        "好的，我来实现接口：\n"
        '<tool_code>\n{"name": "write_file", "arguments": '
        '{"path": "/root/tls/app.py", "content": "import os"}}\n</tool_code>\n'
        "以上就是实现。"
    )
    out = _strip_toolcode_text(reply)
    assert "<tool_code>" not in out
    assert "write_file" not in out
    assert "好的，我来实现接口" in out
    assert "以上就是实现" in out


def test_strip_toolcode_handles_unterminated_block():
    """The live leak (#40) was a 4KB <tool_code> block with NO closing tag —
    the model ran out of tokens mid-call. Strip to end-of-string."""
    reply = (
        "实现如下：\n<tool_code>\n"
        '{"name": "write_file", "arguments": {"path": "/root/tls/app.py", '
        '"content": "' + "x" * 3000 + '"'
    )
    out = _strip_toolcode_text(reply)
    assert "<tool_code>" not in out
    assert "write_file" not in out
    assert out.strip() == "实现如下："


def test_strip_toolcode_handles_fenced_block():
    reply = "方案：\n```tool_code\n{\"name\": \"terminal\", \"command\": \"ls\"}\n```\n完成"
    out = _strip_toolcode_text(reply)
    assert "tool_code" not in out
    assert "terminal" not in out
    assert "方案" in out and "完成" in out


def test_strip_toolcode_leaves_clean_reply_untouched():
    reply = "后端接口已实现，QA 首轮 E2E 通过，ready for release。"
    assert _strip_toolcode_text(reply) == reply


def test_strip_toolcode_empty_and_none_safe():
    assert _strip_toolcode_text("") == ""
    assert _strip_toolcode_text(None) is None


def test_strip_toolcode_whole_reply_is_block_yields_empty():
    """A reply that is ONLY a tool_code block collapses to empty — the turn
    produced no real conversational content."""
    reply = '<tool_code>\n{"name": "read_file", "arguments": {"path": "/x"}}\n</tool_code>'
    assert _strip_toolcode_text(reply) == ""
