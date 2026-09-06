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


def test_workspace_missing_room_is_exact_404(room_db):
    response = _client().get("/api/rooms/missing/workspace")
    assert response.status_code == 404
    assert response.json() == {"detail": "room not found"}
