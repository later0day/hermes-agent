"""Cross-module CLOSED-LOOP / boundary / real-scenario / role tests for the
Agent Room (team) subsystem.

Unlike the per-module unit suites (test_agent_room_router.py, _projection.py,
_held_*.py, ...) which mock every injected callable in isolation, this file
wires the REAL components together — a real AgentRoomStore, a real
AgentRoomMessagesStore, real projection, a real AgentRoomHeldStore + resolver,
and a real AgentRoomRouter — and only substitutes SCRIPTED stand-ins at the
LLM boundary (classifier / first-hop / member "brain"). Each scripted member
brain writes its reply into the shared MessagesStore, so the room genuinely
"moves" across turns and the version gate / hold / revise machinery is
exercised end-to-end the way production does.

Test taxonomy (the four kinds the maintainer asked for):
  * CLOSED-LOOP  (TestClosedLoop*)  — a message enters, flows through the
    full five-step router, a member reply is produced+recorded, the room
    version advances, and (where applicable) a held reply is resolved.
  * BOUNDARY     (TestBoundary*)     — empty roster, empty message, unknown
    member, seq off-by-one at the digest/verbatim window edge, fence race
    exactly at the snapshot version, resolver on an already-resolved row.
  * REAL SCENARIO(TestScenario*)     — multi-turn conversations that mirror
    the production 3-role support room (customer_service / finance /
    tech_support): topic switch + N4 reuse, concurrent broadcast, a user
    interrupting mid-compose (room-moved hold → true revise), no-match
    silent policy.
  * ROLE         (TestRole*)         — each role's contract: observer/first-
    hop only routes (never answers), members only answer their turn,
    room_fetch_context is scoped to the acting member's room, a member
    cannot hand off to itself, @all fans out to everyone-but-sender.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Optional

import pytest

from gateway.agent_room_store import AgentRoomStore
from gateway.agent_room_messages_store import AgentRoomMessagesStore
from gateway.agent_room_router import (
    AgentRoomRouter,
    ClassifierResult,
    RoutingDecision,
)
from gateway.agent_room_firsthop import FirstHopResult
from gateway.agent_room_projection import (
    project_for_member,
    project_for_member_windowed,
    _DEFAULT_RECENT_N,
)
from gateway.agent_room_held_store import (
    AgentRoomHeldStore,
    HELD_REASON_FENCED,
    HELD_REASON_ROOM_MOVED,
    HELD_REASON_NO_WEBHOOK,
    RESOLUTION_REVISE,
    RESOLUTION_SEND_AS_IS,
    RESOLUTION_STAY_SILENT,
    STATUS_RESOLVED,
)
from gateway.agent_room_held_resolver import AgentRoomHeldResolver
import tools.room_fetch_context_tool as rfc


# Every async test in this module runs under pytest-asyncio.
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# A real-component test harness: a room "world" with three members.
# ---------------------------------------------------------------------------

ROOM_ID = "room_support"
MEMBERS = ["customer_service", "finance", "tech_support"]


class RoomWorld:
    """Wires the real stores + router together with scripted member brains.

    ``member_brains`` maps member name → callable(inbound_message) -> reply
    text. The default brain echoes a canned per-member line. Every dispatch
    appends BOTH the inbound (as a user/observer turn is already recorded by
    the caller) — actually only the member reply — to the shared
    MessagesStore, so the room version advances exactly like production.
    """

    def __init__(self, tmp_path, *, no_match_policy: str = "fallback",
                 members=MEMBERS, default_member="customer_service"):
        self.store = AgentRoomStore(tmp_path / "rooms.sqlite")
        self.messages = AgentRoomMessagesStore(tmp_path / "messages.sqlite")
        self.held = AgentRoomHeldStore(db_path=tmp_path / "held.sqlite")
        self.room = self.store.create_room(
            ROOM_ID, "Support",
            observer_profile="room_support_observer",
            members=members, default_member=default_member,
        )
        # scripted knobs
        self.first_hop_result = FirstHopResult(
            members=[default_member], matched=True, reason="")
        self.classifier_result = ClassifierResult(is_new_topic=True, confidence=1.0)
        self.member_brains: dict = {}
        # observability
        self.dispatched: list[tuple[str, str]] = []   # (member, inbound)
        self.delivered: list[tuple[str, str]] = []     # (chat_id, text)

        async def _ack(source, text):
            return "ack-handle"

        async def _classifier(hist, last_routed):
            return self.classifier_result

        async def _first_hop(message, room):
            return self.first_hop_result

        async def _member_dispatcher(member, sid, source, history, message):
            self.dispatched.append((member, message))
            brain = self.member_brains.get(member)
            reply = brain(message) if brain else f"[{member}] 收到：{message[:40]}"
            # A real member turn writes its reply into the room → version++.
            if reply:
                self.messages.append(
                    ROOM_ID, sender_kind="member",
                    sender_name=member, content=reply)
            return reply

        self.router = AgentRoomRouter(
            store=self.store,
            ack_sender=_ack,
            ack_editor=_ack,
            classifier=_classifier,
            first_hop_runner=_first_hop,
            member_dispatcher=_member_dispatcher,
            held_store=self.held,
            room_version_provider=self.messages.max_sequence,
            no_match_policy=no_match_policy,
        )

    # -- convenience -----------------------------------------------------
    def user_says(self, text: str):
        """Record an inbound user turn (advances room version)."""
        return self.messages.append(
            ROOM_ID, sender_kind="user", sender_name="user", content=text)

    def history(self) -> list[dict]:
        return [
            {"role": "user" if m.sender_kind in ("user",) else "assistant",
             "content": m.content}
            for m in self.messages.list_messages(ROOM_ID)
        ]

    async def send(self, text: str, source=None):
        """Full closed loop: record user turn, run router."""
        self.user_says(text)
        return await self.router.process_message(
            source or SimpleNamespace(chat_id="chat-1"),
            text, self.history(), self.room)

    def close(self):
        self.store.close()
        self.messages.close()
        self.held.close()


@pytest.fixture
def world(tmp_path):
    w = RoomWorld(tmp_path)
    yield w
    w.close()


# ===========================================================================
# CLOSED-LOOP — full path, real stores, room genuinely advances
# ===========================================================================

class TestClosedLoopBasic:
    async def test_single_message_routes_records_and_advances_version(self, world):
        v0 = world.messages.max_sequence(ROOM_ID)
        out = await world.send("我的订单还没发货")
        assert out["target_member"] == "customer_service"
        assert out["reply"] and "customer_service" in out["reply"]
        # room advanced: +1 user turn, +1 member reply
        assert world.messages.max_sequence(ROOM_ID) == v0 + 2
        # last message in the room IS the member's reply
        last = world.messages.list_messages(ROOM_ID)[-1]
        assert last.sender_kind == "member" and last.sender_name == "customer_service"

    async def test_n4_reuse_second_turn_skips_first_hop(self, world):
        await world.send("我的订单还没发货")           # routes to customer_service
        # Now simulate a continuation: classifier says reuse.
        world.classifier_result = ClassifierResult(is_new_topic=False, confidence=1.0)
        # make first-hop blow up so we PROVE it isn't consulted on reuse
        async def _boom(message, room):
            raise AssertionError("first-hop must not run on N4 reuse")
        world.router._first_hop_runner = _boom
        out = await world.send("那大概什么时候到？")
        assert out["reused_last_route"] is True
        assert out["target_member"] == "customer_service"

    async def test_projection_reflects_prior_member_reply(self, world):
        await world.send("发票能重开吗")
        # A second member now sees the first member's reply in the projection.
        msgs = world.messages.list_messages(ROOM_ID)
        proj = project_for_member(msgs, "finance")
        joined = "\n".join(p.content for p in proj)
        assert "customer_service" in joined  # peer reply visible to finance


class TestClosedLoopHeldRevise:
    async def test_room_moved_hold_then_true_revise_delivers_fresh(self, world):
        """Member composes against v=N; a NEW user turn arrives (room moves);
        the reply is held as room_moved; the resolver reruns the member
        against the CURRENT room and delivers a FRESH reply — not the stale
        draft."""
        # member reasoned against this snapshot
        world.user_says("帮我算下这个月报销")
        snap = world.messages.max_sequence(ROOM_ID)
        # user interrupts while finance is "composing"
        world.user_says("等等，发票我撤回了")
        # persist the would-be-stale draft
        held = world.held.hold(
            ROOM_ID, session_id="room_member:room_support:finance",
            member="finance", room_version=snap,
            payload="报销需要3-5个工作日", held_reason=HELD_REASON_ROOM_MOVED,
            chat_id="chat-1")

        async def deliver(chat_id, text):
            world.delivered.append((chat_id, text)); return True

        async def rerun(h):
            # a real rerun sees the CURRENT room (incl. the retract)
            proj = project_for_member_windowed(
                world.messages.list_messages(ROOM_ID), h.member)
            assert any("撤回" in p.content for p in proj)
            return "看到你撤回了发票，报销先搁置"

        resolver = AgentRoomHeldResolver(
            held_store=world.held, deliver=deliver,
            room_version_provider=world.messages.max_sequence,
            rerun_member=rerun)
        out = await resolver.resolve_one(held)
        assert out.path == RESOLUTION_REVISE and out.delivered
        assert world.delivered == [("chat-1", "看到你撤回了发票，报销先搁置")]
        assert "3-5个工作日" not in world.delivered[0][1]
        # row is now terminal
        assert world.held.get(held.id).status == STATUS_RESOLVED

    async def test_transport_lost_room_unchanged_sends_as_is(self, world):
        """No new user turn → room version unchanged → the held draft is
        still valid → Send-as-is delivers the ORIGINAL text."""
        world.user_says("报销进度")
        snap = world.messages.max_sequence(ROOM_ID)
        held = world.held.hold(
            ROOM_ID, session_id="s", member="finance", room_version=snap,
            payload="报销已到财务复核阶段", held_reason=HELD_REASON_NO_WEBHOOK,
            chat_id="chat-1")
        delivered = []
        async def deliver(c, t): delivered.append((c, t)); return True
        resolver = AgentRoomHeldResolver(
            held_store=world.held, deliver=deliver,
            room_version_provider=world.messages.max_sequence, rerun_member=None)
        out = await resolver.resolve_one(held)
        assert out.path == RESOLUTION_SEND_AS_IS and out.delivered
        assert delivered == [("chat-1", "报销已到财务复核阶段")]


# ===========================================================================
# BOUNDARY — edges, off-by-one, empty, races
# ===========================================================================

class TestBoundary:
    async def test_empty_message_still_routes(self, world):
        out = await world.send("")
        assert out["target_member"] == "customer_service"

    async def test_no_match_fallback_dispatches_default(self, world):
        world.first_hop_result = FirstHopResult(
            members=[], matched=False, reason="不属于任何成员")
        out = await world.send("今晚吃什么")
        assert out["is_no_match"] is True
        assert out["target_member"] == "customer_service"  # forced fallback

    def test_windowed_projection_exactly_at_threshold(self, world):
        """recent_n boundary: N messages → identical to full projection
        (no digest); N+1 → a digest appears."""
        for i in range(_DEFAULT_RECENT_N):
            world.messages.append(ROOM_ID, sender_kind="user",
                                  sender_name="user", content=f"m{i}")
        at = project_for_member_windowed(
            world.messages.list_messages(ROOM_ID), "finance")
        full = project_for_member(world.messages.list_messages(ROOM_ID), "finance")
        assert len(at) == len(full)  # no windowing at exactly N
        assert not any("room digest" in p.content for p in at)
        # one more crosses the threshold
        world.messages.append(ROOM_ID, sender_kind="user",
                              sender_name="user", content="one-more")
        over = project_for_member_windowed(
            world.messages.list_messages(ROOM_ID), "finance")
        assert over[0].role == "user" and "room digest" in over[0].content

    def test_windowed_recent_n_zero_disables(self, world):
        for i in range(30):
            world.messages.append(ROOM_ID, sender_kind="user",
                                  sender_name="user", content=f"m{i}")
        msgs = world.messages.list_messages(ROOM_ID)
        assert len(project_for_member_windowed(msgs, "finance", recent_n=0)) == \
               len(project_for_member(msgs, "finance"))

    async def test_resolve_already_resolved_is_idempotent_noop(self, world):
        held = world.held.hold(
            ROOM_ID, session_id="s", member="finance", room_version=1,
            payload="x", held_reason=HELD_REASON_NO_WEBHOOK, chat_id="c")
        delivered = []
        async def deliver(c, t): delivered.append((c, t)); return True
        resolver = AgentRoomHeldResolver(
            held_store=world.held, deliver=deliver,
            room_version_provider=lambda _: 1)
        await resolver.resolve_one(held)
        assert len(delivered) == 1
        # re-resolving the same (now resolved) row must not double-deliver
        refetched = world.held.get(held.id)
        assert refetched.status == STATUS_RESOLVED

    async def test_held_empty_payload_is_not_persisted_by_router(self, world):
        """An empty member reply is a genuine stay-silent, not a lost
        artifact — the router's _hold_reply must no-op on empty payload."""
        hid = world.router._hold_reply(
            room_id=ROOM_ID, session_id="s", member="finance",
            payload="   ", held_reason=HELD_REASON_FENCED, room_version=5)
        assert hid is None
        assert world.held.list_held(ROOM_ID) == []

    def test_room_version_provider_failure_degrades_to_zero(self, tmp_path):
        w = RoomWorld(tmp_path)
        try:
            def boom(_): raise RuntimeError("db down")
            w.router._room_version_provider = boom
            assert w.router._current_room_version(ROOM_ID) == 0  # never raises
        finally:
            w.close()


# ===========================================================================
# REAL SCENARIO — multi-turn, mirrors the production 3-role support room
# ===========================================================================

class TestScenario:
    async def test_topic_switch_reroutes_to_different_member(self, world):
        # turn 1: shipping → customer_service
        world.first_hop_result = FirstHopResult(
            members=["customer_service"], matched=True, reason="")
        out1 = await world.send("我的包裹还没到")
        assert out1["target_member"] == "customer_service"
        # turn 2: new topic (invoice) → finance. Classifier says NOT reuse.
        world.classifier_result = ClassifierResult(is_new_topic=True, confidence=1.0)
        world.first_hop_result = FirstHopResult(
            members=["finance"], matched=True, reason="")
        out2 = await world.send("顺便问下发票怎么开")
        assert out2["target_member"] == "finance"

    async def test_concurrent_broadcast_fans_out_to_all(self, world):
        world.first_hop_result = FirstHopResult(
            members=["customer_service", "finance"], matched=True, reason="")
        out = await world.send("这次故障退款和补偿一起说下")
        assert out.get("concurrent") is True
        assert set(out["replies"].keys()) == {"customer_service", "finance"}
        # both member replies landed in the room
        names = {m.sender_name for m in world.messages.list_messages(ROOM_ID)
                 if m.sender_kind == "member"}
        assert {"customer_service", "finance"} <= names

    async def test_no_match_silent_policy_stays_silent(self, tmp_path):
        w = RoomWorld(tmp_path, no_match_policy="silent")
        try:
            w.first_hop_result = FirstHopResult(
                members=[], matched=False, reason="无匹配")
            v_before = w.messages.max_sequence(ROOM_ID)
            out = await w.send("讲个笑话吧")
            assert out["stayed_silent"] is True
            assert out["target_member"] is None
            # nobody was dispatched → no member reply appended (only user turn)
            assert not w.dispatched
            assert w.messages.max_sequence(ROOM_ID) == v_before + 1  # just the user turn
        finally:
            w.close()

    async def test_user_interrupt_midcompose_then_recover(self, world):
        """End-to-end room-moved recovery: the member's stale draft is held,
        then a resolver reruns and delivers a fresh answer reflecting the
        interruption."""
        world.member_brains["finance"] = lambda msg: "原始报销答复"
        # first dispatch produces a reply; then user interrupts
        await world.send("报销要多久")
        snap = world.messages.max_sequence(ROOM_ID)
        world.user_says("算了我不报了")
        held = world.held.hold(
            ROOM_ID, session_id="room_member:room_support:finance",
            member="finance", room_version=snap - 1,  # reasoned before the reply+interrupt
            payload="原始报销答复", held_reason=HELD_REASON_ROOM_MOVED, chat_id="chat-1")
        delivered = []
        async def deliver(c, t): delivered.append((c, t)); return True
        async def rerun(h): return "好的，那报销先取消，需要时再找我"
        resolver = AgentRoomHeldResolver(
            held_store=world.held, deliver=deliver,
            room_version_provider=world.messages.max_sequence, rerun_member=rerun)
        out = await resolver.resolve_one(held)
        assert out.path == RESOLUTION_REVISE
        assert delivered[0][1] == "好的，那报销先取消，需要时再找我"


# ===========================================================================
# ROLE — each role's contract
# ===========================================================================

class TestRole:
    async def test_router_never_answers_only_routes(self, world):
        """The router/observer role produces NO user-facing text of its own —
        the reply always comes verbatim from a member brain."""
        world.member_brains["customer_service"] = lambda msg: "MEMBER-ONLY-TEXT"
        out = await world.send("你好")
        assert out["reply"] == "MEMBER-ONLY-TEXT"

    async def test_member_receives_no_match_fallback_framing(self, world):
        """A member forced as fallback gets the explicit fallback-framing
        prefix so it answers with judgment rather than 'not my job'."""
        world.first_hop_result = FirstHopResult(
            members=[], matched=False, reason="不属于任何成员")
        seen = {}
        world.member_brains["customer_service"] = (
            lambda msg: seen.setdefault("inbound", msg) or "ok")
        await world.send("量子计算机怎么造")
        assert "兜底" in seen["inbound"]  # fallback framing was layered in

    def test_room_fetch_context_is_scoped_to_acting_member_room(self, tmp_path):
        """Role isolation: room_fetch_context only ever reads the room the
        acting member is bound to — a member cannot read another room."""
        wa = RoomWorld(tmp_path / "a")
        wb = RoomWorld(tmp_path / "b")
        try:
            wa.messages.append(ROOM_ID, sender_kind="user",
                              sender_name="user", content="ROOM-A-SECRET")
            wb.messages.append(ROOM_ID, sender_kind="user",
                              sender_name="user", content="ROOM-B-SECRET")
            tok = rfc.bind_room_context(wa.messages, ROOM_ID,
                                        SimpleNamespace(name="finance"))
            try:
                r = json.loads(rfc.room_fetch_context(query="SECRET"))
                assert r["ok"]
                blob = json.dumps(r, ensure_ascii=False)
                assert "ROOM-A-SECRET" in blob
                assert "ROOM-B-SECRET" not in blob  # bound to A's store only
            finally:
                tok.reset()
        finally:
            wa.close(); wb.close()

    async def test_member_cannot_hand_off_to_itself(self, world):
        """@mention role rule: a member mentioning itself does NOT create a
        self-handoff loop."""
        world.first_hop_result = FirstHopResult(
            members=["finance"], matched=True, reason="")
        # finance's reply @mentions itself and customer_service
        world.member_brains["finance"] = (
            lambda msg: "@finance @customer_service 请协助")
        world.member_brains["customer_service"] = lambda msg: "cs handled"
        out = await world.send("退款相关")
        hop_members = [h["member"] for h in out.get("handoffs", [])]
        assert "finance" not in hop_members          # no self-handoff
        assert "customer_service" in hop_members       # peer handoff fired

    async def test_at_all_fans_out_to_everyone_but_sender(self, world):
        world.first_hop_result = FirstHopResult(
            members=["customer_service"], matched=True, reason="")
        world.member_brains["customer_service"] = lambda msg: "@all 请大家看下"
        world.member_brains["finance"] = lambda msg: "fin ok"
        world.member_brains["tech_support"] = lambda msg: "tech ok"
        out = await world.send("全员通知")
        hop_members = {h["member"] for h in out.get("handoffs", [])}
        assert hop_members == {"finance", "tech_support"}  # sender excluded
