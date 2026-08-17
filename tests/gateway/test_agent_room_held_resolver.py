"""Tests for gateway/agent_room_held_resolver.py (Raft AX 改造 1 · resolver)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gateway.agent_room_held_store import (
    AgentRoomHeldStore,
    HELD_REASON_FENCED,
    HELD_REASON_NO_WEBHOOK,
    HELD_REASON_ROOM_MOVED,
    RESOLUTION_REVISE,
    RESOLUTION_SEND_ANYWAY,
    RESOLUTION_SEND_AS_IS,
    RESOLUTION_STAY_SILENT,
    STATUS_RESOLVED,
)
from gateway.agent_room_held_resolver import AgentRoomHeldResolver


@pytest.fixture()
def store(tmp_path):
    s = AgentRoomHeldStore(db_path=tmp_path / "held.sqlite")
    yield s
    s.close()


def _hold(store, *, room_version=5, payload="the reply", chat_id="chat-1",
          reason=HELD_REASON_NO_WEBHOOK, member="finance"):
    return store.hold(
        "room1", session_id="room_member:room1:finance", member=member,
        room_version=room_version, payload=payload, held_reason=reason,
        chat_id=chat_id,
    )


class TestSendAsIs:
    @pytest.mark.asyncio
    async def test_room_unchanged_sends_as_is_and_resolves(self, store):
        held = _hold(store, room_version=5)
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,  # unchanged
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_SEND_AS_IS
        assert outcome.delivered is True
        assert outcome.resolved is True
        deliver.assert_awaited_once_with("chat-1", "the reply")
        assert store.get(held.id).status == STATUS_RESOLVED

    @pytest.mark.asyncio
    async def test_unknown_version_treated_as_send_as_is(self, store):
        held = _hold(store, room_version=0)  # held with no provider wired
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 99,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_SEND_AS_IS
        assert outcome.delivered is True

    @pytest.mark.asyncio
    async def test_delivery_failure_leaves_row_held(self, store):
        held = _hold(store)
        deliver = AsyncMock(return_value=False)  # transport still down
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.delivered is False
        assert outcome.resolved is False
        # Still held for a later retry — NOT consumed.
        assert store.get(held.id).status == "held"

    @pytest.mark.asyncio
    async def test_delivery_raising_leaves_row_held(self, store):
        held = _hold(store)
        deliver = AsyncMock(side_effect=RuntimeError("network down"))
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.delivered is False
        assert outcome.resolved is False
        assert "network down" in outcome.error
        assert store.get(held.id).status == "held"

    @pytest.mark.asyncio
    async def test_no_chat_id_cannot_send(self, store):
        held = _hold(store, chat_id=None)
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcome = await resolver.resolve_one(held, path=RESOLUTION_SEND_AS_IS)
        assert outcome.delivered is False
        assert outcome.error == "no chat_id"
        deliver.assert_not_awaited()


class TestRevise:
    @pytest.mark.asyncio
    async def test_room_moved_triggers_revise(self, store):
        held = _hold(store, room_version=5, payload="stale reply")
        deliver = AsyncMock(return_value=True)
        rerun = AsyncMock(return_value="fresh reply")
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9,  # room moved forward
            rerun_member=rerun,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_REVISE
        assert outcome.delivered is True
        # rerun receives the full HeldReply (unambiguous re-run context).
        rerun.assert_awaited_once_with(held)
        # The FRESH reply is delivered, not the stale payload.
        deliver.assert_awaited_once_with("chat-1", "fresh reply")
        assert store.get(held.id).resolution == RESOLUTION_REVISE

    @pytest.mark.asyncio
    async def test_revise_without_rerun_degrades_to_send_as_is(self, store):
        held = _hold(store, room_version=5, payload="stale but better than nothing")
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9,  # moved → wants Revise
            rerun_member=None,                      # but no rerun wired
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_SEND_AS_IS
        assert outcome.delivered is True
        deliver.assert_awaited_once_with("chat-1", "stale but better than nothing")

    @pytest.mark.asyncio
    async def test_revise_empty_rerun_becomes_stay_silent(self, store):
        held = _hold(store, room_version=5)
        deliver = AsyncMock(return_value=True)
        rerun = AsyncMock(return_value="   ")  # member now has nothing to add
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9, rerun_member=rerun,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_STAY_SILENT
        assert outcome.delivered is False
        assert outcome.resolved is True
        deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revise_rerun_raises_falls_back_to_send_as_is(self, store):
        held = _hold(store, room_version=5, payload="original")
        deliver = AsyncMock(return_value=True)
        rerun = AsyncMock(side_effect=RuntimeError("rerun boom"))
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9, rerun_member=rerun,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_SEND_AS_IS
        assert outcome.delivered is True
        deliver.assert_awaited_once_with("chat-1", "original")


class TestRoomMovedDegrade:
    """Raft AX 改造 1: a room_moved hold is *provably* stale (a new user
    turn superseded it). Without a rerun callable we must NOT re-land it
    (that is the counting-game non-sequitur the hold exists to prevent) —
    the honest degrade is a logged Stay-silent, not Send-as-is."""

    @pytest.mark.asyncio
    async def test_room_moved_without_rerun_degrades_to_stay_silent(self, store):
        held = _hold(
            store, room_version=5, payload="reply to the OLD question",
            reason=HELD_REASON_ROOM_MOVED,
        )
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9,  # moved → wants Revise
            rerun_member=None,                      # but no rerun wired
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_STAY_SILENT
        assert outcome.delivered is False
        assert outcome.resolved is True
        # The stale non-sequitur must NOT be delivered.
        deliver.assert_not_awaited()
        assert store.get(held.id).resolution == RESOLUTION_STAY_SILENT

    @pytest.mark.asyncio
    async def test_transport_lost_hold_still_degrades_to_send_as_is(self, store):
        # A no_webhook hold's reply is still valid — only transport died,
        # so with no rerun it degrades to Send-as-is (deliver-beats-drop),
        # NOT stay-silent. This guards the reason-aware branch.
        held = _hold(
            store, room_version=5, payload="valid reply, dead transport",
            reason=HELD_REASON_NO_WEBHOOK,
        )
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9,  # moved → wants Revise
            rerun_member=None,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_SEND_AS_IS
        assert outcome.delivered is True
        deliver.assert_awaited_once_with("chat-1", "valid reply, dead transport")

    @pytest.mark.asyncio
    async def test_room_moved_with_rerun_still_revises(self, store):
        # When a rerun IS wired, room_moved takes the real Revise path
        # (fresh reply), not the stay-silent degrade.
        held = _hold(
            store, room_version=5, payload="stale",
            reason=HELD_REASON_ROOM_MOVED,
        )
        deliver = AsyncMock(return_value=True)
        rerun = AsyncMock(return_value="fresh answer to the NEW question")
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 9, rerun_member=rerun,
        )
        outcome = await resolver.resolve_one(held)
        assert outcome.path == RESOLUTION_REVISE
        assert outcome.delivered is True
        deliver.assert_awaited_once_with("chat-1", "fresh answer to the NEW question")


class TestForcedPaths:
    @pytest.mark.asyncio
    async def test_forced_stay_silent_does_not_deliver(self, store):
        held = _hold(store)
        deliver = AsyncMock(return_value=True)
        resolver = AgentRoomHeldResolver(held_store=store, deliver=deliver)
        outcome = await resolver.resolve_one(held, path=RESOLUTION_STAY_SILENT)
        assert outcome.path == RESOLUTION_STAY_SILENT
        assert outcome.delivered is False
        assert outcome.resolved is True
        deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forced_send_anyway_ignores_version_drift(self, store):
        held = _hold(store, room_version=5, payload="send me regardless")
        deliver = AsyncMock(return_value=True)
        rerun = AsyncMock(return_value="would-revise")
        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 99,  # moved a lot
            rerun_member=rerun,
        )
        outcome = await resolver.resolve_one(held, path=RESOLUTION_SEND_ANYWAY)
        assert outcome.path == RESOLUTION_SEND_ANYWAY
        assert outcome.delivered is True
        # send-anyway must NOT revise — deliver the original verbatim.
        rerun.assert_not_awaited()
        deliver.assert_awaited_once_with("chat-1", "send me regardless")


class TestResumeAll:
    @pytest.mark.asyncio
    async def test_resume_drains_backlog_oldest_first(self, store):
        h1 = _hold(store, payload="first", chat_id="c1")
        h2 = _hold(store, payload="second", chat_id="c2")
        delivered_order: list[str] = []

        async def deliver(chat_id, payload):
            delivered_order.append(payload)
            return True

        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcomes = await resolver.resume_all("room1")
        assert len(outcomes) == 2
        assert delivered_order == ["first", "second"]
        assert store.list_held("room1") == []  # all resolved

    @pytest.mark.asyncio
    async def test_resume_keeps_undeliverable_held(self, store):
        _hold(store, payload="ok", chat_id="c1")
        _hold(store, payload="stuck", chat_id="c2")

        async def deliver(chat_id, payload):
            return payload != "stuck"  # second one fails

        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcomes = await resolver.resume_all("room1")
        assert sum(1 for o in outcomes if o.delivered) == 1
        remaining = store.list_held("room1")
        assert len(remaining) == 1
        assert remaining[0].payload == "stuck"  # stays for next drain

    @pytest.mark.asyncio
    async def test_resume_isolates_one_row_crash(self, store):
        _hold(store, payload="good", chat_id="c1")

        # deliver raises for everything, but resume must still return
        # outcomes without propagating the exception.
        async def deliver(chat_id, payload):
            raise RuntimeError("boom")

        resolver = AgentRoomHeldResolver(
            held_store=store, deliver=deliver,
            room_version_provider=lambda _rid: 5,
        )
        outcomes = await resolver.resume_all("room1")
        assert len(outcomes) == 1
        assert outcomes[0].delivered is False
