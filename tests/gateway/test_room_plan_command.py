"""``/room plan <request>`` proposes a team from the local profiles; ``/room confirm`` creates it.

The fork's agent-room had a planner; Hosted Rooms only had ``/room create`` with a hand-picked roster.
The planner's picks are validated against the real profile set (an invented or duplicate profile is
dropped, an unknown decider fails), the proposal belongs to the asking user in that chat, and confirm
creates the room through the same path as ``/room create``. Only the auxiliary LLM call is stubbed.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import hosted_rooms
from gateway import room_plan
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli.profiles import write_profile_meta
from hermes_constants import get_hermes_home


@pytest.fixture
def team_home(tmp_path, monkeypatch):
    home = get_hermes_home()
    for name, desc in (("lead", "Plans and coordinates releases."), ("api", "Builds the backend API."),
                       ("qa", "Writes and runs tests.")):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
        write_profile_meta(home / "profiles" / name, description=desc)
    db = tmp_path / "shared.db"
    created = []

    def create_room(*, room_id, name, members):
        room = hosted_rooms.create_room(db, room_id=room_id, name=name, members=members,
                                        authority_gateway_id="g", now=1)
        created.append(room)
        return room

    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service",
                        lambda: SimpleNamespace(create_room=create_room))
    import gateway.slash_commands_room_plan as mixin
    monkeypatch.setattr(mixin, "_PROPOSALS", room_plan.ProposalStore())
    return SimpleNamespace(created=created)


def _llm_reply(monkeypatch, payload):
    seen = {}

    def fake_call(verb, task_id, *, aux_task, system, user, max_tokens, timeout, log):
        seen["user"] = user
        return json.dumps(payload), ""
    monkeypatch.setattr("hermes_cli.kanban_specify._call_aux", fake_call)
    return seen


def _run(text, user="u1"):
    source = SessionSource(platform=Platform.DINGTALK, chat_id="cidG==", chat_type="group", user_id=user)
    return asyncio.run(GatewaySlashCommandsMixin()._handle_room_command(MessageEvent(text=text, source=source)))


def test_plan_then_confirm_creates_the_validated_team(team_home, monkeypatch):
    seen = _llm_reply(monkeypatch, {"name": "Release crew", "decider": "lead",
                                    "members": ["api", "qa", "ghost", "api", "lead"], "reason": "API + tests"})

    proposal = _run("/room plan ship the v2 API with tests")

    assert "api: Builds the backend API." in seen["user"]   # the planner sees role descriptions
    assert "@lead" in proposal and "@api, @qa" in proposal and "ghost" not in proposal
    assert team_home.created == []                          # nothing is created before confirm
    assert _run("/room confirm", user="someone-else").startswith(("No pending", "你在本会话"))  # not theirs
    _run("/room confirm")
    (room,) = team_home.created
    roles = {m["profile"]: m.get("role", "worker") for m in room["members"]}
    assert roles == {"lead": "decider", "api": "worker", "qa": "worker"}
    assert room["name"] == "Release crew"


def test_unknown_decider_is_rejected_and_nothing_is_pending(team_home, monkeypatch):
    _llm_reply(monkeypatch, {"name": "X", "decider": "ghost", "members": ["api"]})
    assert "ghost" in _run("/room plan do something")
    _run("/room confirm")
    assert team_home.created == []
