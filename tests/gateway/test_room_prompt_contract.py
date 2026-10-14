"""Room prompts and publication fixed after the 2026-10-14 live run (room demo-competitor).

Live: the decider's synthesis lacked the "LEADER_REPORT:" prefix the dashboard keys the final-report
card on, and ended with a stray "(pass)"; dispatches were in English for a Chinese request; a worker
claimed verification without lookups; a worker's file never reached the room's shared folder.
"""

import time

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms

ROOM_ID, GW = "room-contract", "gateway-a"
MEMBERS = [
    {"member_id": "m-lead", "profile": "lead", "handle": "lead", "role": "decider"},
    {"member_id": "m-api", "profile": "api", "handle": "api"},
]
LOCAL = ("lead", "api")


def _room(tmp_path):
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(db, room_id=ROOM_ID, name="R", members=MEMBERS, authority_gateway_id=GW, now=1)
    return db, room


def _append(db, **event):
    hosted_rooms.append_event(db, room_id=ROOM_ID, authority_gateway_id=GW, authority_epoch=1, now=time.time(), **event)


def _events(db):
    return hosted_rooms.read_events(db, room_id=ROOM_ID, since_seq=0, limit=100)["events"]


def test_decider_and_worker_prompts_carry_the_contract(tmp_path):
    db, room = _room(tmp_path)
    _append(db, event_id="u1", kind="message.user", actor={"kind": "user", "id": "u"},
            payload={"text": "分析竞品", "thread_id": "t"})
    decider_prompt = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL).task.payload["prompt"]
    assert 'starting with "LEADER_REPORT:"' in decider_prompt
    assert "never append" in decider_prompt and "Write in Chinese" in decider_prompt
    assert "mark those points as unverified" in decider_prompt

    task = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL).task
    for event in discussion.plan_publication(room, _events(db), task, status="settled",
                                             result={"text": "@api 查功能"}, local_profiles=LOCAL).events:
        hosted_rooms.append_event(db, **event.append_kwargs(ROOM_ID), now=time.time())
    worker = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL).task
    assert worker.member.profile == "api"
    prompt = worker.payload["prompt"]
    assert '"Sources:" list' in prompt and "Write in Chinese" in prompt
    assert "about 10 searches" in prompt
    assert f"./{ROOM_ID}/" in prompt


def test_a_stray_trailing_pass_is_stripped_from_a_real_reply(tmp_path):
    db, room = _room(tmp_path)
    _append(db, event_id="u1", kind="message.user", actor={"kind": "user", "id": "u"},
            payload={"text": "go", "thread_id": "t"})
    task = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL).task
    publication = discussion.plan_publication(room, _events(db), task, status="settled",
                                              result={"text": "LEADER_REPORT: done\n\n(pass)"}, local_profiles=LOCAL)
    (message,) = [e for e in publication.events if e.kind == "message.member"]
    assert message.payload["text"] == "LEADER_REPORT: done"


def test_the_language_rule_names_the_script_or_falls_back(tmp_path):
    db, room = _room(tmp_path)
    _append(db, event_id="u1", kind="message.user", actor={"kind": "user", "id": "u"},
            payload={"text": "compare these tools", "thread_id": "t"})
    prompt = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL).task.payload["prompt"]
    assert "Write in the language the user wrote in" in prompt and "Write in Chinese" not in prompt
