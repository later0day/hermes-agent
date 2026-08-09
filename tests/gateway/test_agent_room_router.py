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
