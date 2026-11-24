"""Behavioral coverage for the current hosted-room dashboard REST API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from gateway import hosted_rooms
from gateway.hosted_room_discussion import DiscussionValidationError
from persistence_assembly import open_persistence, profile_scope


def _client() -> TestClient:
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


@pytest.fixture
def room_store(tmp_path, monkeypatch):
    store = open_persistence(profile_scope(home=tmp_path))
    db_path = hosted_rooms.default_db_path(tmp_path)
    monkeypatch.setattr("tui_gateway.methods_groups._active_store", lambda: store)
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db_path)
    yield store
    store.close()


@pytest.fixture
def room_db(room_store):
    hosted_rooms.create_room(
        room_store,
        placement="shared",
        room_id="room-web",
        name="Web room",
        members=[
            {"member_id": "member-lead", "handle": "lead", "profile": "default"},
            {"member_id": "member-research", "handle": "research", "profile": "researcher"},
        ],
        authority_gateway_id="gateway-a",
        now=1,
    )
    return room_store


def test_list_rooms_empty(room_store):

    response = _client().get("/api/rooms")

    assert response.status_code == 200
    assert response.json() == {"rooms": []}


def test_get_room_returns_durable_state_and_live_driver_status(room_db, monkeypatch):
    status = {"state": "running", "active_member": "lead"}
    service = SimpleNamespace(status=lambda room_id: status if room_id == "room-web" else None)
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)

    response = _client().get("/api/rooms/room-web")

    assert response.status_code == 200
    body = response.json()
    assert body["room"]["room_id"] == "room-web"
    assert body["room"]["name"] == "Web room"
    assert [member["handle"] for member in body["room"]["members"]] == ["lead", "research"]
    assert body["driver_status"] == status


def test_get_room_not_found_does_not_require_a_live_service(room_db, monkeypatch):
    monkeypatch.setattr(
        "tui_gateway.methods_groups.get_hosted_room_service",
        lambda: (_ for _ in ()).throw(AssertionError("missing rooms need no driver lookup")),
    )

    response = _client().get("/api/rooms/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "room not found"}


def test_room_log_replays_a_bounded_cursor_slice(room_db):
    for index in range(1, 4):
        hosted_rooms.append_event(
            room_db,
            placement="shared",
            room_id="room-web",
            event_id=f"event-{index}",
            kind="message.user",
            actor={"kind": "user", "id": "alice"},
            authority_gateway_id="gateway-a",
            authority_epoch=1,
            payload={"text": f"message {index}", "thread_id": "thread-1"},
            now=index,
        )

    first_page = _client().get("/api/rooms/room-web/log?since_seq=1&limit=1")
    assert first_page.status_code == 200
    first = first_page.json()
    assert [event["seq"] for event in first["events"]] == [2]
    assert first["cursor"] == 2
    assert first["latest_seq"] == 3
    assert first["has_more"] is True

    second_page = _client().get(f"/api/rooms/room-web/log?since_seq={first['cursor']}&limit=1")
    assert second_page.status_code == 200
    second = second_page.json()
    assert [event["event_id"] for event in second["events"]] == ["event-3"]
    assert second["cursor"] == 3
    assert second["has_more"] is False


def test_send_normalizes_mention_and_surrounding_whitespace(monkeypatch):
    sent = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"event_id": kwargs["event_id"], "seq": 1}

    monkeypatch.setattr(
        "tui_gateway.methods_groups.get_hosted_room_service",
        lambda: SimpleNamespace(send=send),
    )

    response = _client().post("/api/rooms/room-web/messages", json={
        "recipient": "  @lead  ",
        "text": "  Investigate the regression  ",
        "event_id": "web-event-normalized",
        "thread_id": "web-thread-1",
    })

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert sent == [{
        "room_id": "room-web",
        "event_id": "web-event-normalized",
        "payload": {"text": "@lead Investigate the regression", "thread_id": "web-thread-1"},
        "recipient": "lead",
    }]


def test_send_rejects_an_empty_recipient_before_service(monkeypatch):
    monkeypatch.setattr(
        "tui_gateway.methods_groups.get_hosted_room_service",
        lambda: SimpleNamespace(
            send=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("service must not be called"))
        ),
    )

    response = _client().post("/api/rooms/room-web/messages", json={
        "recipient": " @  ",
        "text": "Investigate",
        "event_id": "web-event-no-recipient",
        "thread_id": "web-thread-1",
    })

    assert response.status_code == 400
    assert response.json() == {"detail": "recipient is required"}


def test_create_maps_hosted_room_validation_errors_to_400(monkeypatch):
    def create_room(**_kwargs):
        raise hosted_rooms.HostedRoomError("invalid room roster")

    monkeypatch.setattr(
        "tui_gateway.methods_groups.get_hosted_room_service",
        lambda: SimpleNamespace(create_room=create_room),
    )

    response = _client().post("/api/rooms", json={
        "room_id": "invalid-room",
        "name": "Invalid room",
        "members": [{"profile": "lead", "handle": "lead", "role": "decider"}],
    })

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid room roster"}


def test_send_maps_discussion_validation_errors_to_400(monkeypatch):
    def send(**_kwargs):
        raise DiscussionValidationError("invalid discussion payload")

    monkeypatch.setattr(
        "tui_gateway.methods_groups.get_hosted_room_service",
        lambda: SimpleNamespace(send=send),
    )

    response = _client().post("/api/rooms/room-web/messages", json={
        "recipient": "lead",
        "text": "Investigate",
        "event_id": "web-event-invalid",
        "thread_id": "web-thread-1",
    })

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid discussion payload"}
