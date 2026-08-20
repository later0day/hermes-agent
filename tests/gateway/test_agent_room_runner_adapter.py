"""Contract tests for gateway/agent_room_runner_adapter.

These guard the reverse-dependency of the Agent Room fork feature on the
official (upstream) internal execution APIs. If an upstream merge renames or
re-signs any of them, verify_official_contract() turns CI red here instead of
failing at runtime deep inside a live room turn.
"""

from __future__ import annotations

import pytest

from gateway import agent_room_runner_adapter as adapter


def test_verify_official_contract_passes():
    """Every official symbol the room depends on exists with a compatible
    signature against the CURRENT upstream code."""
    report = adapter.verify_official_contract()
    assert report["run_conversation_ok"] is True
    assert report["get_tool_definitions_ok"] is True
    assert report["resolve_toolset_ok"] is True
    assert report["secret_scope_ok"] is True
    assert report["auxiliary_client_ok"] is True
    assert report["lifecycle_interrupt_registry_ok"] is True
    assert report["aiagent_init_params"] >= len(adapter.AIAGENT_ROOM_KWARGS)


def test_validate_ai_agent_kwargs_accepts_room_kwargs():
    """The exact kwargs _run_agent_blocking passes must all be accepted."""
    room_kwargs = {k: None for k in adapter.AIAGENT_ROOM_KWARGS}
    adapter.validate_ai_agent_kwargs(room_kwargs)  # must not raise


def test_validate_ai_agent_kwargs_rejects_unknown():
    """A kwarg the official constructor doesn't accept fails loudly & early."""
    with pytest.raises(KeyError):
        adapter.validate_ai_agent_kwargs({"definitely_not_a_real_kwarg": 1})


def test_build_agent_uses_official_class():
    """build_agent must construct the official run_agent.AIAgent (not a fork)."""
    assert adapter.import_ai_agent().__module__ == "run_agent"
