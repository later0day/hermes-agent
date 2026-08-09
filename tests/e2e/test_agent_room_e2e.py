"""End-to-end tests for Agent Room — M1 + M2 + M3.

Exercises the full path a user would hit via dashboard or slash command,
but with the aux LLM and member LLM mocked so we can assert deterministic
outputs without spending real API tokens.

Covers:
  1. planner → confirm → room created with new profiles
  2. bind + inbound message → observer routes → member persists reply
  3. broadcast message → observer returns array → concurrent dispatch
  4. cross-member context via M3 projection
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


async def _client():
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
    )


def _mock_llm_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated Hermes home so tests don't touch real profile files."""
    home = tmp_path / "hermes"
    (home / "profiles").mkdir(parents=True)
    (home / "profiles" / "default").mkdir()  # so create_profile clone_from finds it
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home))
    yield home
    reset_hermes_home_override(token)


# ============================================================================
# E2E 1: /api/rooms/plan → /api/rooms/plan/confirm full flow
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_plan_and_confirm_creates_room(hermes_home):
    """Live-style flow: user submits requirement → aux LLM proposes plan →
    user confirms → new profiles created, observer built, room in store."""

    plan_json = json.dumps({
        "rationale": "Team needs customer support and finance",
        "members": [
            {"profile": None, "is_new": True, "name": "cs_agent",
             "description": "Handles customer inquiries",
             "reason": "no existing CS profile"},
            {"profile": None, "is_new": True, "name": "finance_agent",
             "description": "Handles billing",
             "reason": "billing needs a specialist"},
        ],
        "room_description": "Customer support and billing room",
    })

    # Fresh room store for this test
    from gateway.agent_room_store import AgentRoomStore
    from hermes_cli import web_server as ws
    ws._pending_room_plans.clear()

    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_llm_response(plan_json)), \
         patch("hermes_cli.profiles.list_profiles", return_value=[]), \
         patch("hermes_cli.web_server._room_store",
               side_effect=lambda: AgentRoomStore(hermes_home / "rooms.sqlite")), \
         patch("hermes_cli.web_server._room_binding_store"):

        # Step 1: request plan
        async with await _client() as c:
            r = await c.post(
                "/api/rooms/plan",
                json={"requirement": "客服 + 财务"},
            )
        assert r.status_code == 200, r.text
        plan = r.json()["plan"]
        assert len(plan["members"]) == 2
        assert plan["members"][0]["is_new"] is True

        # Step 2: mock the profile-creation side effects
        with patch("hermes_cli.profiles.profile_exists", return_value=False), \
             patch("hermes_cli.profiles.create_profile"), \
             patch("hermes_cli.profiles.write_profile_meta"), \
             patch("hermes_cli.profiles.get_profile_dir",
                   return_value=hermes_home / "profiles" / "obs"), \
             patch("gateway.agent_room_bootstrapper.build_observer_profile"):
            async with await _client() as c:
                r = await c.post("/api/rooms/plan/confirm", json={})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["room"]["room_name"]
        assert "cs_agent" in body["new_profiles"]
        assert "finance_agent" in body["new_profiles"]


# ============================================================================
# E2E 2: sanitizer — Chinese LLM output → ASCII profile names
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_plan_sanitizes_chinese_names(hermes_home):
    """Live bug: LLM emitted '客服专员' → create_profile rejected.
    Fixed by _sanitize_profile_name — verify end-to-end."""

    plan_json = json.dumps({
        "rationale": "test",
        "members": [
            {"profile": None, "is_new": True, "name": "客服专员",
             "description": "客服"},
            {"profile": None, "is_new": True, "name": "财务-Finance-Team",
             "description": "财务"},
        ],
        "room_description": "test",
    })

    from hermes_cli import web_server as ws
    ws._pending_room_plans.clear()

    with patch("agent.auxiliary_client.call_llm",
               return_value=_mock_llm_response(plan_json)), \
         patch("hermes_cli.profiles.list_profiles", return_value=[]):

        async with await _client() as c:
            r = await c.post("/api/rooms/plan",
                             json={"requirement": "客服团队"})

    assert r.status_code == 200
    plan = r.json()["plan"]

    # Sanitizer: '客服专员' has no ASCII → falls back to 'member'
    # '财务-Finance-Team' has 'Finance-Team' extractable
    import re
    for m in plan["members"]:
        assert re.match(r"^[a-z0-9][a-z0-9_-]*$", m["name"]), (
            f"Sanitized name failed regex: {m['name']}"
        )


# ============================================================================
# E2E 3: Full router loop — observer → route_to_member → member reply
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_router_full_loop_with_projection(hermes_home):
    """M3 pipeline: user message → messages store append → observer sees
    projected history → route → member sees projected history → reply
    persisted."""

    from gateway.agent_room_store import AgentRoomStore
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    from gateway.agent_room_router import AgentRoomRouter, ClassifierResult, RoutingDecision
    from gateway.agent_room_projection import project_for_observer, project_for_member

    store = AgentRoomStore(hermes_home / "rooms.sqlite")
    msgs_store = AgentRoomMessagesStore(hermes_home / "msgs.sqlite")
    room = store.create_room(
        "room_e2e", "team",
        observer_profile="obs",
        members=["m1", "m2"],
        description="e2e test",
    )

    # Simulate: user message inbound
    msgs_store.append(room.room_id, sender_kind="user",
                      sender_name="alice", content="how do I refund?")

    # Observer runner: simulate LLM picking m1
    obs_runner = AsyncMock(return_value=RoutingDecision(
        target_member="m1", reason="refund query fits m1",
        is_new_topic=True, reused_last_route=False,
    ))
    ack_sender = AsyncMock(return_value="handle_1")
    ack_editor = AsyncMock()
    classifier = AsyncMock(return_value=ClassifierResult(
        is_new_topic=True, confidence=1.0,
    ))

    # Member dispatcher: simulate LLM reply + persist to msgs_store
    async def member_dispatch(member, sid, src, hist, msg):
        # Verify projection was passed via hist (M3 wires history via router)
        reply = f"reply from {member}: got '{msg}'"
        msgs_store.append(
            room.room_id, sender_kind="member",
            sender_name=member, content=reply,
        )
        return reply

    member_dispatcher = AsyncMock(side_effect=member_dispatch)

    router = AgentRoomRouter(
        store=store,
        ack_sender=ack_sender,
        ack_editor=ack_editor,
        classifier=classifier,
        observer_runner=obs_runner,
        member_dispatcher=member_dispatcher,
    )

    result = await router.process_message(
        source=MagicMock(),
        message="how do I refund?",
        history=[],
        room=room,
    )

    assert result["target_member"] == "m1"
    assert result["reply"] == "reply from m1: got 'how do I refund?'"

    # M3: verify projection saw the right history
    all_msgs = msgs_store.list_messages(room.room_id)
    assert len(all_msgs) == 2  # user + member reply
    assert all_msgs[0].sender_kind == "user"
    assert all_msgs[1].sender_kind == "member"

    # If we ran a second turn, observer would see multi-party history
    projected = project_for_observer(all_msgs)
    assert len(projected) == 2
    assert "[user]:" in projected[0].content
    assert "[m1]:" in projected[1].content
    store.close()
    msgs_store.close()


# ============================================================================
# E2E 4: Concurrent multi-member dispatch (M3.5)
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_concurrent_broadcast(hermes_home):
    """Broadcast message: observer returns array → all members run
    concurrently → each reply persists → response bundle has replies dict."""

    from gateway.agent_room_store import AgentRoomStore
    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    from gateway.agent_room_router import AgentRoomRouter, ClassifierResult, RoutingDecision

    store = AgentRoomStore(hermes_home / "rooms.sqlite")
    msgs_store = AgentRoomMessagesStore(hermes_home / "msgs.sqlite")
    room = store.create_room(
        "room_bc", "team",
        observer_profile="obs",
        members=["m1", "m2", "m3"],
    )

    # Observer picks ALL THREE (broadcast intent)
    obs_runner = AsyncMock(return_value=RoutingDecision(
        target_member=["m1", "m2", "m3"],
        reason="broadcast greeting — all members reply",
        is_new_topic=True, reused_last_route=False,
    ))

    call_order = []

    async def member_dispatch(member, sid, src, hist, msg):
        import asyncio as _a
        call_order.append(f"start:{member}")
        await _a.sleep(0.01)  # force overlap
        call_order.append(f"done:{member}")
        return f"hello from {member}"

    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(return_value=ClassifierResult(
            is_new_topic=True, confidence=1.0,
        )),
        observer_runner=obs_runner,
        member_dispatcher=AsyncMock(side_effect=member_dispatch),
    )

    result = await router.process_message(
        source=MagicMock(),
        message="大家好，请介绍",
        history=[],
        room=room,
    )

    assert result["concurrent"] is True
    assert set(result["replies"].keys()) == {"m1", "m2", "m3"}
    assert result["replies"]["m1"] == "hello from m1"
    assert result["replies"]["m2"] == "hello from m2"
    assert result["replies"]["m3"] == "hello from m3"

    # Concurrency check: all 3 starts happen before any done
    starts = [o for o in call_order if o.startswith("start:")]
    dones = [o for o in call_order if o.startswith("done:")]
    assert len(starts) == 3 and len(dones) == 3
    assert call_order.index(starts[2]) < call_order.index(dones[0])

    store.close()
    msgs_store.close()


# ============================================================================
# E2E 5: Fence — mid-flight structural change drops in-flight turn
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_fence_drops_reply_on_concurrent_delete(hermes_home):
    """User sends message → observer starts routing → admin deletes room
    mid-flight → fence check drops the reply rather than send stale content."""

    from gateway.agent_room_store import AgentRoomStore
    from gateway.agent_room_router import AgentRoomRouter, ClassifierResult, RoutingDecision

    store = AgentRoomStore(hermes_home / "rooms.sqlite")
    room = store.create_room("room_fence", "team", observer_profile="obs",
                             members=["m1"])

    # Fence the observer's session before observer even runs
    store.fence_room(room.room_id, [f"room_observer:{room.room_id}"])

    obs_runner = AsyncMock(return_value=RoutingDecision(
        target_member="m1", reason="", is_new_topic=True,
        reused_last_route=False,
    ))
    member_dispatcher = AsyncMock(return_value="reply")

    router = AgentRoomRouter(
        store=store,
        ack_sender=AsyncMock(),
        ack_editor=AsyncMock(),
        classifier=AsyncMock(return_value=ClassifierResult(
            is_new_topic=True, confidence=1.0,
        )),
        observer_runner=obs_runner,
        member_dispatcher=member_dispatcher,
    )

    result = await router.process_message(
        source=MagicMock(),
        message="hello",
        history=[],
        room=room,
    )

    # Fenced at observer level → no dispatch, no reply delivered
    assert result["fenced_at"] == "observer"
    assert result["reply"] is None
    member_dispatcher.assert_not_awaited()

    store.close()


# ============================================================================
# E2E 6: Cross-member context via projection (M1 §8 mitigation)
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_cross_member_projection_context(hermes_home):
    """User asks m1, then switches topic to m2 — m2 sees m1's past reply
    in its projected history via M3 (superseding M1's §8 summary hack)."""

    from gateway.agent_room_messages_store import AgentRoomMessagesStore
    from gateway.agent_room_projection import project_for_member

    msgs_store = AgentRoomMessagesStore(hermes_home / "msgs.sqlite")

    # Simulate a prior conversation with m1
    msgs_store.append("r1", sender_kind="user", sender_name="alice",
                      content="退款怎么办")
    msgs_store.append("r1", sender_kind="observer", sender_name="obs",
                      content="",
                      tool_calls=[{"function": {"name": "route_to_member",
                                                "arguments": '{"member":"m1"}'}}])
    msgs_store.append("r1", sender_kind="member", sender_name="m1",
                      content="请提供订单号")
    msgs_store.append("r1", sender_kind="user", sender_name="alice",
                      content="顺便问下发票")

    # Now m2 is about to receive routing for the new topic
    all_msgs = msgs_store.list_messages("r1")
    projected_for_m2 = project_for_member(all_msgs, target_member="m2")

    # m2 should see all 4 rows, with m1's reply rendered as "[m1]: ..."
    assert len(projected_for_m2) == 4
    # None of these are m2's own turn → all should be role="user"
    assert all(p.role == "user" for p in projected_for_m2)
    # m1's reply should be prefixed with [m1] so m2 knows who said it
    m1_content = projected_for_m2[2].content
    assert "[m1]" in m1_content
    assert "订单号" in m1_content

    msgs_store.close()
