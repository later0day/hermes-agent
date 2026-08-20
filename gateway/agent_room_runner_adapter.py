"""Single consolidation point for Agent Room's dependencies on the OFFICIAL
(upstream) internal execution APIs.

WHY THIS FILE EXISTS
====================
Agent Room is a fork feature ("二改") that deliberately *reuses* the official
agent execution core instead of forking it. The price of that choice is a set
of reverse-dependencies on upstream internals that carry no stability
guarantee:

    run_agent.AIAgent                       (constructor + run_conversation + close)
    model_tools.get_tool_definitions        (raw tool schema fetch)
    toolsets.resolve_toolset                (toolset name -> tool names)
    agent.secret_scope.*                    (per-profile credential scoping)
    agent.auxiliary_client.{call_llm,async_call_llm}
    agent.subagent_lifecycle.get_active_subagent_parent
    agent.interrupt_compat.request_hard_interrupt
    tools.registry.registry

Before this module those imports were scattered across seven room files
(agent_room_inprocess_runner, agent_room_planner, room_router_tool,
room_decompose_tool, room_fetch_context_tool). When an upstream merge renamed
or re-signed one of them the breakage surfaced in random places at runtime.

This adapter makes the coupling EXPLICIT and CENTRAL: every room module should
import the official symbol *from here*, and ``verify_official_contract()``
(exercised by tests/gateway/test_agent_room_runner_adapter.py) fails loudly the
moment an upstream signature drifts — turning a silent runtime break into a
caught-at-CI break.

All official imports are performed LAZILY inside functions on purpose: importing
run_agent at module load pulls the full CLI stack (dotenv, config, ...), which
is neither available nor wanted in every context that imports a room module.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Contract description — the exact upstream surface the room relies on.
# Keep this in sync with the assertions in verify_official_contract().
# ---------------------------------------------------------------------------

#: The AIAgent.__init__ kwargs the room actually passes. Every name here MUST
#: exist in the live official signature (AIAgent has no **kwargs, so an unknown
#: name would raise TypeError at construction — we catch it earlier & clearer).
AIAGENT_ROOM_KWARGS: frozenset[str] = frozenset(
    {
        "api_key",
        "base_url",
        "provider",
        "requested_provider",
        "api_mode",
        "model",
        "max_iterations",
        "quiet_mode",
        "session_id",
        "platform",
        "credential_pool",
        "fallback_model",
        "enabled_toolsets",
    }
)

#: run_conversation kwargs the room passes.
RUN_CONVERSATION_ROOM_KWARGS: frozenset[str] = frozenset(
    {"system_message", "conversation_history"}
)


# ---------------------------------------------------------------------------
# Lazy accessors for the official symbols (import site of record).
# ---------------------------------------------------------------------------


def import_ai_agent() -> type:
    """Return the official ``run_agent.AIAgent`` class."""
    from run_agent import AIAgent

    return AIAgent


def get_tool_definitions() -> Callable[..., list[dict]]:
    from model_tools import get_tool_definitions as _fn

    return _fn


def resolve_toolset() -> Callable[..., Any]:
    from toolsets import resolve_toolset as _fn

    return _fn


def secret_scope_api() -> tuple[Callable, Callable, Callable]:
    """(build_profile_secret_scope, set_secret_scope, reset_secret_scope)."""
    from agent.secret_scope import (
        build_profile_secret_scope,
        set_secret_scope,
        reset_secret_scope,
    )

    return build_profile_secret_scope, set_secret_scope, reset_secret_scope


def auxiliary_call_llm(*, sync: bool) -> Callable:
    """Return the official aux-client LLM caller (sync or async variant)."""
    if sync:
        from agent.auxiliary_client import call_llm

        return call_llm
    from agent.auxiliary_client import async_call_llm

    return async_call_llm


def get_active_subagent_parent() -> Callable:
    from agent.subagent_lifecycle import get_active_subagent_parent as _fn

    return _fn


def request_hard_interrupt() -> Callable:
    from agent.interrupt_compat import request_hard_interrupt as _fn

    return _fn


def tools_registry() -> Any:
    from tools.registry import registry

    return registry


# ---------------------------------------------------------------------------
# Validated construction of the official agent — the single riskiest coupling
# (13 constructor kwargs against an upstream signature that can change).
# ---------------------------------------------------------------------------


def validate_ai_agent_kwargs(kwargs: dict[str, Any]) -> None:
    """Raise ``KeyError`` if any kwarg is not accepted by the live official
    ``AIAgent.__init__`` — a clear, early failure instead of a bare TypeError
    deep in construction. No-op if the signature can't be introspected."""
    AIAgent = import_ai_agent()
    try:
        params = set(inspect.signature(AIAgent.__init__).parameters)
    except (TypeError, ValueError):
        return  # can't introspect (C-accel/decorated) — let construction decide
    unknown = [k for k in kwargs if k not in params]
    if unknown:
        raise KeyError(
            "agent_room_runner_adapter: AIAgent no longer accepts "
            f"{unknown!r} — upstream signature drifted; update the room "
            "kwargs in agent_room_inprocess_runner._run_agent_blocking"
        )


def build_agent(**kwargs: Any) -> Any:
    """Validate then construct the official AIAgent. All room agent turns must
    build their agent through here so kwarg drift is caught in one place."""
    validate_ai_agent_kwargs(kwargs)
    AIAgent = import_ai_agent()
    return AIAgent(**kwargs)


# ---------------------------------------------------------------------------
# Contract self-check — call from tests/CI to catch upstream drift early.
# ---------------------------------------------------------------------------


def verify_official_contract() -> dict[str, Any]:
    """Assert every official symbol the room depends on still exists with a
    compatible signature. Returns a small report dict on success; raises
    ``AssertionError`` (or ImportError) on the first drift so CI turns red.
    """
    report: dict[str, Any] = {}

    # 1) AIAgent constructor accepts all room kwargs.
    AIAgent = import_ai_agent()
    init_params = set(inspect.signature(AIAgent.__init__).parameters)
    missing_init = sorted(AIAGENT_ROOM_KWARGS - init_params)
    assert not missing_init, (
        f"AIAgent.__init__ dropped room kwargs: {missing_init}"
    )
    report["aiagent_init_params"] = len(init_params)

    # 2) run_conversation exists and accepts the room kwargs + a message.
    rc_params = set(inspect.signature(AIAgent.run_conversation).parameters)
    assert "user_message" in rc_params, (
        "AIAgent.run_conversation lost its first positional 'user_message'"
    )
    missing_rc = sorted(RUN_CONVERSATION_ROOM_KWARGS - rc_params)
    assert not missing_rc, (
        f"AIAgent.run_conversation dropped room kwargs: {missing_rc}"
    )
    assert hasattr(AIAgent, "close"), "AIAgent lost close() (turn cleanup)"
    report["run_conversation_ok"] = True

    # 3) get_tool_definitions accepts the raw-fetch kwargs the room passes.
    gtd_params = set(inspect.signature(get_tool_definitions()).parameters)
    for k in ("enabled_toolsets", "quiet_mode", "skip_tool_search_assembly"):
        assert k in gtd_params, f"get_tool_definitions lost kwarg {k!r}"
    report["get_tool_definitions_ok"] = True

    # 4) resolve_toolset callable.
    assert callable(resolve_toolset()), "toolsets.resolve_toolset missing"
    report["resolve_toolset_ok"] = True

    # 5) secret scope trio present.
    build_scope, set_scope, reset_scope = secret_scope_api()
    assert all(callable(f) for f in (build_scope, set_scope, reset_scope))
    report["secret_scope_ok"] = True

    # 6) auxiliary client both variants present.
    assert callable(auxiliary_call_llm(sync=True))
    assert callable(auxiliary_call_llm(sync=False))
    report["auxiliary_client_ok"] = True

    # 7) subagent lifecycle + interrupt compat + registry present.
    assert callable(get_active_subagent_parent())
    assert callable(request_hard_interrupt())
    assert tools_registry() is not None
    report["lifecycle_interrupt_registry_ok"] = True

    return report
