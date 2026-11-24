"""Desktop E2E-only tool that exercises the real generic gateway approval queue."""

import json

from tools import approval as _approval
from tools.approval_context import get_current_session_key
from tools.approval_gateway_wait import _await_gateway_decision


def request_approval(args, **_kwargs):
    kind = args["kind"]
    session_key = get_current_session_key()
    notify_cb = _approval._gateway_notify_cb(session_key)
    if notify_cb is None:
        raise RuntimeError("the E2E session has no gateway approval notifier")

    data = {
        "allow_permanent": False,
        "choices": ["once", "deny"],
        "command": "",
        "description": f"E2E {kind} approval",
        "kind": kind,
        "pattern_key": f"approval_kinds_e2e_{kind}",
        "pattern_keys": [f"approval_kinds_e2e_{kind}"],
        "risk": args["risk"],
    }
    if kind == "access":
        data.update(access=args["access"], path=args["path"])
    else:
        data["host"] = args["host"]

    decision = _await_gateway_decision(session_key, notify_cb, data)
    return json.dumps({
        "choice": decision.get("choice"),
        "kind": kind,
        "reason": decision.get("reason"),
        "resolved": decision.get("resolved"),
    })


def register(ctx):
    ctx.register_tool(
        name="approval_kinds_e2e",
        toolset="approval_kinds_e2e",
        schema={
            "name": "approval_kinds_e2e",
            "description": "Desktop E2E fixture for folder and site approval cards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "access": {"type": "string", "enum": ["read", "read_write"]},
                    "host": {"type": "string"},
                    "kind": {"type": "string", "enum": ["access", "network"]},
                    "path": {"type": "string"},
                    "risk": {"type": "string"},
                },
                "required": ["kind", "risk"],
            },
        },
        handler=request_approval,
    )
