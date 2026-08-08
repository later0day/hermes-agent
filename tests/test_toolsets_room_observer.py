"""Tests for M1.4 — toolsets.py registration of room_observer + agent/
tool_executor.py dispatch branch for route_to_member.

Ensures the observer profile's ``toolsets: [room_observer]`` config
actually yields:
  1. TOOLSETS['room_observer'] mapping to the ``route_to_member`` tool
     (Spike 3 lockdown verification — no _HERMES_CORE_TOOLS leak-through).
  2. tool_executor.py's central dispatcher having a route_to_member elif
     branch that forwards to tools.room_router_tool.route_to_member.
     Without this branch the LLM's tool call would fall through to the
     "unknown tool" handler and the routing decision would be lost.

Boundary-test coverage: none of M1-B1..B14 directly — M1.4 is
infrastructure only. But the interior of the elif branch is what
delivers M1.3's §9.2 hard-interrupt side effect to a real observer turn,
so we assert the branch exists AND forwards the right arguments.
"""

from __future__ import annotations

import inspect
import re

from toolsets import TOOLSETS


# ---------------------------------------------------------------------------
# toolsets.py registration (Spike 3 lockdown)
# ---------------------------------------------------------------------------


def test_room_observer_toolset_is_registered():
    assert "room_observer" in TOOLSETS


def test_room_observer_grants_only_route_to_member():
    """The whole point of the M1.4 lockdown: observer profile listing
    only this toolset must NOT get terminal / read_file / delegate_task
    / anything else. Just route_to_member."""
    entry = TOOLSETS["room_observer"]
    assert entry["tools"] == ["route_to_member"]


def test_room_observer_has_no_includes():
    """``includes`` is the toolset system's way of composing supersets
    (e.g. hermes-cli includes _HERMES_CORE_TOOLS via named refs). The
    observer must not include ANY other toolset — that would defeat
    the lockdown."""
    entry = TOOLSETS["room_observer"]
    assert entry["includes"] == []


def test_room_observer_description_mentions_route_to_member():
    """description is user-visible when listing toolsets; anchor the
    intent so a later refactor doesn't rename the tool without updating."""
    desc = TOOLSETS["room_observer"]["description"]
    assert "route_to_member" in desc


def test_route_to_member_appears_in_exactly_one_toolset():
    """If route_to_member accidentally ends up in another toolset
    (e.g. hermes-cli), an ordinary CLI agent could call it and mess
    with room routing state. Guard that."""
    owners = [
        name
        for name, entry in TOOLSETS.items()
        if "route_to_member" in entry.get("tools", [])
    ]
    assert owners == ["room_observer"], (
        f"route_to_member leaked into other toolsets: {owners}"
    )


def test_hermes_cli_toolset_does_not_grant_route_to_member():
    """Explicit belt-and-suspenders for the previous test — regression
    guard on the most common core toolset. If a future refactor merges
    all tools into hermes-cli via includes, the observer lockdown breaks."""
    cli = TOOLSETS.get("hermes-cli", {})
    assert "route_to_member" not in cli.get("tools", [])


# ---------------------------------------------------------------------------
# tool_executor.py dispatch branch
# ---------------------------------------------------------------------------


def test_tool_executor_has_route_to_member_branch():
    """M1.4 requires an elif branch in the central tool dispatcher.
    Without it the LLM's tool call falls through to 'unknown tool' and
    §9.2 patch A never fires."""
    import agent.tool_executor as te
    src = inspect.getsource(te)
    assert 'function_name == "route_to_member"' in src, (
        "tool_executor.py missing the 'elif function_name == "
        '"route_to_member"' "' dispatch branch — LLM tool calls will "
        "fall through to unknown-tool handling."
    )


def test_tool_executor_dispatch_imports_room_router_tool():
    """The dispatch branch must actually delegate to
    tools.room_router_tool.route_to_member — not a stub, not a rename."""
    import agent.tool_executor as te
    src = inspect.getsource(te)
    # Look inside the route_to_member elif block for the correct import.
    route_block_match = re.search(
        r'elif function_name == "route_to_member":\s*(.*?)(?=elif function_name == |else:)',
        src,
        re.DOTALL,
    )
    assert route_block_match is not None, "elif block not found"
    block = route_block_match.group(1)
    assert "from tools.room_router_tool import route_to_member" in block, (
        "route_to_member elif block does not import from tools.room_router_tool"
    )


def test_tool_executor_forwards_all_three_arguments():
    """member / reason / is_new_topic — all three must be forwarded from
    the LLM's tool_call arguments to the actual tool. Forgetting
    is_new_topic silently defaults it to False, which would break M1.5's
    last_routed_member cache invalidation."""
    import agent.tool_executor as te
    src = inspect.getsource(te)
    route_block_match = re.search(
        r'elif function_name == "route_to_member":\s*(.*?)(?=elif function_name == |else:)',
        src,
        re.DOTALL,
    )
    assert route_block_match is not None
    block = route_block_match.group(1)
    # All three keyword args referenced in the same block.
    assert 'next_args.get("member"' in block
    assert 'next_args.get("reason"' in block
    assert 'next_args.get("is_new_topic"' in block


def test_tool_executor_wraps_dispatch_in_middleware_like_clarify():
    """Consistency check: the route_to_member branch must run through
    the same _run_agent_tool_execution_middleware wrapper as every other
    tool (approvals, budget accounting, tracing, etc.). Skipping it
    would silently opt route_to_member out of the tool-run guardrails."""
    import agent.tool_executor as te
    src = inspect.getsource(te)
    route_block_match = re.search(
        r'elif function_name == "route_to_member":\s*(.*?)(?=elif function_name == |else:)',
        src,
        re.DOTALL,
    )
    assert route_block_match is not None
    block = route_block_match.group(1)
    assert "_run_agent_tool_execution_middleware" in block, (
        "route_to_member dispatch bypasses the standard middleware wrapper"
    )
