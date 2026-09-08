from pathlib import Path

import pytest

from gateway import hosted_rooms
from gateway import room_task_dag as dag


@pytest.fixture
def room_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "state.db"
    hosted_rooms.create_room(
        db,
        room_id="room-web",
        name="Web room",
        members=[{"member_id": "lead", "handle": "lead", "profile": "default"}],
        authority_gateway_id="gateway-a",
        now=1,
    )
    dag.create_task(db, room_id="room-web", task_id="t1", subject="Build")
    monkeypatch.setattr(hosted_rooms, "default_db_path", lambda: db)
    return db


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_rooms_list_preserves_fields_and_adds_summary(room_db):
    response = _client().get("/api/rooms")
    assert response.status_code == 200
    room = response.json()["rooms"][0]
    assert room["room_id"] == "room-web"
    assert room["name"] == "Web room"
    assert room["workspace"]["task_counts"]["total"] == 1
    assert room["workspace"]["task_counts"]["ready"] == 1


def test_workspace_is_available_from_durable_db_without_live_service(room_db):
    response = _client().get("/api/rooms/room-web/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["tasks"][0]["task_id"] == "t1"
    assert body["log"]["has_more"] is False




def test_rooms_create_and_send_are_thin_service_adapters(monkeypatch):
    class FakeService:
        def __init__(self):
            self.created = None
            self.sent = None

        def create_room(self, **kwargs):
            self.created = kwargs
            return {"room_id": kwargs["room_id"], "name": kwargs["name"], "members": kwargs["members"]}

        def send(self, **kwargs):
            self.sent = kwargs
            return {"event_id": kwargs["event_id"], "payload": kwargs["payload"]}

    service = FakeService()
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: service)
    client = _client()
    created = client.post("/api/rooms", json={
        "room_id": "launch-team",
        "name": "Launch team",
        "members": [
            {"profile": "lead", "handle": "lead", "display_name": "Lead", "role": "decider"},
            {"profile": "researcher", "handle": "researcher", "display_name": "Researcher", "role": "worker"},
        ],
    })
    assert created.status_code == 200
    assert service.created["members"][0]["role"] == "decider"
    assert service.created["members"][1]["role"] == "worker"

    sent = client.post("/api/rooms/launch-team/messages", json={
        "recipient": "lead",
        "text": "Plan and delegate the launch review",
        "event_id": "web-event-1",
        "thread_id": "web-thread-1",
    })
    assert sent.status_code == 200
    assert sent.json()["accepted"] is True
    assert service.sent == {
        "room_id": "launch-team",
        "event_id": "web-event-1",
        "payload": {"text": "@lead Plan and delegate the launch review", "thread_id": "web-thread-1"},
        "recipient": "lead",
    }


def test_rooms_mutations_require_live_service(monkeypatch):
    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: None)
    response = _client().post("/api/rooms", json={
        "room_id": "no-service",
        "name": "No service",
        "members": [
            {"profile": "lead", "handle": "lead", "role": "decider"},
            {"profile": "worker", "handle": "worker", "role": "worker"},
        ],
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "room service not available"}



def test_rooms_send_rejects_empty_task_before_service(monkeypatch):
    class FakeService:
        def send(self, **kwargs):
            raise AssertionError("service must not receive empty work")

    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: FakeService())
    response = _client().post("/api/rooms/room/messages", json={
        "recipient": "lead", "text": "   ", "event_id": "empty", "thread_id": "thread",
    })
    assert response.status_code == 400
    assert response.json() == {"detail": "task text is required"}


def test_rooms_mutations_map_not_found_and_conflict(monkeypatch):
    class FakeService:
        def create_room(self, **kwargs):
            raise hosted_rooms.RoomConflictError("room already exists")

        def send(self, **kwargs):
            raise hosted_rooms.RoomNotFoundError("room not found")

    monkeypatch.setattr("tui_gateway.methods_groups.get_hosted_room_service", lambda: FakeService())
    client = _client()
    created = client.post("/api/rooms", json={
        "room_id": "duplicate", "name": "Duplicate", "members": [
            {"profile": "lead", "handle": "lead", "role": "decider"},
            {"profile": "worker", "handle": "worker", "role": "worker"},
        ],
    })
    missing = client.post("/api/rooms/missing/messages", json={
        "recipient": "lead", "text": "Plan this", "event_id": "missing", "thread_id": "thread",
    })
    assert created.status_code == 409
    assert missing.status_code == 404

def test_workspace_missing_room_is_exact_404(room_db):
    response = _client().get("/api/rooms/missing/workspace")
    assert response.status_code == 404
    assert response.json() == {"detail": "room not found"}
