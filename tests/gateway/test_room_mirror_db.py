"""Tests for gateway/room_mirror_db.py — C2 outbound mirror cursor store.

Exercises the durable ``room_notify_subs`` claim/advance/rewind discipline
against a real hosted-room event log, including the decider ``member_filter``
single-voice enforcement.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms
from gateway import room_mirror_db as mirror

GATEWAY_ID = "gateway-a"
ROOM_ID = "room-1"
LOCAL_PROFILES = ("plan", "backend", "frontend")
MEMBERS = [
    {
        "member_id": "m-plan",
        "profile": "plan",
        "handle": "plan",
        "role": "decider",
        "target": {"kind": "local", "profile": "plan"},
    },
    {
        "member_id": "m-be",
        "profile": "backend",
        "handle": "backend",
        "target": {"kind": "local", "profile": "backend"},
    },
]


@pytest.fixture
def room_db(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=MEMBERS,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def _events(db: Path) -> list[dict]:
    return hosted_rooms.read_events(
        db, room_id=ROOM_ID, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT
    )["events"]


def _append_user(db: Path, event_id: str, text: str, thread: str = "t1") -> None:
    hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": "u"},
        authority_gateway_id=GATEWAY_ID,
        authority_epoch=1,
        payload={"text": text, "thread_id": thread},
        now=time.time(),
    )


def _drive_member(db: Path, room: dict, text: str) -> None:
    decision = discussion.plan_next_task(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    assert decision.status == "task", decision
    publication = discussion.plan_publication(
        room,
        _events(db),
        decision.task,
        status="settled",
        result={"text": text},
        local_profiles=LOCAL_PROFILES,
    )
    for event in publication.events:
        hosted_rooms.append_event(db, **event.append_kwargs(ROOM_ID), now=time.time())


@pytest.mark.asyncio
async def test_watcher_never_drops_events_after_failure_budget(
    room_db, monkeypatch
):
    from gateway.room_mirror_watcher import GatewayRoomMirrorMixin
    import gateway.room_mirror_watcher as watcher_module

    db, room = room_db
    mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    _append_user(db, "u1", "Ship it")
    _drive_member(db, room, "final answer")
    sends = []

    class _Adapter:
        async def send(self, chat_id, content, *, metadata):
            sends.append((chat_id, content, metadata))
            runner._running = False
            return type("Result", (), {"success": True})()

    class _Runner(GatewayRoomMirrorMixin):
        def __init__(self):
            self._running = True
            self._room_mirror_fail_counts = {
                (ROOM_ID, "telegram", "c1", ""): 12
            }

        def _authorization_adapter(self, platform, profile):
            assert platform.value == "telegram"
            assert profile is None
            return _Adapter()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db)
    monkeypatch.setattr(watcher_module.asyncio, "sleep", _no_sleep)
    runner = _Runner()

    await runner._room_mirror_watcher(interval=0)

    assert sends == [("c1", "@plan: final answer", {})]
    assert mirror.list_subs(db)[0]["last_event_seq"] > 0
    assert runner._room_mirror_fail_counts == {}



def test_claim_advances_cursor_and_second_claim_is_empty(room_db):
    db, room = room_db
    mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    _append_user(db, "u1", "Ship it")
    _drive_member(db, room, "@backend build the API")
    _drive_member(db, room, "built it")

    old, new, events = mirror.claim_unseen_room_events(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert old == 0 and new > 0
    member_ids = {e["payload"]["member_id"] for e in events}
    assert member_ids == {"m-plan", "m-be"}
    assert all(e["kind"] == "message.member" for e in events)

    old2, new2, events2 = mirror.claim_unseen_room_events(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert events2 == [] and old2 == new2 == new


@pytest.mark.asyncio
async def test_room_mirror_command_defaults_decider_to_single_external_voice(
    room_db, monkeypatch
):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.slash_commands import GatewaySlashCommandsMixin

    db, _room = room_db
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="admin",
        chat_id="c1",
        chat_type="group",
    )
    runner = GatewaySlashCommandsMixin()

    await runner._handle_room_command(
        MessageEvent(text="/room mirror room-1", source=source)
    )

    assert mirror.list_subs(db)[0]["member_filter"] == "m-plan"

    await runner._handle_room_command(
        MessageEvent(
            text="/room mirror room-1 --all-members",
            source=source,
        )
    )
    assert mirror.list_subs(db)[0]["member_filter"] is None



def test_member_filter_enforces_single_voice(room_db):
    db, room = room_db
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1",
        member_filter="m-plan",
    )
    _append_user(db, "u1", "Ship it")
    _drive_member(db, room, "@backend build the API")  # decider
    _drive_member(db, room, "built it")                # worker

    _, _, events = mirror.claim_unseen_room_events(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    member_ids = {e["payload"]["member_id"] for e in events}
    assert member_ids == {"m-plan"}  # backend suppressed


def test_rewind_re_exposes_events_and_cas_refuses_stale(room_db):
    db, room = room_db
    mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    _append_user(db, "u1", "Ship it")
    _drive_member(db, room, "@backend build the API")

    old, new, events = mirror.claim_unseen_room_events(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert events

    assert mirror.rewind_cursor(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1",
        claimed_cursor=new, old_cursor=old,
    )
    _, _, again = mirror.claim_unseen_room_events(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert {e["payload"]["member_id"] for e in again} == {"m-plan"}

    # Stale rewind (wrong claimed_cursor) is refused by the CAS guard.
    assert not mirror.rewind_cursor(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1",
        claimed_cursor=999, old_cursor=0,
    )


def test_create_sub_is_idempotent_and_refreshes_filter(room_db):
    db, _ = room_db
    mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    # advance the cursor to a nonzero value
    mirror.advance_cursor(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1", new_cursor=7
    )
    # re-subscribing refreshes member_filter but must NOT reset the cursor
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1",
        member_filter="m-plan",
    )
    subs = mirror.list_subs(db)
    assert len(subs) == 1
    assert subs[0]["member_filter"] == "m-plan"
    assert subs[0]["last_event_seq"] == 7


def test_remove_sub(room_db):
    db, _ = room_db
    mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    assert mirror.remove_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert not mirror.remove_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1"
    )
    assert mirror.list_subs(db) == []


# ── C2 slice 2: full-duplex inbound ────────────────────────────────────────

def test_inbound_defaults_off_and_lookup_ignores_outbound_only(room_db):
    db, _ = room_db
    # An outbound-only mirror (the slice-1 default) must never be treated as an
    # inbound binding — a read-only mirror can't silently start ingesting.
    row = mirror.create_sub(db, room_id=ROOM_ID, platform="telegram", chat_id="c1")
    assert row["inbound"] == 0
    assert (
        mirror.inbound_binding_for_source(
            db, platform="telegram", chat_id="c1"
        )
        is None
    )


def test_inbound_flag_enables_binding_lookup(room_db):
    db, _ = room_db
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1", inbound=True
    )
    binding = mirror.inbound_binding_for_source(
        db, platform="telegram", chat_id="c1"
    )
    assert binding is not None
    assert binding["room_id"] == ROOM_ID
    assert binding["inbound"] == 1
    # A different chat must not match.
    assert (
        mirror.inbound_binding_for_source(
            db, platform="telegram", chat_id="other"
        )
        is None
    )


def test_inbound_none_preserves_existing_flag(room_db):
    db, _ = room_db
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1", inbound=True
    )
    # Refreshing the mirror (e.g. to set member_filter) with inbound=None must
    # NOT disable the already-enabled inbound routing.
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1",
        member_filter="m-plan", inbound=None,
    )
    binding = mirror.inbound_binding_for_source(
        db, platform="telegram", chat_id="c1"
    )
    assert binding is not None and binding["inbound"] == 1
    assert binding["member_filter"] == "m-plan"


def test_inbound_false_disables_routing(room_db):
    db, _ = room_db
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1", inbound=True
    )
    mirror.create_sub(
        db, room_id=ROOM_ID, platform="telegram", chat_id="c1", inbound=False
    )
    assert (
        mirror.inbound_binding_for_source(
            db, platform="telegram", chat_id="c1"
        )
        is None
    )


def test_inbound_lookup_on_missing_db_is_none(tmp_path: Path):
    # Fail-open: a lookup against a db that has never been created returns None
    # rather than raising, so a bridge hiccup can't swallow a user's message.
    missing = tmp_path / "does-not-exist.db"
    assert (
        mirror.inbound_binding_for_source(
            missing, platform="telegram", chat_id="c1"
        )
        is None
    )


def test_inbound_column_added_to_legacy_slice1_db(tmp_path: Path):
    import sqlite3

    db = tmp_path / "state.db"
    # Hand-build a slice-1 table WITHOUT the inbound column, with a live cursor.
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE room_notify_subs (
            room_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            last_event_seq INTEGER NOT NULL DEFAULT 0,
            member_filter TEXT, notifier_profile TEXT, delivery_metadata TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (room_id, platform, chat_id, thread_id))"""
    )
    conn.execute(
        "INSERT INTO room_notify_subs "
        "(room_id, platform, chat_id, thread_id, last_event_seq, created_at) "
        "VALUES ('r', 'telegram', 'c1', '', 9, 0)"
    )
    conn.commit()
    conn.close()

    # First access through room_mirror_db must add the column in place, default
    # 0, and preserve the existing cursor (no migration harness, no data loss).
    subs = mirror.list_subs(db)
    assert len(subs) == 1
    assert subs[0]["inbound"] == 0
    assert subs[0]["last_event_seq"] == 9

