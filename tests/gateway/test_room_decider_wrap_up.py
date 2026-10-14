"""A star room's decider gets the last word when a Discussion cap would end it.

Live 2026-10-15 (room demo-competitor-5): a decider and four workers spent the 10-message budget in two rounds
(dispatch, work, re-dispatch after a fact check, rework) and the room went ``bounded`` without a final report.
"""

import time

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms

ROOM_ID, GW = "room-wrap", "gateway-a"
WORKERS = ("w1", "w2", "w3", "w4")
MEMBERS = [{"member_id": "m-lead", "profile": "lead", "handle": "lead", "role": "decider"},
           *({"member_id": f"m-{w}", "profile": w, "handle": w} for w in WORKERS)]
LOCAL = ("lead", *WORKERS)


def _run(db, room, reply, limit=20):
    """Drive the policy like the service does; returns (decisions, published member handles)."""
    spoken = []
    for _ in range(limit):
        events = hosted_rooms.read_events(db, room_id=ROOM_ID, since_seq=0, limit=500)["events"]
        decision = discussion.plan_next_task(room, events, local_profiles=LOCAL)
        if decision.task is None:
            return decision, spoken
        task = decision.task
        spoken.append((task.member.handle, decision.reason, task.payload["prompt"]))
        publication = discussion.plan_publication(room, events, task, status="settled",
                                                  result={"text": reply(task.member.handle)}, local_profiles=LOCAL)
        for event in publication.events:
            hosted_rooms.append_event(db, **event.append_kwargs(ROOM_ID), now=time.time())
    raise AssertionError("discussion never ended")


def test_decider_reports_when_the_message_budget_runs_out(tmp_path):
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(db, room_id=ROOM_ID, name="R", members=MEMBERS, authority_gateway_id=GW, now=1)
    hosted_rooms.append_event(db, room_id=ROOM_ID, authority_gateway_id=GW, authority_epoch=1, now=time.time(),
                              event_id="u1", kind="message.user", actor={"kind": "user", "id": "u"},
                              payload={"text": "research", "thread_id": "t"})
    everyone = " ".join(f"@{w} do part" for w in WORKERS)

    final, spoken = _run(db, room, lambda who: everyone if who == "lead" else f"@lead {who} result")

    assert final.status == "bounded"
    handle, reason, prompt = spoken[-1]
    assert (handle, reason) == ("lead", "max_messages_wrap_up")
    assert "this is your last turn" in prompt
    assert [h for h, _, _ in spoken].count("lead") == 3
