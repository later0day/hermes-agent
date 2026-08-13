"""Pure parser: AIAgent observer run_result -> RoutingDecision.

Extracted from gateway/run.py's _observer_runner so the SAME parsing
logic (native tool-call path + qwen text-form fallbacks) can be reused
by the dashboard's in-process room runner (hermes_cli/web_server.py).

No persistence, no logging side effects, no gateway coupling. Given a
run_result dict (as returned by AIAgent.run_conversation), return a
RoutingDecision describing what the observer decided:

  * action == "decompose_and_route" + decompose_tasks — M4 subtask DAG
  * action == "route"  + target_member (str/list)     — single/multi route
  * empty target_member                                — no decision found

Recognized shapes (qwen3.7-max via dashscope OpenAI-compat is unreliable
about emitting native tool calls, so we accept several text forms):
  A. native tool result:   {"action": "route_to_member"|"decompose_and_route", ...}
  B. JSON blob in content:  {...} (with tasks[] or member)
  C. python-call form:      route_to_member(member=[...], reason=...)
  D. <tool>{...}</tool>     xml-ish wrapper
"""

from __future__ import annotations

import ast
import json
import re
from typing import Optional

from gateway.agent_room_router import RoutingDecision


def _decision_from_dict(d: dict) -> Optional[RoutingDecision]:
    """Map a parsed dict (tool result OR text blob) to a RoutingDecision.
    Returns None if the dict carries no usable decision."""
    if not isinstance(d, dict):
        return None

    # ── decompose_and_route ──────────────────────────────────────────
    tasks = None
    if d.get("action") == "decompose_and_route":
        tasks = d.get("tasks")
    elif d.get("name") == "decompose_and_route":
        args = d.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        tasks = args.get("tasks")
        d = args if isinstance(args, dict) else d
    elif isinstance(d.get("tasks"), list) and d.get("tasks"):
        tasks = d.get("tasks")
    if isinstance(tasks, list) and tasks:
        return RoutingDecision(
            target_member="",
            reason=d.get("reason", ""),
            is_new_topic=bool(d.get("is_new_topic", False)),
            reused_last_route=False,
            action="decompose_and_route",
            decompose_tasks=tasks,
        )

    # ── route_to_member ──────────────────────────────────────────────
    member = None
    reason = ""
    is_new = False
    if d.get("action") == "route_to_member":
        member = d.get("member", "")
        reason = d.get("reason", "")
        is_new = bool(d.get("is_new_topic", False))
    elif d.get("name") == "route_to_member":
        args = d.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        member = args.get("member", "")
        reason = args.get("reason", "")
        is_new = bool(args.get("is_new_topic", False))
    elif "member" in d:
        member = d.get("member", "")
        reason = d.get("reason", "")
        is_new = bool(d.get("is_new_topic", False))

    if member not in (None, "", []):
        return RoutingDecision(
            target_member=member,
            reason=reason,
            is_new_topic=is_new,
            reused_last_route=False,
        )
    return None


def parse_observer_decision(run_result: dict) -> RoutingDecision:
    """Given an AIAgent.run_conversation result, return the observer's
    RoutingDecision. Falls back to an empty-target decision (router then
    routes to default_member) if nothing parseable is found."""
    messages = (run_result or {}).get("messages", []) or []

    # ── Path A: native tool results (most reliable) ──────────────────
    for msg_item in reversed(messages):
        if msg_item.get("role") == "tool":
            try:
                d = json.loads(msg_item.get("content", "{}"))
            except Exception:
                continue
            dec = _decision_from_dict(d)
            if dec is not None:
                return dec

    # ── Path A2: native assistant tool_calls ─────────────────────────
    for msg_item in reversed(messages):
        if msg_item.get("role") == "assistant" and msg_item.get("tool_calls"):
            for tc in msg_item["tool_calls"]:
                fn = tc.get("function") or {}
                dec = _decision_from_dict({
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                })
                if dec is not None:
                    return dec

    # ── Path B/C/D: text-form fallbacks on the LAST assistant row ────
    last_assistant = None
    for msg_item in reversed(messages):
        if msg_item.get("role") == "assistant":
            last_assistant = msg_item
            break

    if last_assistant is not None:
        content = (last_assistant.get("content") or "").strip()
        if content and len(content) >= 3:
            parsed = None

            # Format B: JSON blob {...}
            blob = re.search(r"\{.*\}", content, re.DOTALL)
            if blob:
                try:
                    p = json.loads(blob.group())
                    if isinstance(p, dict):
                        parsed = p
                except Exception:
                    parsed = None

            # Format C: python-call route_to_member(...) / decompose_and_route(...)
            if parsed is None:
                for fn_name in ("decompose_and_route", "route_to_member"):
                    call = re.search(fn_name + r"\s*\((.*)\)", content, re.DOTALL)
                    if not call:
                        continue
                    # Normalize JS/JSON literals the model often emits inside
                    # a python-style call (true/false/null) to Python ones so
                    # ast.literal_eval doesn't choke on bare Name nodes.
                    arg_src = call.group(1)
                    arg_src = re.sub(r"\btrue\b", "True", arg_src)
                    arg_src = re.sub(r"\bfalse\b", "False", arg_src)
                    arg_src = re.sub(r"\bnull\b", "None", arg_src)
                    try:
                        node = ast.parse(f"f({arg_src})", mode="eval").body
                    except Exception:
                        continue
                    if isinstance(node, ast.Call):
                        kw = {}
                        for k in node.keywords:
                            if not k.arg:
                                continue
                            try:
                                kw[k.arg] = ast.literal_eval(k.value)
                            except Exception:
                                continue  # skip an un-evaluable keyword
                        if kw:
                            # Shape it like a native tool call so
                            # _decision_from_dict reads the args from
                            # ``arguments`` (the ``name==...`` branches look
                            # there, not at top-level keys).
                            parsed = {"name": fn_name, "arguments": kw}
                            break

            # Format D: <tool>{...}</tool>
            if parsed is None:
                for cand in re.findall(r"<tool>\s*(\{.*?\})\s*</tool>", content, re.DOTALL):
                    try:
                        p = json.loads(cand)
                    except Exception:
                        continue
                    if isinstance(p, dict):
                        parsed = p
                        break

            if parsed is not None:
                dec = _decision_from_dict(parsed)
                if dec is not None:
                    return dec

    return RoutingDecision(
        target_member="",
        reason="observer produced no parseable routing output",
        is_new_topic=True,
        reused_last_route=False,
    )
