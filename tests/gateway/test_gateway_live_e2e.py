"""Live end-to-end smoke tests for gateway changes (Phases 7-12).

Verifies that the gateway pipeline — agent creation, chat, tool execution —
works correctly with a real LLM provider (DashScope / Qwen). Gated behind
``HERMES_LIVE_TESTS=1`` and skipped by default in CI.

Requires ``DASHSCOPE_API_KEY`` to be set (sourced from ``~/.hermes/.env``).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ── Module-level credential loading (before conftest sanitization) ──────────

def _load_user_env() -> None:
    env_file = Path.home() / ".hermes" / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_user_env()

LIVE = os.environ.get("HERMES_LIVE_TESTS") == "1"
DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live-only — set HERMES_LIVE_TESTS=1"),
    pytest.mark.skipif(not DASHSCOPE_KEY, reason="DASHSCOPE_API_KEY not configured"),
]

LIVE_MODEL = "qwen-plus"
LIVE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_agent(**overrides):
    """Create an AIAgent pointed at DashScope with minimal toolset."""
    from run_agent import AIAgent

    defaults = dict(
        model=LIVE_MODEL,
        provider="openai",
        api_key=DASHSCOPE_KEY,
        base_url=LIVE_BASE_URL,
        max_iterations=3,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        disabled_toolsets=["*"],
    )
    defaults.update(overrides)
    return AIAgent(**defaults)


_IS_LIKELY_ERROR_MARKERS = (
    "api call failed", "connection error", "client has been closed",
    "cannot send a request", "max retries", "traceback", "error code",
)


def _assert_healthy_reply(reply, label: str) -> None:
    assert reply and reply.strip(), f"{label} returned empty: {reply!r}"
    lowered = reply.lower().strip()
    for marker in _IS_LIKELY_ERROR_MARKERS:
        if marker in lowered:
            raise AssertionError(
                f"{label} returned an error-sentinel: matched {marker!r}. "
                f"Reply: {reply!r}"
            )


# ── Basic LLM connectivity ───────────────────────────────────────────────────

def test_agent_chat_returns_meaningful_response():
    """Simplest smoke test: one message in, one meaningful message out."""
    agent = _make_agent()
    reply = agent.chat("Respond with exactly: HELLO 42")

    _assert_healthy_reply(reply, "basic chat")
    assert "42" in reply, f"Model did not follow instruction to include '42': {reply!r}"
    assert len(reply.strip()) < 200, f"Reply too long for simple prompt: len={len(reply)}"


def test_agent_chat_understands_chinese():
    """Gateway must handle Chinese-language prompts correctly."""
    agent = _make_agent()
    reply = agent.chat("请用中文回复：你好世界")

    _assert_healthy_reply(reply, "chinese chat")
    assert any(
        c in reply for c in ("你好", "世界", "您好")
    ), f"No Chinese greeting in reply: {reply!r}"


def test_agent_chat_responds_without_tools():
    """With all toolsets disabled, the agent should produce a plain text answer
    without attempting tool calls."""
    agent = _make_agent()
    reply = agent.chat("What is 2+2? Answer with just the number.")

    _assert_healthy_reply(reply, "no-tools chat")
    assert "4" in reply, f"Expected '4' in reply: {reply!r}"


# ── Raw OpenAI SDK connectivity through the same gateway path ─────────────────

def test_raw_openai_client_chat():
    """Make a raw OpenAI SDK call through the same DashScope endpoint used by
    the gateway's `chat_completion_helpers.create_chat_completion` path."""
    import openai

    client = openai.OpenAI(
        api_key=DASHSCOPE_KEY,
        base_url=LIVE_BASE_URL,
    )
    resp = client.chat.completions.create(
        model=LIVE_MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=50,
    )

    assert resp.choices, "No choices in response"
    content = resp.choices[0].message.content or ""
    assert content.strip(), "Empty response content"
    assert "PONG" in content.upper()


# ── Tool execution pipeline ──────────────────────────────────────────────────

def test_agent_tool_execution_roundtrip():
    """Agent with terminal_tool enabled should execute a simple shell command.

    This verifies the tool dispatch pipeline: tool call → execution →
    tool result → model continuation."""
    agent = _make_agent(
        disabled_toolsets=[],
        enabled_toolsets=["terminal"],
    )
    reply = agent.chat(
        "Run `echo hello_from_e2e` and tell me exactly what the output was."
    )

    _assert_healthy_reply(reply, "tool exec")
    assert "hello_from_e2e" in reply, (
        f"Tool execution did not produce expected output in reply: {reply!r}"
    )


# ── Sequential chat stability ────────────────────────────────────────────────

def test_three_sequential_chats_across_client_rebuild():
    """Regression test for #10933: client closed after transport rebuild.

    Turn 1: fresh agent — always worked.
    Turn 2: often failed under #10933.
    Turn 3: post-rebuild — extra insurance.
    """
    agent = _make_agent()

    r1 = agent.chat("Respond with only the word: ONE")
    _assert_healthy_reply(r1, "turn 1")

    r2 = agent.chat("Respond with only the word: TWO")
    _assert_healthy_reply(r2, "turn 2")

    rebuilt = agent._replace_primary_openai_client(reason="e2e_test_rebuild")
    assert rebuilt, "rebuild returned False"

    r3 = agent.chat("Respond with only the word: THREE")
    _assert_healthy_reply(r3, "turn 3 (post-rebuild)")


# ── Gateway config reload + chat ─────────────────────────────────────────────

def test_agent_chat_after_config_reload():
    """Chat works after a simulated config reload (provider re-resolution)."""
    agent = _make_agent()

    r1 = agent.chat("Respond with only the word: ALPHA")
    _assert_healthy_reply(r1, "pre-reload chat")

    agent._replace_primary_openai_client(reason="simulated_config_reload")

    r2 = agent.chat("Respond with only the word: BETA")
    _assert_healthy_reply(r2, "post-reload chat")