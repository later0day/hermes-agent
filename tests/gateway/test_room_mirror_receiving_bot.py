"""``/room mirror`` delivers back through the bot that received the command.

The subscription's ``notifier_profile`` was never set, so the watcher always resolved the PRIMARY bot —
wrong for a secondary profile with its own bot (the primary may not be in that chat: endless failed
sends). Credentialless / ``/agent use``-bound chats arrive on the primary bot and keep ``None``.
"""
from __future__ import annotations

import pytest

from gateway import hosted_rooms
from gateway import room_mirror_db as mirror
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin

MEMBERS = [{"member_id": "m-a", "profile": "a", "handle": "a", "target": {"kind": "local", "profile": "a"}},
           {"member_id": "m-b", "profile": "b", "handle": "b", "target": {"kind": "local", "profile": "b"}}]


class _Runner(GatewaySlashCommandsMixin):
    def __init__(self, owner_profile):
        self.owner_profile = owner_profile

    def _transport_owner(self, source):
        return (object(), self.owner_profile)


def _source():
    src = SessionSource(platform=Platform.TELEGRAM, user_id="admin", chat_id="c1", chat_type="group")
    object.__setattr__(src, "_transport_adapter_ref", lambda: None)
    return src


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    hosted_rooms.create_room(path, room_id="room-1", name="R", members=MEMBERS, authority_gateway_id="g", now=1)
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "expected"), [("own_bot", "own_bot"), (None, None)])
async def test_mirror_here_records_the_receiving_bot(db, owner, expected):
    await _Runner(owner)._handle_room_command(MessageEvent(text="/room mirror room-1", source=_source()))
    assert mirror.list_subs(db)[0]["notifier_profile"] == expected


@pytest.mark.asyncio
async def test_mirror_to_another_chat_does_not_borrow_the_receiving_bot(db):
    await _Runner("own_bot")._handle_room_command(
        MessageEvent(text="/room mirror room-1 telegram:c2", source=_source()))
    assert mirror.list_subs(db)[0]["notifier_profile"] is None
