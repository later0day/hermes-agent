"""Agent/profile audit logging tests."""

from __future__ import annotations

import json

import pytest

from gateway.agent_audit import (
    append_agent_audit_event,
    list_agent_audit_events,
    redact_agent_audit_data,
)
from gateway.config import Platform
from gateway.session import SessionSource


def test_redact_agent_audit_data_redacts_secret_fields_but_keeps_binding_key():
    data = {
        "source_binding_key": "source:dingtalk:chat-1:user-1",
        "session_webhook": "https://example.test/secret-webhook",
        "nested": {
            "api_key": "sk-secret",
            "app_key": "ding-key",
            "profile_name": "worker",
        },
        "items": [{"token": "tok-secret"}, {"label": "safe"}],
    }

    redacted = redact_agent_audit_data(data)

    assert redacted["source_binding_key"] == "source:dingtalk:chat-1:user-1"
    assert redacted["session_webhook"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["app_key"] == "[REDACTED]"
    assert redacted["nested"]["profile_name"] == "worker"
    assert redacted["items"][0]["token"] == "[REDACTED]"
    assert redacted["items"][1]["label"] == "safe"


def test_append_agent_audit_event_writes_redacted_jsonl(tmp_path):
    audit_path = tmp_path / "audit" / "agent.jsonl"
    source = SessionSource(
        platform=Platform.DINGTALK,
        chat_id="chat-1",
        chat_type="group",
        user_id="user-1",
        user_name="Alice",
        thread_id="thread-1",
    )

    event = append_agent_audit_event(
        "agent.use",
        audit_path=audit_path,
        source=source,
        actor_user_id="user-1",
        actor_user_name="Alice",
        profile_name="worker",
        before={"profile_name": "default", "webhook": "https://old.example"},
        after={"profile_name": "worker", "api_key": "sk-worker"},
        extra={"source_binding_key": "source:dingtalk:chat-1:user-1"},
    )

    line = audit_path.read_text(encoding="utf-8").strip()
    stored = json.loads(line)

    assert stored == event
    assert stored["action"] == "agent.use"
    assert stored["actor_user_id"] == "user-1"
    assert stored["source"]["platform"] == "dingtalk"
    assert stored["source"]["chat_id"] == "chat-1"
    assert stored["source"]["thread_id"] == "thread-1"
    assert stored["before"]["webhook"] == "[REDACTED]"
    assert stored["after"]["api_key"] == "[REDACTED]"
    assert stored["extra"]["source_binding_key"] == "source:dingtalk:chat-1:user-1"


def test_append_agent_audit_event_requires_action(tmp_path):
    with pytest.raises(ValueError, match="action is required"):
        append_agent_audit_event("", audit_path=tmp_path / "audit.jsonl")


def test_list_agent_audit_events_filters_recent_and_redacts(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    append_agent_audit_event(
        "agent.use",
        audit_path=audit_path,
        profile_name="alpha",
        after={"token": "secret-alpha"},
    )
    append_agent_audit_event(
        "agent.clear",
        audit_path=audit_path,
        profile_name="beta",
        after={"profile_name": "beta"},
    )
    append_agent_audit_event(
        "agent.delete",
        audit_path=audit_path,
        profile_name="alpha",
        after={"profile_name": "alpha"},
    )
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write("{not-json}\n")

    events = list_agent_audit_events(
        audit_path=audit_path,
        profile_name="alpha",
        limit=10,
    )

    assert [event["action"] for event in events] == ["agent.delete", "agent.use"]
    assert events[1]["after"]["token"] == "[REDACTED]"


def test_list_agent_audit_events_supports_offset_and_scan_window(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    for idx in range(6):
        append_agent_audit_event(
            f"agent.{idx}",
            audit_path=audit_path,
            profile_name="alpha",
        )

    events = list_agent_audit_events(
        audit_path=audit_path,
        profile_name="alpha",
        limit=2,
        offset=1,
        max_scan_lines=4,
    )

    assert [event["action"] for event in events] == ["agent.4", "agent.3"]
