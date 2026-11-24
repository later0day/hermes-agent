"""Registration and public-dispatch contracts for the Room observer tool."""

from __future__ import annotations

import json

import tools.room_router_tool
from model_tools import handle_function_call
from toolsets import TOOLSETS


# ---------------------------------------------------------------------------
# toolsets.py registration (observer lockdown)
# ---------------------------------------------------------------------------


def test_room_observer_toolset_is_registered():
    assert "room_observer" in TOOLSETS


def test_room_observer_grants_only_route_to_member():
    """The observer profile must not receive terminal, file, or delegation tools."""
    entry = TOOLSETS["room_observer"]
    assert set(entry["tools"]) == {"route_to_member"}


def test_room_observer_has_no_includes():
    """Including another toolset would defeat the observer's tool lockdown."""
    entry = TOOLSETS["room_observer"]
    assert entry["includes"] == []


def test_room_observer_description_mentions_route_to_member():
    desc = TOOLSETS["room_observer"]["description"]
    assert "route_to_member" in desc


def test_route_to_member_appears_in_exactly_one_toolset():
    owners = [
        name
        for name, entry in TOOLSETS.items()
        if "route_to_member" in entry.get("tools", [])
    ]
    assert owners == ["room_observer"], (
        f"route_to_member leaked into other toolsets: {owners}"
    )


def test_hermes_cli_toolset_does_not_grant_route_to_member():
    cli = TOOLSETS.get("hermes-cli", {})
    assert "route_to_member" not in cli.get("tools", [])


# ---------------------------------------------------------------------------
# Generic model-tools -> registry -> Room handler dispatch
# ---------------------------------------------------------------------------


def test_registry_dispatch_forwards_all_routing_arguments():
    result = json.loads(handle_function_call(
        "route_to_member",
        {
            "member": ["researcher", "reviewer"],
            "reason": "Both research and review are required",
            "is_new_topic": True,
        },
        task_id="room-task",
        tool_call_id="route-call",
        session_id="room-session",
    ))

    assert result == {
        "action": "route_to_member",
        "member": ["researcher", "reviewer"],
        "reason": "Both research and review are required",
        "is_new_topic": True,
    }


def test_route_dispatch_uses_request_and_execution_middleware(monkeypatch):
    def request_middleware(kind, **kwargs):
        if kind != "tool_request":
            return []
        return [{
            "args": {**kwargs["args"], "member": ["reviewer"]},
            "source": "room-test-request",
            "reason": "rewrite member",
        }]

    def execution_middleware(**kwargs):
        return kwargs["next_call"]({
            **kwargs["args"],
            "reason": kwargs["args"]["reason"] + " after execution middleware",
            "is_new_topic": True,
        })

    manager = type(
        "Manager",
        (),
        {"_middleware": {
            "tool_request": [request_middleware],
            "tool_execution": [execution_middleware],
        }},
    )()
    monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", request_middleware)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)

    result = json.loads(handle_function_call(
        "route_to_member",
        {"member": ["researcher"], "reason": "original", "is_new_topic": False},
    ))

    assert result["member"] == ["reviewer"]
    assert result["reason"] == "original after execution middleware"
    assert result["is_new_topic"] is True


def test_route_dispatch_honors_pre_tool_call_guardrail(monkeypatch):
    def invoke_hook(hook_name, **_kwargs):
        if hook_name == "pre_tool_call":
            return [{"action": "block", "message": "Room routing is paused"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a blocked routing decision must not reach the handler")
        ),
    )

    result = json.loads(handle_function_call(
        "route_to_member",
        {"member": ["researcher"], "reason": "should not run", "is_new_topic": True},
    ))

    assert result == {"error": "Room routing is paused"}


def test_route_dispatch_emits_post_tool_call_with_result_and_identity(monkeypatch):
    hooks = []
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: hooks.append((hook_name, kwargs)) or [],
    )

    raw_result = handle_function_call(
        "route_to_member",
        {"member": ["researcher"], "reason": "post-hook check", "is_new_topic": False},
        task_id="room-task",
        tool_call_id="route-call",
        session_id="room-session",
    )

    post_calls = [kwargs for name, kwargs in hooks if name == "post_tool_call"]
    assert len(post_calls) == 1
    post = post_calls[0]
    assert post["tool_name"] == "route_to_member"
    assert post["args"] == {
        "member": ["researcher"],
        "reason": "post-hook check",
        "is_new_topic": False,
    }
    assert post["task_id"] == "room-task"
    assert post["tool_call_id"] == "route-call"
    assert post["session_id"] == "room-session"
    assert post["result"] == raw_result
    assert json.loads(post["result"])["reason"] == "post-hook check"
    assert post["duration_ms"] >= 0
