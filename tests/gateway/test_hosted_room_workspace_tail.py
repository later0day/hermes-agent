"""The Rooms workspace shows a long room's NEWEST events, not its oldest page.

``build_room_workspace`` read the log once from seq 0 (ascending, capped at MAX_LOG_LIMIT events and a
byte budget), so a room past one page rendered its beginning and never its latest messages or final
report. It now pages forward from the tail window.
"""

from gateway import hosted_room_views as views
from gateway import hosted_rooms

ROOM = "room-long"


def test_workspace_of_a_room_past_one_page_shows_its_latest_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_rooms, "MAX_LOG_LIMIT", 20)
    db = tmp_path / "state.db"
    hosted_rooms.create_room(
        db, room_id=ROOM, name="Long room", authority_gateway_id="gateway-a", now=1,
        members=[{"member_id": "lead", "handle": "lead", "profile": "default", "role": "coordinator"}])
    for i in range(45):
        hosted_rooms.append_event(
            db, room_id=ROOM, event_id=f"u{i}", kind="message.user", actor={"kind": "user", "id": "user"},
            payload={"text": f"message-{i:02d}", "thread_id": "t"}, authority_gateway_id="gateway-a",
            authority_epoch=1, now=2 + i)

    conversation = str(views.build_room_workspace(db, room_id=ROOM)["conversation"])

    assert "message-44" in conversation   # the newest message is on screen
    assert "message-00" not in conversation
