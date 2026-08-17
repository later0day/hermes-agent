"""Tests for gateway/agent_room_task_orchestrator.py — M4.3."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.agent_room_task_orchestrator import (
    OrchestrationResult,
    OrchestratorError,
    PlannedSubtask,
    SubtaskResult,
    build_subtasks,
    orchestrate,
    render_subtask_results_for_synthesis,
    topological_levels,
)


# ═══════════════════════════════════════════════════════════════════════
# build_subtasks — validation + normalization
# ═══════════════════════════════════════════════════════════════════════


def test_build_subtasks_drops_empty_title():
    tasks = build_subtasks(
        [
            {"title": "OK", "assignee": "a"},
            {"title": "", "assignee": "b"},
            {"title": "  ", "assignee": "c"},
            {"assignee": "d"},  # missing title
        ],
        room_members=["a", "b", "c", "d"],
    )
    assert [t.title for t in tasks] == ["OK"]


def test_build_subtasks_m4_b3_assignee_validation():
    """M4-B3: assignee not in room roster → falls back to default_member."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "ghost"},           # unknown → fallback
            {"title": "T1", "assignee": "legal"},           # valid
            {"title": "T2", "assignee": ""},                 # empty → fallback
        ],
        room_members=["legal", "client_svc"],
        default_member="client_svc",
    )
    assert len(tasks) == 3
    assert tasks[0].assignee == "client_svc"  # ghost → default
    assert tasks[1].assignee == "legal"
    assert tasks[2].assignee == "client_svc"  # empty → default


def test_build_subtasks_drops_when_no_valid_fallback():
    """If assignee is unknown AND default_member is not in roster, drop."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "ghost"},
        ],
        room_members=["legal"],
        default_member="not_in_roster",
    )
    # default_member not in roster → falls back to members[0]="legal"
    assert len(tasks) == 1
    assert tasks[0].assignee == "legal"


def test_build_subtasks_no_members_at_all():
    """Edge case: empty roster → drop everything."""
    tasks = build_subtasks(
        [{"title": "T0", "assignee": "anyone"}],
        room_members=[],
    )
    assert tasks == []


def test_build_subtasks_parents_cleaned():
    """Non-int, out-of-range, self-parent, and duplicate indices dropped."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0, 0, "x", 99, True, 1]},
        ],
        room_members=["a"],
    )
    assert tasks[1].parents == (0,)  # only 0 keeps (dupe, str, oor, bool, self all dropped)


def test_build_subtasks_m4_b2_cycle_rejected():
    """M4-B2: cycles rejected at build time."""
    with pytest.raises(OrchestratorError, match="cycle"):
        build_subtasks(
            [
                {"title": "T0", "assignee": "a", "parents": [1]},
                {"title": "T1", "assignee": "a", "parents": [0]},
            ],
            room_members=["a"],
        )


def test_build_subtasks_diamond_dag_ok():
    """A diamond (0 → 1, 0 → 2, 1+2 → 3) is a valid DAG."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
            {"title": "T2", "assignee": "a", "parents": [0]},
            {"title": "T3", "assignee": "a", "parents": [1, 2]},
        ],
        room_members=["a"],
    )
    assert len(tasks) == 4
    assert tasks[3].parents == (1, 2)


# ═══════════════════════════════════════════════════════════════════════
# topological_levels
# ═══════════════════════════════════════════════════════════════════════


def test_topological_levels_diamond():
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
            {"title": "T2", "assignee": "a", "parents": [0]},
            {"title": "T3", "assignee": "a", "parents": [1, 2]},
        ],
        room_members=["a"],
    )
    levels = topological_levels(tasks)
    assert len(levels) == 3
    assert [t.title for t in levels[0]] == ["T0"]
    assert sorted(t.title for t in levels[1]) == ["T1", "T2"]
    assert [t.title for t in levels[2]] == ["T3"]


def test_topological_levels_all_independent():
    tasks = build_subtasks(
        [{"title": f"T{i}", "assignee": "a"} for i in range(3)],
        room_members=["a"],
    )
    levels = topological_levels(tasks)
    assert len(levels) == 1
    assert len(levels[0]) == 3


def test_topological_levels_linear():
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
            {"title": "T2", "assignee": "a", "parents": [1]},
        ],
        room_members=["a"],
    )
    levels = topological_levels(tasks)
    assert len(levels) == 3
    assert [len(l) for l in levels] == [1, 1, 1]


# ═══════════════════════════════════════════════════════════════════════
# orchestrate — execution
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_orchestrate_empty_returns_empty_result():
    result = await orchestrate([], room_id="r", dispatcher=AsyncMock())
    assert result.total_subtasks == 0
    assert result.results == []


@pytest.mark.asyncio
async def test_orchestrate_single_task_dispatched():
    tasks = build_subtasks(
        [{"title": "hi", "body": "say hello", "assignee": "a"}],
        room_members=["a"],
    )
    disp = AsyncMock(return_value="reply from a")
    result = await orchestrate(tasks, room_id="r", dispatcher=disp)
    disp.assert_awaited_once()
    call_args = disp.await_args[0]
    # dispatcher(member, session_id, message, history)
    assert call_args[0] == "a"
    assert "hi" in call_args[2] and "say hello" in call_args[2]
    assert result.completed == 1
    assert result.results[0].status == "success"
    assert result.results[0].reply == "reply from a"


@pytest.mark.asyncio
async def test_orchestrate_diamond_dag_execution_order():
    """Level 0 → level 1 (parallel) → level 2. Verify T3 waits for T1+T2."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
            {"title": "T2", "assignee": "a", "parents": [0]},
            {"title": "T3", "assignee": "a", "parents": [1, 2]},
        ],
        room_members=["a"],
    )
    order: list[str] = []

    async def _disp(member, sid, msg, hist):
        order.append(f"start:{msg.split(chr(10))[0]}")
        await asyncio.sleep(0.01)
        order.append(f"done:{msg.split(chr(10))[0]}")
        return f"reply-for-{msg.split(chr(10))[0]}"

    result = await orchestrate(tasks, room_id="r", dispatcher=_disp)
    assert result.completed == 4

    # T0 finishes before T1 and T2 start
    assert order.index("done:T0") < order.index("start:T1")
    assert order.index("done:T0") < order.index("start:T2")
    # T1 and T2 both finish before T3 starts
    assert order.index("done:T1") < order.index("start:T3")
    assert order.index("done:T2") < order.index("start:T3")
    # T1 and T2 run in parallel (both start before either done)
    assert order.index("start:T1") < order.index("done:T2") or \
           order.index("start:T2") < order.index("done:T1")


@pytest.mark.asyncio
async def test_orchestrate_failed_parent_skips_children():
    """Failure in one task → dependents marked skipped."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
            {"title": "T2", "assignee": "a", "parents": [0]},  # sibling — ok
        ],
        room_members=["a"],
    )

    async def _disp(member, sid, msg, hist):
        if msg.startswith("T0"):
            raise RuntimeError("T0 boom")
        return f"reply from {msg}"

    result = await orchestrate(tasks, room_id="r", dispatcher=_disp)
    assert result.failed == 1
    assert result.skipped == 2   # both T1 and T2 depend on failed T0
    assert result.completed == 0

    statuses = {r.index: r.status for r in result.results}
    assert statuses[0] == "failed"
    assert statuses[1] == "skipped_parent_failed"
    assert statuses[2] == "skipped_parent_failed"


@pytest.mark.asyncio
async def test_m4_b4_all_subtasks_failed_still_returns_result():
    """M4-B4: even if every subtask fails, orchestrate returns a full
    result — no crash, synthesis turn still runs."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a"},
        ],
        room_members=["a"],
    )

    async def _disp(*args, **kwargs):
        raise RuntimeError("total meltdown")

    result = await orchestrate(tasks, room_id="r", dispatcher=_disp)
    assert result.total_subtasks == 2
    assert result.failed == 2
    assert result.completed == 0
    assert all(r.status == "failed" for r in result.results)


@pytest.mark.asyncio
async def test_m4_b5_fence_stops_orchestration():
    """M4-B5: fence check triggered mid-flight → all remaining subtasks
    marked fenced, orchestration returns early."""
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "a", "parents": [0]},
        ],
        room_members=["a"],
    )

    fenced = {"active": False}

    async def _disp(member, sid, msg, hist):
        # Simulate that a room delete happens after T0 completes
        if msg.startswith("T0"):
            reply = "T0 reply"
            fenced["active"] = True
            return reply
        return f"reply from {msg}"

    def _fence(sid: str) -> bool:
        return fenced["active"]

    result = await orchestrate(
        tasks, room_id="r", dispatcher=_disp, fence_gate=_fence,
    )
    assert result.fenced_mid_flight is True
    # T0 succeeded before fence flipped, T1 fenced
    assert result.results[0].status == "success"
    assert result.results[1].status == "fenced"


@pytest.mark.asyncio
async def test_fence_before_dispatch_blocks_everything():
    """Pre-existing fence: all tasks marked fenced, none dispatched."""
    tasks = build_subtasks(
        [{"title": "T0", "assignee": "a"}],
        room_members=["a"],
    )
    disp = AsyncMock(return_value="never")
    result = await orchestrate(
        tasks, room_id="r", dispatcher=disp, fence_gate=lambda sid: True,
    )
    disp.assert_not_awaited()
    assert result.fenced_mid_flight is True
    assert result.results[0].status == "fenced"


@pytest.mark.asyncio
async def test_projection_provider_called_per_task():
    tasks = build_subtasks(
        [
            {"title": "T0", "assignee": "a"},
            {"title": "T1", "assignee": "b"},
        ],
        room_members=["a", "b"],
    )
    calls: list[str] = []

    def _proj(member: str) -> list[dict]:
        calls.append(member)
        return [{"role": "user", "content": f"context for {member}"}]

    disp = AsyncMock(return_value="ok")
    await orchestrate(
        tasks, room_id="r", dispatcher=disp, projection_provider=_proj,
    )
    assert sorted(calls) == ["a", "b"]


@pytest.mark.asyncio
async def test_session_id_builder_is_customizable():
    tasks = build_subtasks(
        [{"title": "T0", "assignee": "a"}],
        room_members=["a"],
    )
    captured: list[str] = []
    disp = AsyncMock(return_value="ok")

    def _sid(member: str, idx: int) -> str:
        s = f"custom_{member}_{idx}"
        captured.append(s)
        return s

    await orchestrate(
        tasks, room_id="r", dispatcher=disp, session_id_for_member=_sid,
    )
    assert captured == ["custom_a_0"]
    # dispatcher receives the custom session id
    disp.assert_awaited_once()
    assert disp.await_args[0][1] == "custom_a_0"


# ═══════════════════════════════════════════════════════════════════════
# Synthesis rendering
# ═══════════════════════════════════════════════════════════════════════


def test_render_synthesis_success_only():
    result = OrchestrationResult(
        total_subtasks=2, completed=2,
        results=[
            SubtaskResult(0, "T0", "a", "success", reply="reply 0"),
            SubtaskResult(1, "T1", "b", "success", reply="reply 1"),
        ],
    )
    text = render_subtask_results_for_synthesis(result)
    assert "[0]" in text and "[1]" in text
    assert "'T0' · a · success" in text
    assert "reply 0" in text
    assert "reply 1" in text


def test_render_synthesis_with_failures_and_skips():
    result = OrchestrationResult(
        total_subtasks=3, completed=1, failed=1, skipped=1,
        results=[
            SubtaskResult(0, "T0", "a", "success", reply="ok"),
            SubtaskResult(1, "T1", "b", "failed", error="RuntimeError: boom"),
            SubtaskResult(2, "T2", "c", "skipped_parent_failed"),
        ],
    )
    text = render_subtask_results_for_synthesis(result)
    assert "1/3 completed, 1 failed, 1 skipped" in text
    assert "boom" in text
    assert "skipped because parent" in text


def test_render_synthesis_truncates_long_reply():
    long = "x" * 2000
    result = OrchestrationResult(
        total_subtasks=1, completed=1,
        results=[SubtaskResult(0, "T0", "a", "success", reply=long)],
    )
    text = render_subtask_results_for_synthesis(result)
    assert "…" in text
    assert len(text) < 1200  # header + truncated body


def test_render_synthesis_fenced_note():
    result = OrchestrationResult(
        total_subtasks=1, fenced_mid_flight=True,
        results=[SubtaskResult(0, "T0", "a", "fenced")],
    )
    text = render_subtask_results_for_synthesis(result)
    assert "fenced mid-flight" in text
    assert "room state changed" in text


def test_render_synthesis_empty_returns_placeholder():
    result = OrchestrationResult(total_subtasks=0)
    assert render_subtask_results_for_synthesis(result) == "(no subtasks)"


# ═══════════════════════════════════════════════════════════════════════
# OrchestrationResult.to_dict
# ═══════════════════════════════════════════════════════════════════════


def test_orchestration_result_to_dict_shape():
    result = OrchestrationResult(
        total_subtasks=2, completed=1, failed=1,
        results=[
            SubtaskResult(0, "T0", "a", "success", reply="ok"),
            SubtaskResult(1, "T1", "b", "failed", error="boom"),
        ],
    )
    d = result.to_dict()
    assert d["total_subtasks"] == 2
    assert d["completed"] == 1
    assert d["failed"] == 1
    assert len(d["results"]) == 2
    assert d["results"][0]["status"] == "success"
    assert d["results"][1]["error"] == "boom"
