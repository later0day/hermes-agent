"""The decider sees what each teammate is good at, so it can dispatch by expertise.

Hosted Rooms gave the decider only ``@handle`` names; the fork's agent-room routed work on member
descriptions. The decider prompt now lists each teammate with its profile's role description
(``profile.yaml`` — the same signal kanban routing uses), read from a real temp HERMES_HOME.
"""

from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms
from hermes_cli.profiles import write_profile_meta
from hermes_constants import get_hermes_home

ROOM_ID, GATEWAY_ID = "room-hints", "gateway-a"
MEMBERS = [
    {"member_id": "m-lead", "profile": "lead", "handle": "lead", "role": "decider"},
    {"member_id": "m-api", "profile": "api", "handle": "api"},
    {"member_id": "m-qa", "profile": "qa", "handle": "qa"},
]


def test_decider_prompt_lists_teammates_with_their_role_descriptions(tmp_path):
    home = get_hermes_home()
    for name in ("lead", "api", "qa"):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    write_profile_meta(home / "profiles" / "api", description="Designs and builds the REST/gRPC backend.")
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(db, room_id=ROOM_ID, name="Release", members=MEMBERS,
                                    authority_gateway_id=GATEWAY_ID, now=1)
    hosted_rooms.append_event(
        db, room_id=ROOM_ID, event_id="u1", kind="message.user", actor={"kind": "user", "id": "u"},
        authority_gateway_id=GATEWAY_ID, authority_epoch=1, payload={"text": "Ship it", "thread_id": "t"}, now=2)
    events = hosted_rooms.read_events(db, room_id=ROOM_ID, since_seq=0, limit=50)["events"]

    task = discussion.plan_next_task(room, events, local_profiles=("lead", "api", "qa")).task

    assert task is not None and task.member.profile == "lead"
    prompt = task.payload["prompt"]
    assert "- @api: Designs and builds the REST/gRPC backend." in prompt
    assert "- @qa\n" in prompt or prompt.rstrip().endswith("- @qa")   # undescribed teammate: handle only
    assert "- @lead" not in prompt                                     # not itself
