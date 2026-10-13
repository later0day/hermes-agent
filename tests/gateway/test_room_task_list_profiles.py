"""`/room task list` without a live room service enumerates profiles from disk.

That fallback must apply the same liveness rules as ``HostedRoomService.local_profiles``: after
`/agent delete` (or `hermes profile delete`) the ``profiles/.deleted/`` tombstone dir exists, and
feeding it to roster validation turned every live-DAG listing into "invalid local profile".
"""

from __future__ import annotations

import time

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms

GATEWAY_ID = "gateway-a"
ROOM_ID = "room-1"
MEMBERS = [
    {"member_id": "m-plan", "profile": "plan", "handle": "plan", "role": "decider",
     "target": {"kind": "local", "profile": "plan"}},
    {"member_id": "m-be", "profile": "backend", "handle": "backend",
     "target": {"kind": "local", "profile": "backend"}},
]


def _make_profile(home, name):
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    return profile


@pytest.mark.asyncio
async def test_task_list_fallback_ignores_deleted_profile_tombstones(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_constants import get_hermes_home, mark_named_profile_deleted

    home = get_hermes_home()
    _make_profile(home, "plan")
    _make_profile(home, "backend")
    mark_named_profile_deleted(_make_profile(home, "gone"))
    assert (home / "profiles" / ".deleted").is_dir()

    db = tmp_path / "shared-state.db"
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db)
    room = hosted_rooms.create_room(db, room_id=ROOM_ID, name="Release", members=MEMBERS,
                                    authority_gateway_id=GATEWAY_ID, now=1)
    hosted_rooms.append_event(
        db, room_id=ROOM_ID, event_id="u1", kind="message.user",
        actor={"kind": "user", "id": "u"}, authority_gateway_id=GATEWAY_ID, authority_epoch=1,
        payload={"text": "Ship it", "thread_id": "t1"}, now=time.time(),
    )
    events = hosted_rooms.read_events(db, room_id=ROOM_ID, since_seq=0,
                                      limit=hosted_rooms.MAX_LOG_LIMIT)["events"]
    decision = discussion.plan_next_task(room, events, local_profiles=("plan", "backend"))
    publication = discussion.plan_publication(
        room, events, decision.task, status="settled", result={"text": "@backend build\n\nthe API"},
        local_profiles=("plan", "backend"),
    )
    for event in publication.events:
        hosted_rooms.append_event(db, **event.append_kwargs(ROOM_ID), now=time.time())

    source = SessionSource(platform=Platform.TELEGRAM, user_id="admin", chat_id="c1",
                           chat_type="group")
    out = await GatewaySlashCommandsMixin()._handle_room_command(
        MessageEvent(text=f"/room task list {ROOM_ID}", source=source)
    )

    assert "error" not in out.lower(), out
    rows = [line for line in out.splitlines() if "@backend" in line]
    assert rows and "build the API" in rows[0]  # a multi-line dispatch stays one task row
