"""C2 inbound routing must not add I/O before ``_handle_message`` claims the session.

The inbound hook runs on every plain message ahead of the session claim, where upstream allows no awaits (a
second message in that window would start a duplicate agent). Unbound chats are therefore rejected from an
in-memory index; only an indexed (bound) source pays for the authoritative SQLite lookup.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway import hosted_rooms
from gateway import room_mirror_db as mir
from gateway.config import Platform
from gateway.room_mirror_watcher import INBOUND_ROOM_NOT_ROUTED, GatewayRoomMirrorMixin
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


@pytest.fixture()
def db(tmp_path, monkeypatch):
    store = storage_handle(tmp_path)
    path = hosted_rooms.default_db_path(tmp_path)
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda _home=None: path)
    monkeypatch.setattr("tui_gateway.methods_groups._active_store", lambda: store)
    return SimpleNamespace(store=store, path=path)


def _source(chat_id="123"):
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id=chat_id, thread_id="", user_id="u1")


def _event(text="hello"):
    return SimpleNamespace(text=text, message_id="m1")


def test_unbound_chat_is_rejected_from_memory_without_touching_sqlite(db, monkeypatch):
    lookups = []  # recorded, not raised: the hook deliberately swallows lookup errors (fail to normal dispatch)
    monkeypatch.setattr(mir, "inbound_binding_for_source", lambda *a, **k: lookups.append(k))
    bridge = GatewayRoomMirrorMixin()

    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source())) is INBOUND_ROOM_NOT_ROUTED
    assert lookups == [], "unbound chat must not reach the SQLite binding lookup"


def _room(db, room_id="room-1"):
    return hosted_rooms.create_room(
        db.store, placement="shared", room_id=room_id, name="R", authority_gateway_id="g", now=1, members=[
            {"member_id": f"m-{p}", "profile": p, "handle": p, "target": {"kind": "local", "profile": p}}
            for p in ("plan", "backend")])


def test_index_tracks_inbound_subscriptions_and_routes_bound_chats(db, monkeypatch):
    _room(db)
    mir.create_sub(db.path, room_id="room-1", platform="telegram", chat_id="123", thread_id="", inbound=True)
    mir.create_sub(db.path, room_id="room-1", platform="telegram", chat_id="999", thread_id="")  # mirror only
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()

    assert bridge._room_inbound_sources == frozenset({("telegram", "123", "")})

    sent = []
    service = SimpleNamespace(send=lambda **kw: sent.append(kw) or {"seq": 1})
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)

    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event("do it"), _source("123"))) is None
    assert sent and sent[0]["room_id"] == "room-1" and sent[0]["payload"]["text"] == "do it"
    # A mirror-only (no --inbound) chat never ingests.
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("999"))) is INBOUND_ROOM_NOT_ROUTED

    mir.remove_sub(db.path, room_id="room-1", platform="telegram", chat_id="123", thread_id="")
    bridge._reload_room_inbound_sources()
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("123"))) is INBOUND_ROOM_NOT_ROUTED


def test_routes_platform_ids_that_are_not_identifier_safe(db, monkeypatch):
    """DingTalk chat/message ids carry ``+ / =`` (WeChat ids an ``@``); routing must not reject them,
    and a redelivered message must keep the same room event id while a new message gets another."""
    chat_id = "cidUKHyy+TSBvzQY6P34TpjPA=="
    _room(db)
    mir.create_sub(db.path, room_id="room-1", platform="dingtalk", chat_id=chat_id, thread_id="", inbound=True)
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()
    sent = []

    def _send(*, room_id, event_id, payload, actor_id):
        # The real log append validates event_id and actor.id as identifiers.
        sent.append({"event_id": event_id, "actor_id": actor_id})
        return hosted_rooms.append_event(
            db.store, placement="shared", room_id=room_id, event_id=event_id, kind="message.user",
            actor={"kind": "user", "id": actor_id}, authority_gateway_id="g", authority_epoch=1,
            payload=payload, now=1)

    service = SimpleNamespace(send=_send)
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)
    source = SimpleNamespace(platform=Platform.DINGTALK, chat_id=chat_id, thread_id="",
                             user_id="$:LWCP_v1:$NoDPZEnaHdl5WfqKihxdt899LnbQ0x+0")

    for message_id in ("msgQY+tEb7fn/oGUZQorA", "msgQY+tEb7fn/oGUZQorA", "msgOther+id=="):
        event = SimpleNamespace(text="add mul", message_id=message_id)
        assert asyncio.run(bridge._maybe_route_inbound_to_room(event, source)) is None

    first, redelivered, other = (call["event_id"] for call in sent)
    assert first == redelivered != other
    assert len({call["actor_id"] for call in sent}) == 1  # one stable identity per IM user


def test_disbanding_a_room_hands_its_inbound_chat_back_to_the_agent(db):
    """A disbanded room must stop capturing its full-duplex chat at once (any disband entry point, any
    process): routing to it only answers "couldn't deliver" and walls the chat off from its agent."""
    room = _room(db)
    mir.create_sub(db.path, room_id="room-1", platform="telegram", chat_id="123", thread_id="", inbound=True)
    bridge = GatewayRoomMirrorMixin()
    bridge._reload_room_inbound_sources()
    assert bridge._room_inbound_sources == frozenset({("telegram", "123", "")})

    hosted_rooms.disband_room(db.store, placement="shared", room_id="room-1",
                              expected_gateway_id=room["authority_gateway_id"], expected_epoch=1)
    bridge._reload_room_inbound_sources()

    assert bridge._room_inbound_sources == frozenset()
    assert mir.inbound_binding_for_source(db.path, platform="telegram", chat_id="123") is None
    assert asyncio.run(bridge._maybe_route_inbound_to_room(_event(), _source("123"))) is INBOUND_ROOM_NOT_ROUTED


@pytest.fixture()
def feishu_room(db, monkeypatch, request):
    """Real Feishu normalization → GatewayRunner → HostedRoomService → SQLite.

    Feishu has no client, so its real chat/sender resolvers use their offline ID
    fallbacks. The only runner doubles are unrelated auth/command gates and the
    local-agent claim boundary; room routing and persistence are not replaced.
    """
    from gateway.config import PlatformConfig
    from gateway.run import GatewayRunner
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from tests.gateway.feishu_helpers import install_dedup_state, make_message, make_sender
    from tui_gateway.hosted_room_service import HostedRoomService

    gateway_id = hosted_rooms.local_authority_gateway_id()
    hosted_rooms.create_room(
        db.store, placement="shared", room_id="room-1", name="R", now=1,
        authority_gateway_id=gateway_id, members=[
            {"member_id": f"m-{p}", "profile": p, "handle": p,
             "target": {"kind": "local", "profile": p}}
            for p in ("plan", "backend")
        ],
    )
    server = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())
    service = HostedRoomService(server, store=db.store, placement="shared")
    # Do not start member turns or a router in this transport/persistence test.
    service._prepare_tracked = lambda _binding: None
    service.runtime = SimpleNamespace(wakeup=lambda: None)
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)

    runner = GatewayRunner.__new__(GatewayRunner)
    local_claims = []
    replies = []

    async def admit(event):
        return event, event.source, False

    async def no_pending_reply(_event, _source, _key):
        return None

    async def no_authority(_event, _source, _key, _internal):
        return False, None

    async def no_idle_command(_event, _source, _key):
        return False, None

    async def no_new_turn_gate(_source, _internal, _key):
        return False, None

    def claim_local_agent(key, source):
        local_claims.append((key, source.chat_id, source.thread_id))
        return None, "local-agent"

    runner._hm_admit_event = admit
    runner._hm_estop_gate = lambda _event, _source, _internal: None
    runner._session_key_for_source = (
        lambda source: f"{source.platform.value}:{source.chat_id}:{source.thread_id or ''}"
    )
    runner._hm_pending_reply_intercepts = no_pending_reply
    runner._hm_authority_admission = no_authority
    runner._hm_evict_idle_stale_agent = lambda _key: None
    runner._is_session_running = lambda _key: False
    runner._hm_dispatch_idle_commands = no_idle_command
    runner._hm_new_turn_gate = no_new_turn_gate
    runner._claim_active_session_slot = claim_local_agent

    adapters = []

    def make_adapter(*, allow_bots="none", require_mention=True):
        adapter = FeishuAdapter(PlatformConfig(extra={
            "allow_bots": allow_bots,
            "require_mention": require_mention,
        }))
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = "u_self"
        adapter._group_policy = adapter._default_group_policy = "open"
        adapter._allow_all_dm = True
        install_dedup_state(adapter)

        async def dispatch(event):
            # Join the normalized event to the real runner synchronously; Feishu's
            # debounce and outbound delivery are outside this cross-layer contract.
            replies.append(await runner._handle_message(event))

        adapter._enqueue_text_event = dispatch
        adapters.append(adapter)
        return adapter

    def bind(*, chat_id="oc_room", thread_id="omt_topic"):
        mir.create_sub(
            db.path, room_id="room-1", platform="feishu", chat_id=chat_id,
            thread_id=thread_id, inbound=True,
        )
        runner._reload_room_inbound_sources()

    def payload(
        *, message_id="om_one", chat_id="oc_room", thread_id="omt_topic",
        chat_type="group", sender_type="user", open_id="ou_human",
        mentioned=False, text="compile the room",
    ):
        mention = SimpleNamespace(
            key="@_user_1", name="Hermes",
            id=SimpleNamespace(open_id="ou_self", user_id="u_self", union_id=""),
        )
        message = make_message(
            message_id=message_id, chat_id=chat_id, chat_type=chat_type,
            mentions=[mention] if mentioned else [],
        )
        message.content = '{"text":"' + ("@_user_1 " if mentioned else "") + text + '"}'
        message.thread_id = thread_id or None
        return SimpleNamespace(event=SimpleNamespace(
            sender=make_sender(sender_type=sender_type, open_id=open_id), message=message,
        ))

    def user_events():
        return [
            event for event in hosted_rooms.read_events(
                db.store, placement="shared", room_id="room-1", since_seq=0,
                limit=hosted_rooms.MAX_LOG_LIMIT,
            )["events"]
            if event["kind"] == "message.user"
        ]

    def close_adapters():
        for adapter in adapters:
            adapter._shutdown_sdk_executor()

    request.addfinalizer(close_adapters)
    return SimpleNamespace(
        bind=bind, make_adapter=make_adapter, payload=payload,
        user_events=user_events, local_claims=local_claims, replies=replies,
    )


@pytest.mark.parametrize(
    "chat_type,sender_type,allow_bots,require_mention,mentioned,bound,expected",
    [
        pytest.param("group", "user", "none", True, True, True, "room", id="bound-human-mentioned"),
        pytest.param("group", "user", "none", True, False, True, "dropped", id="bound-human-unmentioned"),
        pytest.param("group", "bot", "none", True, True, True, "dropped", id="bound-bot-disabled"),
        pytest.param("group", "bot", "all", False, False, True, "room", id="bound-bot-all-unmentioned"),
        pytest.param("group", "bot", "mentions", False, False, True, "dropped", id="bound-bot-mention-required"),
        pytest.param("p2p", "bot", "all", True, False, True, "room", id="bound-dm-bot-no-mention"),
        pytest.param("group", "user", "none", True, True, False, "local", id="unbound-human-mentioned"),
    ],
)
def test_feishu_admission_routes_through_real_gateway_and_room_sqlite(
    feishu_room, chat_type, sender_type, allow_bots, require_mention, mentioned, bound, expected,
):
    thread_id = "omt_topic" if chat_type == "group" else ""
    if bound:
        feishu_room.bind(thread_id=thread_id)
    else:
        feishu_room.bind(thread_id="omt_other")  # same oc_* chat, but not this exact thread
    adapter = feishu_room.make_adapter(allow_bots=allow_bots, require_mention=require_mention)
    data = feishu_room.payload(
        chat_type=chat_type, thread_id=thread_id, sender_type=sender_type,
        open_id="ou_peer_bot" if sender_type == "bot" else "ou_human", mentioned=mentioned,
    )

    asyncio.run(adapter._handle_message_event_data(data))

    events = feishu_room.user_events()
    if expected == "room":
        assert feishu_room.replies == [None]
        assert feishu_room.local_claims == []
        assert len(events) == 1
        assert events[0]["actor"] == {
            "kind": "user", "id": "ou_peer_bot" if sender_type == "bot" else "ou_human",
        }
        assert "compile the room" in events[0]["payload"]["text"]
        assert events[0]["payload"]["thread_id"].startswith("inbound:")
    elif expected == "dropped":
        assert feishu_room.replies == []
        assert feishu_room.local_claims == []
        assert events == []
    else:
        assert feishu_room.replies == ["local-agent"]
        assert len(feishu_room.local_claims) == 1
        assert events == []


def test_feishu_ids_and_thread_survive_redelivery_and_room_event_idempotency(feishu_room):
    from tests.gateway.feishu_helpers import install_dedup_state

    feishu_room.bind(chat_id="oc_room", thread_id="omt_topic")
    adapter = feishu_room.make_adapter(require_mention=False)
    first = feishu_room.payload(message_id="om_repeat")

    asyncio.run(adapter._handle_message_event_data(first))
    asyncio.run(adapter._handle_message_event_data(first))  # Feishu's in-process om_* dedup
    assert len(feishu_room.user_events()) == 1
    original_event_id = feishu_room.user_events()[0]["event_id"]

    # Replay after a process-local dedup-cache loss must still converge on the
    # same room event: the room key includes feishu/oc_*/omt_*/om_*.
    install_dedup_state(adapter)
    asyncio.run(adapter._handle_message_event_data(first))
    asyncio.run(adapter._handle_message_event_data(feishu_room.payload(message_id="om_next")))

    first_event, second_event = feishu_room.user_events()
    assert first_event["actor"] == {"kind": "user", "id": "ou_human"}
    assert first_event["event_id"] == original_event_id
    assert first_event["event_id"] != second_event["event_id"]
    assert first_event["payload"]["thread_id"] == second_event["payload"]["thread_id"]
    assert first_event["payload"]["thread_id"].startswith("inbound:")
    assert feishu_room.local_claims == []
    assert feishu_room.replies == [None, None, None]
