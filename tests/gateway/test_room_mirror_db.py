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
