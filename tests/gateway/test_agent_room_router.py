"""Tests for gateway/agent_room_router.py — M1.5.

Covers the complete §6.1 five-step flow with fully mocked dependencies
(no real gateway / no real agent turns / no real DingTalk). Boundary
tests owned by this milestone (per EXECUTION_PLAN.md):
  M1-B4  observer returns member="" → falls back to default_member
  M1-B5  webhook editor raises → routing still succeeds (ack edit is best-effort)
  M1-B6  member turn internal delegate_task use (out of scope for router)
  M1-B7  concurrent messages get independent routing (no per-msg state)
  M1-B10 room re-bound mid-flight → Fence drops the in-flight decision
  M1-B12 aux LLM unavailable → classifier raises → observer path is taken

Also verifies §5.4 N4 reuse conditions, §6.2 session_id conventions,
and §8 Rule B cross-member summary parse + application.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.agent_room_router import (
    AgentRoomRouter,
    ClassifierResult,
    CrossMemberContext,
    RoutingDecision,
)
from gateway.agent_room_firsthop import FirstHopResult
from gateway.agent_room_store import AgentRoomStore


# ---------------------------------------------------------------------------
# Fixtures — a router with all callables mocked, and a real Store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return AgentRoomStore(tmp_path / "rooms.sqlite")


@pytest.fixture
def room(store):
    return store.create_room(
        "room_1",
        "Support",
        observer_profile="room_support_observer",
        members=["client_svc", "finance"],
        default_member="client_svc",
    )


@pytest.fixture
def mocks():
    """Bundle of async mocks for every injected callable."""
    return {
        "ack_sender": AsyncMock(return_value="ack-handle-1"),
        "ack_editor": AsyncMock(return_value=None),
        "classifier": AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        "observer_runner": AsyncMock(
            return_value=RoutingDecision(
                target_member="client_svc",
                reason="best fit",
                is_new_topic=True,
                reused_last_route=False,
            )
        ),
        "member_dispatcher": AsyncMock(return_value="member reply text"),
    }


@pytest.fixture
def router(store, mocks):
    return AgentRoomRouter(store=store, **mocks)


# ---------------------------------------------------------------------------
# §6.2 session_id conventions
# ---------------------------------------------------------------------------


def test_observer_session_id_format():
    assert AgentRoomRouter.observer_session_id("room_abc") == "room_observer:room_abc"


def test_member_session_id_format():
    assert (
        AgentRoomRouter.member_session_id("room_abc", "client_svc")
        == "room_member:room_abc:client_svc"
    )


# ---------------------------------------------------------------------------
# §5.4 N4 classifier reuse condition
# ---------------------------------------------------------------------------


def test_classifier_reuses_when_not_new_topic_and_high_confidence():
    result = ClassifierResult(is_new_topic=False, confidence=0.9)
    assert result.should_reuse_last_routed() is True


def test_classifier_does_not_reuse_when_new_topic():
    result = ClassifierResult(is_new_topic=True, confidence=0.95)
    assert result.should_reuse_last_routed() is False


def test_classifier_does_not_reuse_at_low_confidence():
    result = ClassifierResult(is_new_topic=False, confidence=0.6)
    assert result.should_reuse_last_routed() is False


def test_classifier_threshold_is_strictly_greater_than():
    """Boundary: confidence=0.7 exactly should NOT reuse."""
    result = ClassifierResult(is_new_topic=False, confidence=0.7)
    assert result.should_reuse_last_routed() is False


# ---------------------------------------------------------------------------
# §8 Rule B cross-member summary parse
# ---------------------------------------------------------------------------


def test_extract_cross_member_context_recognizes_summary():
    reason = "上一位处理人 client_svc 的回复摘要: 用户询问了退款流程"
    ctx = AgentRoomRouter._extract_cross_member_context(reason)
    assert ctx.previous_member == "client_svc"
    assert ctx.summary == "用户询问了退款流程"
    assert ctx.has_summary() is True


def test_extract_cross_member_context_handles_fullwidth_colon():
    """SOUL.md template uses ':', but observer LLMs may emit '：' (U+FF1A)."""
    reason = "上一位处理人 finance 的回复摘要：账单是 500 元"
    ctx = AgentRoomRouter._extract_cross_member_context(reason)
    assert ctx.previous_member == "finance"
    assert ctx.summary == "账单是 500 元"


def test_extract_cross_member_context_absent_when_no_prefix():
    ctx = AgentRoomRouter._extract_cross_member_context("plain routing reason")
    assert ctx.has_summary() is False
    assert ctx.previous_member is None


def test_extract_cross_member_context_absent_on_empty_reason():
    ctx = AgentRoomRouter._extract_cross_member_context("")
    assert ctx.has_summary() is False


def test_apply_cross_member_prefix_wraps_message_in_blockquote():
    ctx = CrossMemberContext(previous_member="client_svc", summary="answered refund")
    wrapped = AgentRoomRouter._apply_cross_member_prefix(
        "然后我要问账单", ctx
    )
    assert "> 上一位处理人 client_svc 的回复摘要：answered refund" in wrapped
    assert "> 用户消息：然后我要问账单" in wrapped
    assert wrapped.count(">") >= 3  # 3-line blockquote


def test_apply_cross_member_prefix_passthrough_when_no_summary():
    ctx = CrossMemberContext(previous_member=None, summary=None)
    assert (
        AgentRoomRouter._apply_cross_member_prefix("msg", ctx)
        == "msg"
    )


# ---------------------------------------------------------------------------
# §6.1 Step 1 — immediate acknowledgement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_sends_ack_immediately(router, mocks, room):
    source = object()
    await router.process_message(source, "hello", [], room)

    mocks["ack_sender"].assert_awaited_once()
    ack_args = mocks["ack_sender"].await_args
    assert ack_args[0][0] is source
    assert "正在为你选择处理人" in ack_args[0][1]


# ---------------------------------------------------------------------------
# §6.1 Step 2 — N4 classifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step2_classifier_receives_tail_5_history_and_last_routed(
    router, mocks, room,
):
    history = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
    # Preload last_routed so the classifier gets both inputs
    await router._set_last_routed(room.room_id, "client_svc")

    await router.process_message(object(), "current", history, room)

    call = mocks["classifier"].await_args
    fed_history, fed_last_routed = call[0]
    assert len(fed_history) == 5  # tail 5
    assert fed_history[-1]["content"] == "msg9"
    assert fed_last_routed == "client_svc"


@pytest.mark.asyncio
async def test_step2_classifier_handles_empty_history(router, mocks, room):
    await router.process_message(object(), "first msg", [], room)

    call = mocks["classifier"].await_args
    fed_history, fed_last_routed = call[0]
    assert fed_history == []
    assert fed_last_routed is None


# ---------------------------------------------------------------------------
# §6.1 Step 3 — N4 fast path (reuse)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step3_n4_reuse_skips_observer_turn_entirely(router, mocks, room):
    """§5.4 fast path: continuation with high confidence + prior route
    → no observer turn. Observer runner MUST NOT be called."""
    await router._set_last_routed(room.room_id, "finance")
    mocks["classifier"].return_value = ClassifierResult(
        is_new_topic=False, confidence=0.9,
    )

    result = await router.process_message(object(), "next", [], room)

    mocks["observer_runner"].assert_not_awaited()
    assert result["target_member"] == "finance"
    assert result["reused_last_route"] is True


@pytest.mark.asyncio
async def test_step3_first_message_no_reuse_even_when_classifier_says_continuation(
    router, mocks, room,
):
    """Cold cache: last_routed is None → cannot reuse even if classifier
    says continuation. Must run observer turn."""
    assert await router._get_last_routed(room.room_id) is None
    mocks["classifier"].return_value = ClassifierResult(
        is_new_topic=False, confidence=0.95,
    )

    result = await router.process_message(object(), "first", [], room)

    mocks["observer_runner"].assert_awaited_once()
    assert result["reused_last_route"] is False


# ---------------------------------------------------------------------------
# §6.1 Step 3 — full observer path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step3_observer_receives_correct_arguments(router, mocks, room):
    source = object()
    history = [{"role": "user", "content": "prior"}]

    await router.process_message(source, "new msg", history, room)

    call = mocks["observer_runner"].await_args
    observer_profile, session_id, fed_source, fed_history, fed_msg = call[0]
    assert observer_profile == "room_support_observer"
    assert session_id == "room_observer:room_1"
    assert fed_source is source
    assert fed_history == history
    assert fed_msg == "new msg"


@pytest.mark.asyncio
async def test_step3_updates_last_routed_after_successful_route(router, mocks, room):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="finance",
        reason="new topic",
        is_new_topic=True,
        reused_last_route=False,
    )

    await router.process_message(object(), "msg", [], room)

    assert await router._get_last_routed(room.room_id) == "finance"


# ---------------------------------------------------------------------------
# M1-B4: observer returns invalid member → default_member fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m1_b4_empty_member_falls_back_to_default(router, mocks, room):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="",
        reason="uncertain",
        is_new_topic=True,
        reused_last_route=False,
    )

    result = await router.process_message(object(), "msg", [], room)

    # room's default_member is "client_svc"
    assert result["target_member"] == "client_svc"


@pytest.mark.asyncio
async def test_m1_b4_unknown_member_name_falls_back_to_default(router, mocks, room):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="ghost_profile",
        reason="hallucinated",
        is_new_topic=True,
        reused_last_route=False,
    )

    result = await router.process_message(object(), "msg", [], room)

    assert result["target_member"] == "client_svc"


@pytest.mark.asyncio
async def test_m1_b4_falls_back_to_members_zero_when_no_explicit_default(
    store, mocks,
):
    room = store.create_room(
        "room_x", "X",
        observer_profile="room_x_observer",
        members=["alpha", "beta"],  # no default_member set
    )
    router = AgentRoomRouter(store=store, **mocks)
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="",
        reason="uncertain",
        is_new_topic=True,
        reused_last_route=False,
    )

    result = await router.process_message(object(), "msg", [], room)

    assert result["target_member"] == "alpha"  # members[0]


# ---------------------------------------------------------------------------
# M1-B5: ack editor failure must not derail routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m1_b5_ack_editor_is_no_longer_called(
    router, mocks, room,
):
    """Post-live-fix: ack editor is no longer invoked (Step 4 was
    removed for cleaner UX). The initial ack (Step 1) is kept, but
    the '已转交给 X 处理...' edit was pure noise since the member's
    real reply arrives on its own separate IM message.

    Verify:
      1. ack_editor is NOT called
      2. Dispatch still reaches the member
      3. Member reply still lands
    """
    mocks["ack_editor"].side_effect = RuntimeError("would raise if called")

    result = await router.process_message(object(), "msg", [], room)

    mocks["ack_editor"].assert_not_awaited()  # Step 4 removed
    mocks["member_dispatcher"].assert_awaited_once()
    assert result["target_member"] == "client_svc"
    assert result["reply"] == "member reply text"


# ---------------------------------------------------------------------------
# §6.1 Step 4.5 — §8 Rule B summary injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step4_5_prepends_cross_member_summary_to_member_input(
    router, mocks, room,
):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="finance",
        reason="上一位处理人 client_svc 的回复摘要: 用户询问了退款流程",
        is_new_topic=False,
        reused_last_route=False,
    )

    result = await router.process_message(object(), "顺便问下账单", [], room)

    dispatched_msg = mocks["member_dispatcher"].await_args[0][4]
    assert "上一位处理人 client_svc 的回复摘要" in dispatched_msg
    assert "用户消息：顺便问下账单" in dispatched_msg
    assert result["cross_member_summary_applied"] is True
    assert result["cross_member_previous"] == "client_svc"


@pytest.mark.asyncio
async def test_step4_5_no_summary_passes_message_through_unchanged(
    router, mocks, room,
):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="client_svc",
        reason="best fit",  # no §8 prefix
        is_new_topic=True,
        reused_last_route=False,
    )

    await router.process_message(object(), "hello", [], room)

    dispatched_msg = mocks["member_dispatcher"].await_args[0][4]
    assert dispatched_msg == "hello"


# ---------------------------------------------------------------------------
# §6.1 Step 5 — member dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step5_dispatches_to_correct_session_id(router, mocks, room):
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member="finance",
        reason="fit",
        is_new_topic=True,
        reused_last_route=False,
    )

    await router.process_message(object(), "msg", [], room)

    call = mocks["member_dispatcher"].await_args
    member, session_id, _source, _history, _msg = call[0]
    assert member == "finance"
    assert session_id == "room_member:room_1:finance"


# ---------------------------------------------------------------------------
# §6.3 Fence — three checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fence_at_observer_completion_drops_decision(
    router, mocks, store, room,
):
    """§6.3 checkpoint A: room fenced mid-observer-turn → decision
    discarded, no member dispatched, no reply."""
    # Fence BEFORE observer even starts (simulates a very fast /room unbind)
    store.fence_room(room.room_id, ["room_observer:room_1"])

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "observer"
    assert result["reply"] is None
    mocks["member_dispatcher"].assert_not_awaited()


@pytest.mark.asyncio
async def test_m1_b10_fence_at_member_predispatch(router, mocks, store, room):
    """M1-B10: user runs /room unbind while observer is running. Observer
    completes normally (its session wasn't fenced), but by the time we
    look up the member's session, it IS fenced. Skip dispatch."""
    # Fence the member session BEFORE process_message runs
    store.fence_room(room.room_id, ["room_member:room_1:client_svc"])

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "member_predispatch"
    assert result["target_member"] == "client_svc"  # observer decision preserved for logs
    assert result["reply"] is None
    mocks["member_dispatcher"].assert_not_awaited()


@pytest.mark.asyncio
async def test_fence_at_member_postdispatch_drops_reply(
    router, mocks, store, room,
):
    """§6.3 checkpoint C: member's turn ran to completion, but the room
    was fenced during its execution. Reply is dropped rather than being
    delivered to a now-unbound group."""
    async def dispatch_then_fence(*_args, **_kw):
        # Fence the member session mid-dispatch (simulates concurrent
        # /room delete / member remove).
        store.fence_room(room.room_id, ["room_member:room_1:client_svc"])
        return "member's reply"

    mocks["member_dispatcher"].side_effect = dispatch_then_fence

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "member_postdispatch"
    assert result["reply"] is None


# ---------------------------------------------------------------------------
# M1-B7: concurrent messages get independent routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m1_b7_three_concurrent_messages_route_independently(
    router, mocks, room,
):
    """1-second-3-messages concurrency case: the router must not share
    per-message state on ``self``, so 3 concurrent process_message calls
    each get their own observer turn and dispatch."""
    async def stagger_observer(*args):
        # Simulate a slow-ish observer to force overlap
        await asyncio.sleep(0.05)
        return RoutingDecision(
            target_member="client_svc",
            reason="fit",
            is_new_topic=True,
            reused_last_route=False,
        )

    mocks["observer_runner"].side_effect = stagger_observer

    results = await asyncio.gather(
        router.process_message(object(), "msg1", [], room),
        router.process_message(object(), "msg2", [], room),
        router.process_message(object(), "msg3", [], room),
    )

    assert len(results) == 3
    assert all(r["target_member"] == "client_svc" for r in results)
    assert mocks["observer_runner"].await_count == 3
    assert mocks["member_dispatcher"].await_count == 3


@pytest.mark.asyncio
async def test_m4_b7_new_message_not_blocked_by_inflight_synthesis(
    store, mocks, room,
):
    """M4-B7: while a decompose task is still running its (slow) synthesis
    turn, a NEW simple message that arrives concurrently must route and
    finish independently — it is not queued behind the synthesis. The
    router holds no per-message state on ``self``, so the two flows don't
    serialize."""
    synthesis_started = asyncio.Event()
    release_synthesis = asyncio.Event()

    async def slow_synthesis(observer_profile, session_id, src, rendered):
        # Signal that synthesis is in flight, then block until released.
        synthesis_started.set()
        await release_synthesis.wait()
        return "final synthesized reply"

    async def observer_router(observer_profile, sid, src, hist, msg):
        if "complex" in msg:
            return RoutingDecision(
                target_member="",
                reason="multi-step",
                is_new_topic=True,
                reused_last_route=False,
                action="decompose_and_route",
                decompose_tasks=[
                    {"title": "step1", "body": "", "assignee": "finance", "parents": []},
                ],
            )
        return RoutingDecision(
            target_member="client_svc",
            reason="simple",
            is_new_topic=True,
            reused_last_route=False,
        )

    m = dict(mocks)
    m["observer_runner"] = AsyncMock(side_effect=observer_router)
    m["member_dispatcher"] = AsyncMock(return_value="member reply")
    router = AgentRoomRouter(
        store=store, synthesis_runner=slow_synthesis, **m,
    )

    # Start the decompose flow; it will block inside synthesis.
    decompose_task = asyncio.create_task(
        router.process_message(object(), "complex multi-step request", [], room)
    )
    # Wait until synthesis is actually in flight (decompose is mid-run).
    await asyncio.wait_for(synthesis_started.wait(), timeout=2.0)

    # Now fire a simple message concurrently — it must complete WITHOUT
    # waiting for the blocked synthesis to be released.
    simple_result = await asyncio.wait_for(
        router.process_message(object(), "simple question", [], room),
        timeout=2.0,
    )
    assert simple_result["target_member"] == "client_svc"
    assert simple_result.get("decompose") is not True

    # The decompose flow is still parked in synthesis until we release it.
    assert not decompose_task.done()
    release_synthesis.set()
    decompose_result = await asyncio.wait_for(decompose_task, timeout=2.0)
    assert decompose_result["decompose"] is True


# ---------------------------------------------------------------------------
# M1-B12: aux LLM unavailable → classifier raises → observer path taken
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_m1_b12_classifier_exception_propagates(router, mocks, room):
    """When the aux LLM is down and the classifier itself raises, the
    router intentionally does NOT swallow it — the caller (gateway/run
    _process_message_via_room) should catch it, log a WARN, and fall
    back to 'always run full observer turn' behavior. The router's
    concern is 'don't silently reuse a stale last_routed_member' — that
    is achieved by not returning a fake ClassifierResult from a hidden
    default. The exception surfaces so the caller can decide.

    This matches HTML §11m1 B12: 'Aux LLM 不可用时的 N4 轻量分类 →
    降级为总是跑完整观察者 turn, 不静默沿用 · 记 WARN 日志'."""
    mocks["classifier"].side_effect = RuntimeError("aux LLM connect failed")

    with pytest.raises(RuntimeError, match="aux LLM"):
        await router.process_message(object(), "msg", [], room)


# ---------------------------------------------------------------------------
# clear_last_routed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_last_routed_forces_next_message_to_full_observer(
    router, mocks, room,
):
    await router._set_last_routed(room.room_id, "client_svc")
    await router.clear_last_routed(room.room_id)

    # Even if classifier says "continuation with high confidence", the
    # cleared cache forces a full observer turn.
    mocks["classifier"].return_value = ClassifierResult(
        is_new_topic=False, confidence=0.95,
    )
    await router.process_message(object(), "msg", [], room)

    mocks["observer_runner"].assert_awaited_once()


# ═════════════════════════════════════════════════════════════════════════════
# M3.5 · Concurrent multi-member routing
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_m3_concurrent_dispatch_fans_out(router, mocks, room):
    """Observer returns a list of members → router dispatches to all
    of them concurrently and returns per-member replies."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "finance"],
        reason="split domains",
        is_new_topic=True,
        reused_last_route=False,
    )

    # Track dispatch order to verify concurrency (both start before
    # either finishes)
    import asyncio
    order = []

    async def _capture(member, sid, src, hist, msg):
        order.append(f"start:{member}")
        await asyncio.sleep(0.01)
        order.append(f"done:{member}")
        return f"reply from {member}"

    mocks["member_dispatcher"].side_effect = _capture

    result = await router.process_message(object(), "help me", [], room)

    assert result["concurrent"] is True
    assert result["fenced_at"] is None
    assert set(result["replies"].keys()) == {"client_svc", "finance"}
    assert result["replies"]["client_svc"] == "reply from client_svc"
    assert result["replies"]["finance"] == "reply from finance"
    # Concurrency check: both starts happen before either done
    starts = [o for o in order if o.startswith("start:")]
    dones = [o for o in order if o.startswith("done:")]
    assert len(starts) == 2 and len(dones) == 2
    assert order.index(starts[1]) < order.index(dones[0]), (
        "second dispatch should start before first one completes "
        "(gather-style concurrency)"
    )


# ---------------------------------------------------------------------------
# 2026-08-17 fix: concurrent-dispatch scope prefix.
#
# Root cause found while investigating a "team members coordinating" report
# (real dev_team room, room_197aea4f01b1): when a compound request like
# "后端把X实现一下，前端把Y写一下" is routed concurrently to BOTH
# backend_engineer and frontend_engineer, every member used to receive the
# exact same unmodified member_input — the full sentence literally spelling
# out the teammate's half of the task too. Despite the room-context system
# prompt's broadcast-repeat guardrail, each member's own LLM routinely
# answered BOTH halves and fabricated completion claims for a teammate's
# task it never ran (confirmed live: seq43/44, seq60/61, seq39 — none of
# which contained an @mention token, ruling out the handoff-chain path).
# Fix: give each concurrently-dispatched member an explicit scope note
# naming who else was also routed the same message.
# ---------------------------------------------------------------------------


def test_apply_concurrent_scope_prefix_names_other_members(router):
    out = AgentRoomRouter._apply_concurrent_scope_prefix(
        "后端把X实现一下，前端把Y写一下", "backend_engineer", ["frontend_engineer"],
    )
    assert "frontend_engineer" in out
    assert "只处理属于你自己职责范围的" in out
    # 2026-08-17: the wording explicitly forbids reporting a teammate's
    # completion status ("✅ 后端接口已实现"), which live traffic showed
    # frontend_engineer kept doing even with the milder first wording.
    assert "工作进度" in out or "工作状态" in out
    assert "后端把X实现一下，前端把Y写一下" in out


def test_apply_concurrent_scope_prefix_multiple_others_joined():
    out = AgentRoomRouter._apply_concurrent_scope_prefix(
        "msg", "backend_engineer", ["frontend_engineer", "qa_engineer"],
    )
    assert "frontend_engineer、qa_engineer" in out


def test_apply_concurrent_scope_prefix_noop_when_no_others():
    # Defensive: if somehow called with an empty "others" list (should not
    # happen from the router since is_multi implies >=2 members), the
    # original message passes through unchanged rather than growing a
    # useless prefix.
    assert AgentRoomRouter._apply_concurrent_scope_prefix("msg", "x", []) == "msg"


@pytest.mark.asyncio
async def test_m3_concurrent_dispatch_gives_each_member_a_distinct_scoped_input(
    router, mocks, room,
):
    """Each concurrently-dispatched member must receive an input naming its
    OWN teammates (not itself), not the byte-identical original message."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "finance"],
        reason="split domains",
        is_new_topic=True,
        reused_last_route=False,
    )
    seen: dict[str, str] = {}

    async def _capture(member, sid, src, hist, msg):
        seen[member] = msg
        return f"reply from {member}"

    mocks["member_dispatcher"].side_effect = _capture

    await router.process_message(object(), "后端实现X，前端写Y", [], room)

    assert "finance" in seen["client_svc"]
    assert "client_svc" in seen["finance"]
    # Neither member's own name appears in the "routed to" list of its own input.
    assert "路由提示：这条消息已同时路由给 finance" in seen["client_svc"]
    assert "路由提示：这条消息已同时路由给 client_svc" in seen["finance"]
    # The original message text still reaches both members.
    assert "后端实现X，前端写Y" in seen["client_svc"]
    assert "后端实现X，前端写Y" in seen["finance"]


@pytest.mark.asyncio
async def test_m3_concurrent_dedupes_and_filters_invalid_members(
    router, mocks, room,
):
    """M3 hallucination guard: LLM lists members not in roster → drop them.
    Duplicates are also removed."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "GHOST_PROFILE", "client_svc", "finance"],
        reason="mixed valid/invalid",
        is_new_topic=True,
        reused_last_route=False,
    )
    mocks["member_dispatcher"].return_value = "reply"

    result = await router.process_message(object(), "help", [], room)

    assert set(result["replies"].keys()) == {"client_svc", "finance"}
    # dispatcher called exactly twice — GHOST dropped, dupe deduped
    assert mocks["member_dispatcher"].await_count == 2


@pytest.mark.asyncio
async def test_m3_one_member_failure_does_not_sink_others(router, mocks, room):
    """M3-B4: per-member turn failure is isolated — surviving members
    still deliver replies."""
    call_count = {"n": 0}

    async def _one_fails(member, sid, src, hist, msg):
        if member == "finance":
            raise RuntimeError("simulated finance turn crash")
        return f"ok from {member}"

    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "finance"],
        reason="both needed",
        is_new_topic=True,
        reused_last_route=False,
    )
    mocks["member_dispatcher"].side_effect = _one_fails

    result = await router.process_message(object(), "help", [], room)

    assert result["replies"]["client_svc"] == "ok from client_svc"
    assert "[error:" in result["replies"]["finance"]
    assert "RuntimeError" in result["replies"]["finance"]


@pytest.mark.asyncio
async def test_m3_all_invalid_members_fall_back_to_default(router, mocks, room):
    """LLM proposes multiple invalid members → collapses to single-member
    dispatch on default_member (never sends a ghost message)."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["nobody_1", "nobody_2"],
        reason="hallucinated",
        is_new_topic=True,
        reused_last_route=False,
    )
    mocks["member_dispatcher"].return_value = "reply"

    result = await router.process_message(object(), "help", [], room)

    # Falls back to single-member (default_member = client_svc)
    assert result.get("concurrent") is not True
    assert result["target_member"] == "client_svc"
    assert result["reply"] == "reply"


@pytest.mark.asyncio
async def test_m3_single_valid_member_in_list_degenerates_to_single(
    router, mocks, room,
):
    """LLM proposes [valid, invalid] → after filter only one remains →
    use the single-member path (simpler ack text, single fence check)."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["finance", "GHOST"],
        reason="mostly valid",
        is_new_topic=True,
        reused_last_route=False,
    )
    mocks["member_dispatcher"].return_value = "reply"

    result = await router.process_message(object(), "help", [], room)

    assert result.get("concurrent") is not True
    assert result["target_member"] == "finance"
    assert result["reply"] == "reply"


@pytest.mark.asyncio
async def test_m3_fence_predispatch_drops_all_concurrent(
    router, mocks, store, room,
):
    """Fence at member-predispatch level for a concurrent turn drops
    the whole turn — no partial dispatch."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "finance"],
        reason="both",
        is_new_topic=True,
        reused_last_route=False,
    )
    # Fence one of the two member sessions BEFORE observer returns
    store.fence_room(room.room_id, ["room_member:room_1:finance"])

    result = await router.process_message(object(), "help", [], room)

    assert result["fenced_at"] == "member_predispatch"
    mocks["member_dispatcher"].assert_not_awaited()


@pytest.mark.asyncio
async def test_m3_ack_message_edit_removed(router, mocks, room):
    """Post-live-fix: Step 4 (edit ack to '已并发转交给 X, Y 处理...')
    was removed as UX noise. The concurrent-path replies flow through
    each member's dispatcher call and land as separate messages; the
    ack card doesn't need mid-flight editing. Verify ack_editor is
    NOT called even for the concurrent path."""
    mocks["observer_runner"].return_value = RoutingDecision(
        target_member=["client_svc", "finance"],
        reason="both",
        is_new_topic=True,
        reused_last_route=False,
    )
    mocks["member_dispatcher"].return_value = "reply"

    await router.process_message(object(), "help", [], room)

    mocks["ack_editor"].assert_not_awaited()


# ─── RoutingDecision helpers ─────────────────────────────────────────────

def test_routing_decision_is_multi():
    single = RoutingDecision(
        target_member="alice", reason="", is_new_topic=False, reused_last_route=False,
    )
    multi = RoutingDecision(
        target_member=["alice", "bob"], reason="",
        is_new_topic=False, reused_last_route=False,
    )
    assert single.is_multi is False
    assert multi.is_multi is True


def test_routing_decision_target_members_normalized():
    assert RoutingDecision(
        target_member="alice", reason="", is_new_topic=False, reused_last_route=False,
    ).target_members == ["alice"]
    assert RoutingDecision(
        target_member=["a", "b"], reason="", is_new_topic=False, reused_last_route=False,
    ).target_members == ["a", "b"]
    assert RoutingDecision(
        target_member="", reason="", is_new_topic=False, reused_last_route=False,
    ).target_members == []


# ---------------------------------------------------------------------------
# M4.4 — decompose_and_route orchestration + synthesis
# ---------------------------------------------------------------------------


def _decompose_decision(tasks, reason="multi-step", is_new_topic=True):
    return RoutingDecision(
        target_member="",
        reason=reason,
        is_new_topic=is_new_topic,
        reused_last_route=False,
        action="decompose_and_route",
        decompose_tasks=tasks,
    )


def test_routing_decision_is_decompose_flag():
    d = _decompose_decision([{"title": "x", "assignee": "client_svc"}])
    assert d.is_decompose is True
    plain = RoutingDecision(
        target_member="client_svc", reason="", is_new_topic=False, reused_last_route=False,
    )
    assert plain.is_decompose is False


@pytest.mark.asyncio
async def test_m4_decompose_runs_orchestrator_and_synthesis(store, mocks, room):
    """Observer emits a 2-node DAG; both members dispatched, synthesis
    runner composes the final reply."""
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "起草合同", "assignee": "client_svc", "parents": []},
        {"title": "算成本", "assignee": "finance", "parents": [0]},
    ])
    synthesis = AsyncMock(return_value="最终综合回复")
    router = AgentRoomRouter(store=store, synthesis_runner=synthesis, **mocks)

    result = await router.process_message(
        source=object(), message="帮我起草合同并算成本", history=[], room=room,
    )

    assert result["decompose"] is True
    assert result["reply"] == "最终综合回复"
    # Both subtasks dispatched (member_dispatcher called twice).
    assert mocks["member_dispatcher"].await_count == 2
    # Synthesis called once with the observer session id.
    synthesis.assert_awaited_once()
    assert synthesis.await_args.args[1] == AgentRoomRouter.observer_session_id(room.room_id)
    orch = result["orchestration"]
    assert orch["total_subtasks"] == 2
    assert orch["completed"] == 2


@pytest.mark.asyncio
async def test_m4_decompose_without_synthesis_runner_concatenates(store, mocks, room):
    """No synthesis_runner wired → falls back to plaintext concatenation."""
    mocks["member_dispatcher"] = AsyncMock(side_effect=["合同草稿", "成本 100 元"])
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "起草合同", "assignee": "client_svc"},
        {"title": "算成本", "assignee": "finance"},
    ])
    router = AgentRoomRouter(store=store, synthesis_runner=None, **mocks)

    result = await router.process_message(
        source=object(), message="x", history=[], room=room,
    )
    assert result["decompose"] is True
    assert "合同草稿" in result["reply"]
    assert "成本 100 元" in result["reply"]


@pytest.mark.asyncio
async def test_m4_decompose_dependent_skipped_when_parent_fails(store, mocks, room):
    """Parent subtask fails → dependent child skipped, surfaced in synthesis."""
    async def _dispatch(member, sid, source, hist, msg):
        if member == "client_svc":
            raise RuntimeError("boom")
        return "ok"
    mocks["member_dispatcher"] = AsyncMock(side_effect=_dispatch)
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "parent", "assignee": "client_svc"},
        {"title": "child", "assignee": "finance", "parents": [0]},
    ])
    captured = {}
    async def _synth(profile, sid, source, rendered):
        captured["rendered"] = rendered
        return "done"
    router = AgentRoomRouter(store=store, synthesis_runner=_synth, **mocks)

    result = await router.process_message(
        source=object(), message="x", history=[], room=room,
    )
    orch = result["orchestration"]
    assert orch["failed"] == 1
    assert orch["skipped"] == 1
    # child never dispatched (only the parent attempt happened).
    assert mocks["member_dispatcher"].await_count == 1
    assert "skipped" in captured["rendered"]


@pytest.mark.asyncio
async def test_m4_decompose_unknown_assignee_rewritten_to_default(store, mocks, room):
    """Assignee not in roster → build_subtasks rewrites to default_member."""
    dispatched = []
    async def _dispatch(member, sid, source, hist, msg):
        dispatched.append(member)
        return "ok"
    mocks["member_dispatcher"] = AsyncMock(side_effect=_dispatch)
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "t", "assignee": "nonexistent_member"},
    ])
    router = AgentRoomRouter(
        store=store, synthesis_runner=AsyncMock(return_value="ok"), **mocks
    )
    await router.process_message(source=object(), message="x", history=[], room=room)
    # default_member is client_svc
    assert dispatched == ["client_svc"]


@pytest.mark.asyncio
async def test_m4_decompose_empty_tasks_returns_no_reply(store, mocks, room):
    mocks["observer_runner"].return_value = _decompose_decision([])
    router = AgentRoomRouter(
        store=store, synthesis_runner=AsyncMock(return_value="x"), **mocks
    )
    result = await router.process_message(
        source=object(), message="x", history=[], room=room,
    )
    assert result["decompose"] is True
    assert result["reply"] is None
    mocks["member_dispatcher"].assert_not_awaited()


@pytest.mark.asyncio
async def test_m4_decompose_synthesis_failure_falls_back_to_concat(store, mocks, room):
    mocks["member_dispatcher"] = AsyncMock(return_value="子任务结果")
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "t", "assignee": "client_svc"},
    ])
    synthesis = AsyncMock(side_effect=RuntimeError("synth boom"))
    router = AgentRoomRouter(store=store, synthesis_runner=synthesis, **mocks)
    result = await router.process_message(
        source=object(), message="x", history=[], room=room,
    )
    # Synthesis raised → concat fallback used.
    assert "子任务结果" in result["reply"]


@pytest.mark.asyncio
async def test_m4_decompose_fence_post_orchestration_drops_synthesis(store, mocks, room):
    mocks["observer_runner"].return_value = _decompose_decision([
        {"title": "t", "assignee": "client_svc"},
    ])
    observer_sid = AgentRoomRouter.observer_session_id(room.room_id)

    # Fence the observer session AFTER orchestration (during member dispatch).
    async def _dispatch(member, sid, source, hist, msg):
        store.fence_room(room.room_id, [observer_sid])
        return "ok"
    mocks["member_dispatcher"] = AsyncMock(side_effect=_dispatch)
    synthesis = AsyncMock(return_value="should not appear")
    router = AgentRoomRouter(store=store, synthesis_runner=synthesis, **mocks)

    result = await router.process_message(
        source=object(), message="x", history=[], room=room,
    )
    assert result["fenced_at"] == "observer_postorchestration"
    assert result["reply"] is None
    synthesis.assert_not_awaited()


# ---------------------------------------------------------------------------
# Hybrid: member-to-member @mention handoff chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_chain_follows_single_mention(store, room):
    """First member's reply @mentions another member → that member is
    dispatched a second time as a handoff hop."""
    replies = {
        "client_svc": "我先看下，剩下的 @finance 帮忙核算",
        "finance": "成本是 500 元",
    }

    async def _dispatch(member, sid, src, hist, msg):
        return replies[member]

    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(return_value="h"),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        observer_runner=AsyncMock(
            return_value=RoutingDecision(
                target_member="client_svc", reason="start",
                is_new_topic=True, reused_last_route=False,
            )
        ),
        member_dispatcher=AsyncMock(side_effect=_dispatch),
    )
    result = await router.process_message(object(), "帮我算成本", [], room)

    assert result["target_member"] == "client_svc"
    hops = result["handoffs"]
    assert len(hops) == 1
    assert hops[0]["member"] == "finance"
    assert hops[0]["from"] == "client_svc"
    assert hops[0]["depth"] == 1
    assert "500" in hops[0]["reply"]


@pytest.mark.asyncio
async def test_handoff_chain_stops_when_no_mention(store, room):
    async def _dispatch(member, sid, src, hist, msg):
        return "直接回答，没有 @ 任何人"

    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(return_value="h"),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        observer_runner=AsyncMock(
            return_value=RoutingDecision(
                target_member="client_svc", reason="start",
                is_new_topic=True, reused_last_route=False,
            )
        ),
        member_dispatcher=AsyncMock(side_effect=_dispatch),
    )
    result = await router.process_message(object(), "简单问题", [], room)
    assert result["handoffs"] == []


@pytest.mark.asyncio
async def test_handoff_chain_bounded_by_depth(store):
    """A ping-pong between two members is capped by the handoff policy so
    it cannot loop forever."""
    room = store.create_room(
        "room_pp", "PingPong",
        observer_profile="obs",
        members=["a", "b"],
        default_member="a",
    )
    # a always @b, b always @a → infinite without the depth cap.
    def _reply(member):
        other = "b" if member == "a" else "a"
        return f"继续 @{other}"

    async def _dispatch(member, sid, src, hist, msg):
        return _reply(member)

    from gateway.agent_room_handoff import resolve_handoff_policy
    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(return_value="h"),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        observer_runner=AsyncMock(
            return_value=RoutingDecision(
                target_member="a", reason="start",
                is_new_topic=True, reused_last_route=False,
            )
        ),
        member_dispatcher=AsyncMock(side_effect=_dispatch),
        handoff_policy=resolve_handoff_policy(max_depth=3),
    )
    result = await router.process_message(object(), "start", [], room)
    # depth cap 3 → hops at depth 1,2,3 then stop = 3 hops.
    hops = result["handoffs"]
    assert [h["depth"] for h in hops] == [1, 2, 3]


@pytest.mark.asyncio
async def test_handoff_skips_fenced_target(store, room):
    async def _dispatch(member, sid, src, hist, msg):
        return "让 @finance 接手" if member == "client_svc" else "不该被调用"

    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(return_value="h"),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        observer_runner=AsyncMock(
            return_value=RoutingDecision(
                target_member="client_svc", reason="start",
                is_new_topic=True, reused_last_route=False,
            )
        ),
        member_dispatcher=AsyncMock(side_effect=_dispatch),
    )
    # Fence the finance member session so its handoff hop is skipped.
    store.fence_room(room.room_id, [AgentRoomRouter.member_session_id(room.room_id, "finance")])
    result = await router.process_message(object(), "帮我算成本", [], room)
    assert result["handoffs"] == []


# ---------------------------------------------------------------------------
# First-hop classifier path (replaces the observer agent turn)
# ---------------------------------------------------------------------------


def _base_mocks_no_observer():
    return {
        "ack_sender": AsyncMock(return_value="h"),
        "ack_editor": AsyncMock(),
        "classifier": AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        "member_dispatcher": AsyncMock(return_value="member reply"),
    }


@pytest.mark.asyncio
async def test_first_hop_classifier_single_member(store, room):
    """When no @mention, the injected first_hop_runner decides the target
    and the observer_runner is never called."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["finance"], matched=True, reason="账单")
    )
    observer = AsyncMock()
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop, observer_runner=observer,
        **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "账单不对", [], room)
    assert result["target_member"] == "finance"
    first_hop.assert_awaited_once()
    observer.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_hop_classifier_multi_member(store, room):
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["client_svc", "finance"], matched=True)
    )
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop, **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "退款+投诉", [], room)
    assert result.get("concurrent") is True
    assert set(result["target_member"]) == {"client_svc", "finance"}


@pytest.mark.asyncio
async def test_first_hop_deterministic_mention_takes_priority(store, room):
    """An explicit @mention bypasses the classifier entirely."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["client_svc"], matched=True)
    )
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop, **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "@finance 帮我看看", [], room)
    assert result["target_member"] == "finance"
    # classifier must NOT run when the user @mentioned someone.
    first_hop.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_hop_invalid_member_falls_back_to_default(store, room):
    """If the classifier returns a name not in the roster, the router
    falls back to the room default — and this is also a no-match
    (a hallucinated/non-roster name carries no real domain judgment)."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["nonexistent"], matched=True)
    )
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop, **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "随便说点啥", [], room)
    assert result["target_member"] == "client_svc"  # room default


@pytest.mark.asyncio
async def test_first_hop_fenced_mid_classify_drops(store, room):
    async def _fence_then_return(msg, room_arg):
        store.fence_room(
            room.room_id,
            [AgentRoomRouter.observer_session_id(room.room_id)],
        )
        return FirstHopResult(members=["finance"], matched=True)

    router = AgentRoomRouter(
        store=store, first_hop_runner=AsyncMock(side_effect=_fence_then_return),
        **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "账单", [], room)
    assert result["fenced_at"] == "observer"
    assert result["reply"] is None


@pytest.mark.asyncio
async def test_observer_still_used_when_no_first_hop_runner(store, room):
    """Back-compat: callers that inject only observer_runner keep the
    legacy observer path."""
    observer = AsyncMock(return_value=RoutingDecision(
        target_member="finance", reason="legacy",
        is_new_topic=True, reused_last_route=False,
    ))
    router = AgentRoomRouter(
        store=store, observer_runner=observer, **_base_mocks_no_observer(),
    )
    result = await router.process_message(object(), "账单不对", [], room)
    assert result["target_member"] == "finance"
    observer.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2026-08-14 no-match escalation ("不合理吧" research thread)
#
# Design: hermes-studio has no auto-routing AI layer at all (deterministic
# @mention only, empty match → nobody replies); AutoGen separates a Group
# Chat Manager role from participant agents; the OpenAI Agents SDK's
# Triage Agent can own the answer itself instead of always handing off.
# None of them silently force a roster member to answer as if it were a
# real domain match. This room design still must always produce *some*
# reply (no coordinator role exists to hand off to — that would be a
# bigger, unrequested data-model change) but the forced fallback member
# must be told explicitly that it's a fallback, not a real match, so it
# can use its own judgment instead of reflexively deflecting. See
# scratchpad/coordinator_demo2.py for the empirical A/B validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_hop_explicit_no_match_flags_decision_and_prefixes_message(store, room):
    """A real "this belongs to nobody" verdict (matched=False, non-empty
    fallback members) must set RoutingDecision.is_no_match=True and the
    member actually dispatched must receive the no-match framing prefix,
    not the user's raw message unchanged."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(
            members=["client_svc"], matched=False,
            reason="这是决策问题，不属于任何成员职责范围",
        )
    )
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        ack_sender=AsyncMock(return_value="h"),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        member_dispatcher=dispatcher,
    )
    result = await router.process_message(object(), "该先上线前端还是先修后端bug？", [], room)
    assert result["target_member"] == "client_svc"
    assert result["is_no_match"] is True
    # dispatcher must have received a prefixed message, not the raw text.
    dispatched_input = dispatcher.await_args.args[-1]
    assert "兜底" in dispatched_input
    assert "该先上线前端还是先修后端bug？" in dispatched_input


@pytest.mark.asyncio
async def test_first_hop_real_match_does_not_get_no_match_prefix(store, room):
    """A real domain match must NOT be prefixed with the no-match framing
    — that framing is only for forced fallbacks."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["finance"], matched=True, reason="账单")
    )
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        **{**_base_mocks_no_observer(), "member_dispatcher": dispatcher},
    )
    result = await router.process_message(object(), "账单不对", [], room)
    assert result["is_no_match"] is False
    dispatched_input = dispatcher.await_args.args[-1]
    assert dispatched_input == "账单不对"


@pytest.mark.asyncio
async def test_first_hop_hallucinated_names_treated_as_no_match(store, room):
    """Even if ``matched=True`` was somehow set incorrectly upstream, the
    router's own is_no_match determination is defense-in-depth: if
    nothing survives roster validation, it's a no-match regardless of
    what the classifier claimed."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["totally_not_a_member"], matched=True)
    )
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        **{**_base_mocks_no_observer(), "member_dispatcher": dispatcher},
    )
    result = await router.process_message(object(), "随便说点啥", [], room)
    assert result["target_member"] == "client_svc"
    dispatched_input = dispatcher.await_args.args[-1]
    assert "兜底" in dispatched_input


@pytest.mark.asyncio
async def test_first_hop_plain_list_return_still_works_back_compat(store, room):
    """If a caller's first_hop_runner still returns a bare list[str]
    (pre-FirstHopResult contract), the router must not crash — it treats
    that as a real match (no ``matched`` attribute to introspect) so
    existing integrations degrade gracefully rather than exploding."""
    first_hop = AsyncMock(return_value=["finance"])
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        **{**_base_mocks_no_observer(), "member_dispatcher": dispatcher},
    )
    result = await router.process_message(object(), "账单不对", [], room)
    assert result["target_member"] == "finance"
    dispatched_input = dispatcher.await_args.args[-1]
    assert dispatched_input == "账单不对"


# ---------------------------------------------------------------------------
# Raft AX 改造 1 · held-draft — fence hit HOLDS instead of silently dropping
# ---------------------------------------------------------------------------


@pytest.fixture
def held_store(tmp_path):
    from gateway.agent_room_held_store import AgentRoomHeldStore
    s = AgentRoomHeldStore(db_path=tmp_path / "held.sqlite")
    yield s
    s.close()


@pytest.mark.asyncio
async def test_held_single_member_postdispatch_fence_holds_reply(
    mocks, store, room, held_store,
):
    """§6.3 checkpoint C with a held_store wired: the member's finished
    reply is HELD (durable + recoverable), not silently dropped."""
    async def dispatch_then_fence(*_args, **_kw):
        store.fence_room(room.room_id, ["room_member:room_1:client_svc"])
        return "finance's finished analysis"

    mocks["member_dispatcher"].side_effect = dispatch_then_fence
    router = AgentRoomRouter(
        store=store, held_store=held_store,
        room_version_provider=lambda _rid: 42,
        **mocks,
    )

    class _Src:
        chat_id = "chat-xyz"

    result = await router.process_message(_Src(), "msg", [], room)

    assert result["fenced_at"] == "member_postdispatch"
    assert result["reply"] is None
    # The reply is now HELD, not gone.
    held_id = result["held_id"]
    assert held_id is not None
    held = held_store.get(held_id)
    assert held is not None
    assert held.payload == "finance's finished analysis"
    assert held.member == "client_svc"
    assert held.room_version == 42
    assert held.chat_id == "chat-xyz"
    assert held.status == "held"


@pytest.mark.asyncio
async def test_held_no_store_preserves_legacy_silent_drop(
    mocks, store, room,
):
    """Without a held_store wired, the fence behavior is byte-for-byte the
    legacy silent drop — zero-risk additive change."""
    async def dispatch_then_fence(*_args, **_kw):
        store.fence_room(room.room_id, ["room_member:room_1:client_svc"])
        return "would-be-dropped reply"

    mocks["member_dispatcher"].side_effect = dispatch_then_fence
    router = AgentRoomRouter(store=store, **mocks)  # NO held_store

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "member_postdispatch"
    assert result["reply"] is None
    # held_id is present but None — nothing was held.
    assert result.get("held_id") is None


@pytest.mark.asyncio
async def test_held_empty_reply_is_not_held(mocks, store, room, held_store):
    """An empty member reply on a fence is a genuine stay-silent, not a lost
    artifact — it must NOT create a held row."""
    async def dispatch_then_fence(*_args, **_kw):
        store.fence_room(room.room_id, ["room_member:room_1:client_svc"])
        return ""  # member chose to say nothing

    mocks["member_dispatcher"].side_effect = dispatch_then_fence
    router = AgentRoomRouter(
        store=store, held_store=held_store,
        room_version_provider=lambda _rid: 1, **mocks,
    )

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "member_postdispatch"
    assert result["held_id"] is None
    assert held_store.list_held(room.room_id) == []


@pytest.mark.asyncio
async def test_held_multi_member_postdispatch_fence_holds_each_reply(
    mocks, store, room, held_store,
):
    """Concurrent fan-out fenced mid-flight: each member's reply is held
    (keyed by member) instead of the whole batch vanishing."""
    call_state = {"first": True}

    async def dispatch(member, sid, src, hist, msg):
        # Fence only once the first member has produced its reply so the
        # post-dispatch gate trips for the whole batch.
        if call_state["first"]:
            call_state["first"] = False
        store.fence_room(room.room_id, [sid])
        return f"{member}'s reply"

    mocks["member_dispatcher"].side_effect = dispatch
    # Force a multi-member decision via @mention of both members.
    router = AgentRoomRouter(
        store=store, held_store=held_store,
        room_version_provider=lambda _rid: 9, **mocks,
    )

    result = await router.process_message(
        object(), "@client_svc @finance 大家看看", [], room,
    )

    assert result["fenced_at"] == "member_postdispatch"
    assert result["reply"] is None
    held_ids = result["held_ids"]
    # Both members' replies held.
    assert set(held_ids.keys()) == {"client_svc", "finance"}
    payloads = {
        held_store.get(hid).member: held_store.get(hid).payload
        for hid in held_ids.values() if hid is not None
    }
    assert payloads["client_svc"] == "client_svc's reply"
    assert payloads["finance"] == "finance's reply"
    for hid in held_ids.values():
        assert held_store.get(hid).room_version == 9


@pytest.mark.asyncio
async def test_held_store_failure_degrades_to_drop(mocks, store, room):
    """If the held_store.hold() itself raises, routing must NOT crash —
    it degrades to the legacy drop with held_id=None."""
    class _BoomStore:
        def hold(self, *a, **k):
            raise RuntimeError("disk full")

    async def dispatch_then_fence(*_args, **_kw):
        store.fence_room(room.room_id, ["room_member:room_1:client_svc"])
        return "reply we could not persist"

    mocks["member_dispatcher"].side_effect = dispatch_then_fence
    router = AgentRoomRouter(
        store=store, held_store=_BoomStore(),
        room_version_provider=lambda _rid: 1, **mocks,
    )

    result = await router.process_message(object(), "msg", [], room)

    assert result["fenced_at"] == "member_postdispatch"
    assert result["reply"] is None
    assert result["held_id"] is None  # persistence failed → legacy drop


# ---------------------------------------------------------------------------
# Raft AX 改造 2 · no_match_policy="silent" (Silence is a valid outcome)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_match_silent_policy_dispatches_nobody(store, room):
    """Under silent policy, a forced no-match fallback stays silent —
    NO member is dispatched, and the bundle marks stayed_silent."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(
            members=["client_svc"], matched=False,
            reason="不属于任何成员职责范围",
        )
    )
    dispatcher = AsyncMock(return_value="should not be called")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        ack_sender=AsyncMock(return_value="h"), ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        member_dispatcher=dispatcher,
        no_match_policy="silent",
    )
    result = await router.process_message(object(), "该先上线前端还是先修后端？", [], room)
    assert result["stayed_silent"] is True
    assert result["is_no_match"] is True
    assert result["target_member"] is None
    assert result["reply"] is None
    dispatcher.assert_not_awaited()  # nobody ran


@pytest.mark.asyncio
async def test_no_match_fallback_policy_still_dispatches(store, room):
    """Default policy (fallback) is unchanged: a no-match still dispatches
    to the default member with the no-match prefix."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(
            members=["client_svc"], matched=False, reason="out of scope",
        )
    )
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        ack_sender=AsyncMock(return_value="h"), ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        member_dispatcher=dispatcher,
        # no_match_policy defaults to "fallback"
    )
    result = await router.process_message(object(), "随便问", [], room)
    assert result.get("stayed_silent") is None
    assert result["target_member"] == "client_svc"
    dispatcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_silent_policy_does_not_silence_real_match(store, room):
    """Silent policy only silences NO-MATCH. A real domain match under the
    silent policy is dispatched normally — silence is not a gag order."""
    first_hop = AsyncMock(
        return_value=FirstHopResult(members=["finance"], matched=True, reason="账单")
    )
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=first_hop,
        ack_sender=AsyncMock(return_value="h"), ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        member_dispatcher=dispatcher,
        no_match_policy="silent",
    )
    result = await router.process_message(object(), "账单不对", [], room)
    assert result.get("stayed_silent") is None
    assert result["target_member"] == "finance"
    dispatcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_silent_policy_does_not_silence_mention(store, room):
    """An explicit @mention is user intent — never silenced even under
    silent policy (mentions never set is_no_match)."""
    dispatcher = AsyncMock(return_value="ok")
    router = AgentRoomRouter(
        store=store, first_hop_runner=AsyncMock(),
        ack_sender=AsyncMock(return_value="h"), ack_editor=AsyncMock(),
        classifier=AsyncMock(
            return_value=ClassifierResult(is_new_topic=True, confidence=1.0)
        ),
        member_dispatcher=dispatcher,
        no_match_policy="silent",
    )
    result = await router.process_message(object(), "@finance 账单", [], room)
    assert result.get("stayed_silent") is None
    assert "finance" in (result["target_member"] or "")
    dispatcher.assert_awaited_once()


def test_unknown_no_match_policy_degrades_to_fallback(store, mocks):
    """An unknown policy value must never accidentally go silent."""
    router = AgentRoomRouter(store=store, no_match_policy="banana", **mocks)
    assert router._no_match_policy == "fallback"
    router2 = AgentRoomRouter(store=store, no_match_policy="SILENT", **mocks)
    assert router2._no_match_policy == "silent"  # case-insensitive
