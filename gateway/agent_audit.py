"""Audit logging helpers for gateway agent/profile operations."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root


DEFAULT_AGENT_AUDIT_LOG = get_default_hermes_root() / "gateway_agent_audit.jsonl"

_AUDIT_LOCK = threading.Lock()
_REDACTED = "[REDACTED]"

_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "app_key",
    "access_key",
    "authorization",
    "key",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "session_webhook",
    "token",
    "webhook",
    "webhook_url",
}
_SECRET_FIELD_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_access_key",
    "_authorization",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_secret_key",
    "_session_webhook",
    "_token",
    "_webhook",
    "_webhook_url",
)


def _is_secret_field(field_name: str | None) -> bool:
    if not field_name:
        return False
    normalized = field_name.lower().replace("-", "_").replace(" ", "_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith(_SECRET_FIELD_SUFFIXES)


def redact_agent_audit_data(value: Any, *, field_name: str | None = None) -> Any:
    """Return a JSON-safe copy with secret-ish fields redacted."""
    if _is_secret_field(field_name):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(key): redact_agent_audit_data(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_agent_audit_data(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return [redact_agent_audit_data(item, field_name=field_name) for item in value]
    return value


def _source_to_audit_dict(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if hasattr(source, "to_dict"):
        data = source.to_dict()
        if isinstance(data, dict):
            return data
    result: dict[str, Any] = {}
    for attr in (
        "platform",
        "chat_id",
        "chat_name",
        "chat_type",
        "user_id",
        "user_name",
        "thread_id",
        "guild_id",
        "parent_chat_id",
        "message_id",
    ):
        val = getattr(source, attr, None)
        if val is None:
            continue
        result[attr] = getattr(val, "value", val)
    return result


def append_agent_audit_event(
    action: str,
    *,
    audit_path: str | Path | None = None,
    source: Any = None,
    actor_user_id: str | None = None,
    actor_user_name: str | None = None,
    profile_name: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one redacted JSONL audit event and return the stored event."""
    clean_action = str(action or "").strip()
    if not clean_action:
        raise ValueError("action is required")

    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": clean_action,
    }
    if actor_user_id is not None:
        event["actor_user_id"] = actor_user_id
    if actor_user_name is not None:
        event["actor_user_name"] = actor_user_name
    if profile_name is not None:
        event["profile_name"] = profile_name
    source_data = _source_to_audit_dict(source)
    if source_data:
        event["source"] = source_data
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    if extra is not None:
        event["extra"] = extra

    redacted = redact_agent_audit_data(event)
    path = Path(audit_path) if audit_path is not None else DEFAULT_AGENT_AUDIT_LOG
    with _AUDIT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(redacted, ensure_ascii=False, sort_keys=True) + "\n")
    return redacted


def list_agent_audit_events(
    *,
    audit_path: str | Path | None = None,
    profile_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
    max_scan_lines: int = 5000,
) -> list[dict[str, Any]]:
    """Return recent redacted audit events, newest first."""
    path = Path(audit_path) if audit_path is not None else DEFAULT_AGENT_AUDIT_LOG
    if not path.exists():
        return []
    try:
        max_events = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        max_events = 50
    try:
        skip_events = max(0, int(offset))
    except (TypeError, ValueError):
        skip_events = 0
    try:
        scan_lines = max(1, min(int(max_scan_lines), 50000))
    except (TypeError, ValueError):
        scan_lines = 5000

    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    scanned = 0
    matched = 0
    for line in reversed(lines[-scan_lines:]):
        scanned += 1
        if scanned > scan_lines:
            break
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if profile_name and str(event.get("profile_name") or "") != profile_name:
            continue
        if matched < skip_events:
            matched += 1
            continue
        matched += 1
        events.append(redact_agent_audit_data(event))
        if len(events) >= max_events:
            break
    return events
