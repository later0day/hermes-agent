from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource
from gateway.turn_status_card import (
    TurnStatusCardConfig,
    TurnStatusCardCoordinator,
)


def _register_tool_emoji(name: str, emoji: str) -> None:
    """Register a stub tool entry so get_tool_emoji() resolves to *emoji*.

    Subprocess-per-test isolation means each test gets a fresh tool
    registry, so we re-seed the few tool entries the status-card
    assertions depend on. We register under a synthetic toolset so we
    don't collide with any real toolset name a tool module might bring
    along if something else imports it later.
    """
    from tools.registry import registry
    registry.register(
        name=name,
        toolset="_turn_status_card_test",
        schema={"name": name, "description": "test", "parameters": {}},
        handler=lambda args, **kw: "",
        emoji=emoji,
    )


@pytest.fixture(autouse=True)
def _seed_tool_emojis():
    """Seed the tool emojis the status-card tests rely on.

    Mirrors production: ``terminal`` → 💻, ``read_file`` → 📖. Any
    test that uses a different tool name will fall back to the
    ``get_tool_emoji`` default of ⚡ — exactly what production does
    when an MCP/plugin tool ships without an emoji.

    Note: ``terminal`` and ``read_file`` go through a SUBCLASSIFIER
    in the status card (e.g. `pytest …` → 🧪) so the registered
    emoji only shows when no command/extension pattern matches. The
    ``web_search`` entry is here for tests that need the simple
    "registry → emoji" path without a subclassifier in the way.
    """
    _register_tool_emoji("terminal", "💻")
    _register_tool_emoji("read_file", "📖")
    _register_tool_emoji("web_search", "🔍")


class FakeStatusAdapter:
    def __init__(self):
        self.sends = []
        self.edits = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return SimpleNamespace(success=True, message_id="status-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "content": content,
            "finalize": finalize,
        })
        return SimpleNamespace(success=True, message_id=message_id)


class FailingStatusAdapter(FakeStatusAdapter):
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return SimpleNamespace(success=False, error="editable card unavailable")


async def wait_until(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def make_card(adapter: FakeStatusAdapter) -> TurnStatusCardCoordinator:
    return TurnStatusCardCoordinator(
        adapter=adapter,
        chat_id="chat-1",
        metadata={"thread_id": "thread-1"},
        config=TurnStatusCardConfig(edit_interval=0.01, preview_max_len=24),
    )


@pytest.mark.asyncio
async def test_plain_text_only_does_not_create_status_card():
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_delta("plain response")
    card.finish()
    await task

    assert adapter.sends == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_tool_lifecycle_updates_one_card_and_finalizes():
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_delta("I'll inspect the file first.")
    card.on_tool_progress(
        "tool.started",
        "read_file",
        "gateway/run.py",
        {"path": "gateway/run.py"},
        tool_call_id="call-1",
        index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    card.on_tool_progress(
        "tool.completed",
        "read_file",
        None,
        None,
        duration=0.3,
        is_error=False,
        tool_call_id="call-1",
        index=0,
    )
    await wait_until(lambda: adapter.edits)

    card.finish()
    await task

    assert len(adapter.sends) == 1
    assert all(edit["message_id"] == "status-1" for edit in adapter.edits)
    assert adapter.edits[-1]["finalize"] is True
    final_content = adapter.edits[-1]["content"]
    assert "✅ 1 工具" in final_content
    assert "答案见下方" in final_content
    assert "Answer ready." not in final_content
    # Local "lively card" style: ✅ completed glyph, per-tool emoji
    # subclassified by the preview text (a .py path → 🐍 instead of the
    # generic 📖), "·" separator. Glyph stays as the leading column for
    # status alignment; emoji sits between glyph and tool name.
    assert "✅ 🐍 `read_file` · gateway/run.py · 0.3s" in final_content
    assert "**🛠 工具**" in final_content
    assert adapter.sends[0]["metadata"]["expect_edits"] is True


@pytest.mark.asyncio
async def test_duplicate_tool_names_are_tracked_by_call_id():
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started",
        "terminal",
        "pytest a",
        {"cmd": "pytest a"},
        tool_call_id="call-a",
        index=0,
    )
    card.on_tool_progress(
        "tool.started",
        "terminal",
        "pytest b",
        {"cmd": "pytest b"},
        tool_call_id="call-b",
        index=1,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    card.on_tool_progress(
        "tool.completed",
        "terminal",
        None,
        None,
        duration=1.0,
        is_error=False,
        tool_call_id="call-b",
        index=1,
    )
    card.on_tool_progress(
        "tool.completed",
        "terminal",
        None,
        None,
        duration=2.0,
        is_error=False,
        tool_call_id="call-a",
        index=0,
    )
    card.finish()
    await task

    content = adapter.edits[-1]["content"]
    # `pytest` previews subclassify ``terminal`` to 🧪 (test runner).
    assert content.count("✅ 🧪 `terminal`") == 2
    assert "pytest a · 2.0s" in content
    assert "pytest b · 1.0s" in content


@pytest.mark.asyncio
async def test_delta_below_threshold_does_not_create_card():
    """Short streaming-only answers stay card-free."""
    adapter = FakeStatusAdapter()
    card = TurnStatusCardCoordinator(
        adapter=adapter,
        chat_id="chat-1",
        config=TurnStatusCardConfig(
            edit_interval=0.01,
            preview_max_len=24,
            delta_activation_threshold=200,
        ),
    )
    task = asyncio.create_task(card.run())

    card.on_delta("short answer " * 5)  # ~60 chars, below threshold
    card.finish()
    await task

    assert adapter.sends == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_delta_above_threshold_activates_card_without_echoing_answer():
    """Long streaming-only answers get a status card whose status line
    does NOT echo the answer text (avoiding duplication with the final
    answer card delivered separately).
    """
    adapter = FakeStatusAdapter()
    card = TurnStatusCardCoordinator(
        adapter=adapter,
        chat_id="chat-1",
        config=TurnStatusCardConfig(
            edit_interval=0.01,
            preview_max_len=24,
            delta_activation_threshold=50,
        ),
    )
    task = asyncio.create_task(card.run())

    # Accumulate enough delta to cross the threshold.
    long_payload = (
        "This is a long streaming answer that the user should not see "
        "duplicated between the status card and the final answer card."
    )
    card.on_delta(long_payload)
    await wait_until(lambda: len(adapter.sends) == 1)

    initial_content = adapter.sends[0]["content"]
    assert "**进度**" in initial_content
    assert "正在生成回答" in initial_content
    # The actual streaming text must not bleed into the status card.
    assert long_payload not in initial_content
    assert "long streaming answer" not in initial_content

    card.finish()
    await task

    # Final state: still no tools, summary uses the no-tools form.
    final_content = adapter.edits[-1]["content"]
    assert "答案见下方" in final_content
    assert "工具 ·" not in final_content
    assert long_payload not in final_content


@pytest.mark.asyncio
async def test_tool_event_after_delta_activation_drops_streaming_only_flag():
    """When a tool arrives after delta-driven activation, the status
    line should revert to the normal commentary/preview behavior, not
    stay stuck on '正在生成回答...'.
    """
    adapter = FakeStatusAdapter()
    card = TurnStatusCardCoordinator(
        adapter=adapter,
        chat_id="chat-1",
        config=TurnStatusCardConfig(
            edit_interval=0.01,
            preview_max_len=24,
            delta_activation_threshold=20,
        ),
    )
    task = asyncio.create_task(card.run())

    card.on_delta("Reasoning text that crosses the threshold to activate.")
    await wait_until(lambda: len(adapter.sends) == 1)
    assert "正在生成回答" in adapter.sends[0]["content"]

    card.on_tool_progress(
        "tool.started",
        "read_file",
        "src/foo.py",
        {"path": "src/foo.py"},
        tool_call_id="call-1",
        index=0,
    )
    await wait_until(lambda: adapter.edits)

    latest = adapter.edits[-1]["content"]
    assert "正在生成回答" not in latest
    # Running rows use the animated spinner glyph (· → • → ● → ⬤), not a
    # static ▶, so assert on the stable emoji + tool-name suffix.
    assert "🐍 `read_file`" in latest

    card.finish()
    await task


@pytest.mark.asyncio
async def test_finish_is_idempotent():
    """Calling finish() twice must not double-process _DONE."""
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started",
        "read_file",
        "x.py",
        {"path": "x.py"},
        tool_call_id="c1",
        index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    card.finish()
    card.finish()  # second call must be a no-op
    card.finish()  # third call too
    await task

    # Only one finalizing edit should land.
    finalize_edits = [e for e in adapter.edits if e["finalize"]]
    assert len(finalize_edits) == 1


@pytest.mark.asyncio
async def test_running_tool_glyph_and_inline_elapsed():
    """Running tools render with the animated spinner glyph and an
    inline elapsed timer.

    Per the local "lively card" redesign, the running row shows
    ``<spinner> <emoji> `name` · preview · 0.0s…`` where the spinner
    animates · → • → ● → ⬤ and the elapsed timer updates while the
    tool runs (the trailing … marks it as still in-flight).
    """
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started",
        "terminal",
        "long running",
        {"cmd": "long running"},
        tool_call_id="c1",
        index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    initial = adapter.sends[0]["content"]
    # "long running" preview doesn't match any command pattern, so we
    # keep the generic 💻 glyph for an unrecognized terminal command.
    # Assert on the stable emoji + name + preview (the leading spinner
    # glyph animates, so it is not asserted here).
    assert "💻 `terminal` · long running" in initial
    # The running row carries an in-flight elapsed timer suffixed with …
    assert "0.0s…" in initial

    card.finish()
    await task


@pytest.mark.asyncio
async def test_failed_tool_renders_error_summary_after_arrow():
    """Failed tools show ❌ + inline error summary (first 100 chars).

    The failure row format is:
        - ❌ `name` · preview · 0.5s  ← error summary
    """
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started",
        "terminal",
        "ls /missing/path/here",
        {"cmd": "ls /missing/path/here"},
        tool_call_id="c1",
        index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    # Simulate the JSON-wrapped error result that tools/registry.py
    # emits when a tool call fails — this is the most common shape.
    card.on_tool_progress(
        "tool.completed",
        "terminal",
        None,
        None,
        duration=0.5,
        is_error=True,
        result='{"success": false, "error": "ls: /missing/path/here: No such file or directory"}',
        tool_call_id="c1",
        index=0,
    )
    card.finish()
    await task

    final = adapter.edits[-1]["content"]
    # `ls` matches the file-listing pattern → 🗂️ subclassification.
    assert "❌ 🗂️ `terminal` · ls /missing/path/here · 0.5s" in final
    # The error summary lands inline after ← and is extracted from the
    # JSON "error" field, not the raw JSON envelope.
    assert "← ls: /missing/path/here: No such file or directory" in final
    # Final summary uses ⚠️ prefix and reports the failure count.
    assert "⚠️" in final
    assert "1 失败" in final


@pytest.mark.asyncio
async def test_error_summary_is_capped_to_configured_length():
    """Long tool errors get truncated with an ellipsis so the card
    stays compact even when the underlying tool dumps a stack trace.
    """
    adapter = FakeStatusAdapter()
    card = TurnStatusCardCoordinator(
        adapter=adapter,
        chat_id="chat-1",
        config=TurnStatusCardConfig(
            edit_interval=0.01,
            preview_max_len=24,
            error_summary_max_len=40,
        ),
    )
    task = asyncio.create_task(card.run())

    long_error = "A" * 200
    card.on_tool_progress(
        "tool.started", "terminal", "boom", {"cmd": "boom"},
        tool_call_id="c1", index=0,
    )
    card.on_tool_progress(
        "tool.completed", "terminal", None, None,
        duration=0.1, is_error=True,
        result=long_error,
        tool_call_id="c1", index=0,
    )
    card.finish()
    await task

    final = adapter.edits[-1]["content"]
    # Capped at 40 chars (39 chars + ellipsis).
    assert "A" * 40 not in final
    assert "A" * 39 + "…" in final


@pytest.mark.asyncio
async def test_non_retryable_initial_send_failure_disables_card_updates():
    adapter = FailingStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started",
        "terminal",
        "ls",
        {"cmd": "ls"},
        tool_call_id="call-1",
        index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)

    card.on_tool_progress(
        "tool.completed",
        "terminal",
        None,
        None,
        duration=0.2,
        is_error=False,
        tool_call_id="call-1",
        index=0,
    )
    await asyncio.sleep(0.05)
    card.finish()
    await task

    assert len(adapter.sends) == 1
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_registered_tool_emoji_is_rendered_between_glyph_and_name():
    """The per-tool emoji from the registry sits between the status
    glyph and the tool name on every row, so the user can scan the
    tool's identity in a row of completed calls.

    Uses ``web_search`` (no subclassifier) so the assertion checks
    the simple "registry → emoji" path. ``terminal`` / ``read_file``
    have their own subclassification tests below.
    """
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started", "web_search", "kanban roadmap", {"query": "kanban roadmap"},
        tool_call_id="c1", index=0,
    )
    await wait_until(lambda: len(adapter.sends) == 1)
    running = adapter.sends[0]["content"]
    # Running row: animated spinner glyph + emoji + name + preview.
    assert "🔍 `web_search` · kanban roadmap" in running

    card.on_tool_progress(
        "tool.completed", "web_search", None, None,
        duration=0.4, is_error=False,
        tool_call_id="c1", index=0,
    )
    card.finish()
    await task

    final = adapter.edits[-1]["content"]
    assert "✅ 🔍 `web_search` · kanban roadmap · 0.4s" in final


@pytest.mark.asyncio
async def test_terminal_command_subclassification():
    """``terminal`` rows show a command-aware emoji that picks out the
    pattern (git/test/install/container/network/file/...). Repeated
    runs against the same pattern always pick the same emoji so the
    row stays stable; different commands get different emojis so a
    list of mixed commands is scannable.
    """
    cases = [
        ("git status", "🌳"),
        ("pytest tests/", "🧪"),
        ("pip install httpx", "📦"),
        ("docker ps", "🐳"),
        ("curl https://example.com", "🌐"),
        ("ls -la /tmp", "🗂️"),
        ("cat /etc/hosts", "👀"),
        ("rg pattern", "🔍"),
        ("rm -rf node_modules", "🧹"),
        ("ps -ef", "🔧"),
        ("python -m foo", "▶️"),
        ("make build", "🔨"),
        ("totally-unknown-binary --foo", "💻"),
    ]
    for cmd, expected_emoji in cases:
        adapter = FakeStatusAdapter()
        card = make_card(adapter)
        task = asyncio.create_task(card.run())
        card.on_tool_progress(
            "tool.started", "terminal", cmd, {"cmd": cmd},
            tool_call_id=f"c-{cmd}", index=0,
        )
        card.on_tool_progress(
            "tool.completed", "terminal", None, None,
            duration=0.1, is_error=False,
            tool_call_id=f"c-{cmd}", index=0,
        )
        card.finish()
        await task
        final = adapter.edits[-1]["content"]
        assert f"✅ {expected_emoji} `terminal`" in final, (
            f"command {cmd!r} expected {expected_emoji}, got line: {final!r}"
        )


@pytest.mark.asyncio
async def test_read_file_extension_subclassification():
    """``read_file`` rows pick an extension/basename-aware emoji."""
    cases = [
        ("agent/run_agent.py", "🐍"),
        ("web/src/app.tsx", "🔷"),
        ("scripts/lib.js", "🟡"),
        ("README.md", "📖"),
        ("config.yaml", "⚙️"),
        ("data.json", "🗂️"),
        ("styles/main.css", "🎨"),
        ("main.go", "🐹"),
        ("src/lib.rs", "🦀"),
        ("scripts/run.sh", "🐚"),
        ("Dockerfile", "🐳"),
        ("Makefile", "🔨"),
        (".gitignore", "🌳"),
        ("uv.lock", "🔒"),
        ("README_NO_EXT", "📖"),
    ]
    for path, expected_emoji in cases:
        adapter = FakeStatusAdapter()
        card = make_card(adapter)
        task = asyncio.create_task(card.run())
        card.on_tool_progress(
            "tool.started", "read_file", path, {"path": path},
            tool_call_id=f"c-{path}", index=0,
        )
        card.on_tool_progress(
            "tool.completed", "read_file", None, None,
            duration=0.1, is_error=False,
            tool_call_id=f"c-{path}", index=0,
        )
        card.finish()
        await task
        final = adapter.edits[-1]["content"]
        assert f"✅ {expected_emoji} `read_file`" in final, (
            f"path {path!r} expected {expected_emoji}, got line: {final!r}"
        )


@pytest.mark.asyncio
async def test_unregistered_tool_falls_back_to_default_emoji():
    """Plugin/MCP tools without a registered emoji render with ⚡
    (the documented ``get_tool_emoji`` default).
    """
    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started", "unregistered_plugin_tool",
        "some args", {"x": 1},
        tool_call_id="c1", index=0,
    )
    card.on_tool_progress(
        "tool.completed", "unregistered_plugin_tool", None, None,
        duration=0.2, is_error=False,
        tool_call_id="c1", index=0,
    )
    card.finish()
    await task

    final = adapter.edits[-1]["content"]
    assert "✅ ⚡ `unregistered_plugin_tool` · some args · 0.2s" in final


@pytest.mark.asyncio
async def test_skin_override_wins_over_registry_emoji(monkeypatch):
    """When the active skin overrides a tool's emoji, the card uses
    the skin override — this is how users theme the card to match
    the rest of their CLI.
    """
    from agent import display as display_module

    fake_skin = SimpleNamespace(tool_emojis={"terminal": "🦄"}, tool_prefix="┊")
    monkeypatch.setattr(display_module, "_get_skin", lambda: fake_skin)

    adapter = FakeStatusAdapter()
    card = make_card(adapter)
    task = asyncio.create_task(card.run())

    card.on_tool_progress(
        "tool.started", "terminal", "ls", {"cmd": "ls"},
        tool_call_id="c1", index=0,
    )
    card.on_tool_progress(
        "tool.completed", "terminal", None, None,
        duration=0.1, is_error=False,
        tool_call_id="c1", index=0,
    )
    card.finish()
    await task

    final = adapter.edits[-1]["content"]
    assert "✅ 🦄 `terminal`" in final
    assert "💻" not in final


class TurnStatusCaptureAdapter(BasePlatformAdapter):
    SUPPORTS_TURN_STATUS_CARD = True

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DINGTALK)
        self.sent = []
        self.edits = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return SendResult(success=True, message_id="turn-status-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False) -> SendResult:
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "content": content,
            "finalize": finalize,
        })
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class TurnStatusAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        assert cb is not None
        cb(
            "tool.started",
            "read_file",
            "gateway/run.py",
            {"path": "gateway/run.py"},
            tool_call_id="call-read",
            index=0,
        )
        time.sleep(0.02)
        cb(
            "tool.completed",
            "read_file",
            None,
            None,
            duration=0.4,
            is_error=False,
            tool_call_id="call-read",
            index=0,
        )
        cb(
            "tool.started",
            "terminal",
            "pytest tests/gateway/test_turn_status_card.py",
            {"cmd": "pytest tests/gateway/test_turn_status_card.py"},
            tool_call_id="call-test",
            index=1,
        )
        cb(
            "tool.completed",
            "terminal",
            None,
            None,
            duration=1.2,
            is_error=False,
            tool_call_id="call-test",
            index=1,
        )
        return {"final_response": "done", "messages": [], "api_calls": 1}


class SubagentTurnStatusAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        assert cb is not None
        cb("subagent.start", preview="research IP services", subagent_id="sa-1")
        cb(
            "subagent.tool",
            "terminal",
            "curl ifconfig.me",
            {"cmd": "curl ifconfig.me"},
            subagent_id="sa-1",
            tool_call_id="call-subagent-terminal",
            index=0,
        )
        cb(
            "subagent.tool.completed",
            "terminal",
            None,
            None,
            duration=0.6,
            is_error=False,
            subagent_id="sa-1",
            tool_call_id="call-subagent-terminal",
            index=0,
        )
        cb("subagent.complete", preview="research done", subagent_id="sa-1")
        return {"final_response": "done", "messages": [], "api_calls": 1}


class InterimTurnStatusAgent:
    def __init__(self, **kwargs):
        self.tools = []
        self.interim_assistant_callback = None

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.interim_assistant_callback
        assert cb is not None
        cb("I'll inspect the current state first.")
        time.sleep(0.02)
        cb("I found the relevant path and am checking the result.")
        return {"final_response": "done", "messages": [], "api_calls": 1}


def make_gateway_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def install_gateway_fakes(
    monkeypatch,
    tmp_path,
    agent_cls=TurnStatusAgent,
    *,
    env_tool_progress="all",
    dingtalk_display=None,
):
    if env_tool_progress is None:
        monkeypatch.delenv("HERMES_TOOL_PROGRESS_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", env_tool_progress)
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    if dingtalk_display is None:
        dingtalk_display = {
            "tool_progress": "all",
            "streaming": False,
        }
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {
                "interim_assistant_messages": True,
                "platforms": {
                    "dingtalk": dict(dingtalk_display),
                },
            },
        },
    )
    return gateway_run


@pytest.mark.asyncio
async def test_run_agent_uses_turn_status_card_instead_of_progress_bubbles(monkeypatch, tmp_path):
    adapter = TurnStatusCaptureAdapter()
    runner = make_gateway_runner(adapter)
    install_gateway_fakes(monkeypatch, tmp_path)

    source = SessionSource(platform=Platform.DINGTALK, chat_id="chat-1")
    result = await runner._run_agent(
        message="run tests",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:dingtalk:dm:chat-1",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["expect_edits"] is True
    assert adapter.sent[0]["content"].startswith("**进度**")
    assert adapter.edits
    final_edit = adapter.edits[-1]
    assert final_edit["message_id"] == "turn-status-1"
    assert final_edit["finalize"] is True
    assert "✅ 2 工具" in final_edit["content"]
    assert "1.6s" in final_edit["content"]
    assert "答案见下方" in final_edit["content"]
    assert "Answer ready." not in final_edit["content"]
    assert "✅ 🐍 `read_file` · gateway/run.py · 0.4s" in final_edit["content"]
    assert "✅ 🧪 `terminal` · pytest tests/gateway/test_turn_status... · 1.2s" in final_edit["content"]


@pytest.mark.asyncio
async def test_run_agent_maps_subagent_tools_into_turn_status_card(monkeypatch, tmp_path):
    adapter = TurnStatusCaptureAdapter()
    runner = make_gateway_runner(adapter)
    install_gateway_fakes(monkeypatch, tmp_path, agent_cls=SubagentTurnStatusAgent)

    source = SessionSource(platform=Platform.DINGTALK, chat_id="chat-1")
    result = await runner._run_agent(
        message="research IP sites",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:dingtalk:dm:chat-1",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["expect_edits"] is True
    final_edit = adapter.edits[-1]
    assert final_edit["finalize"] is True
    assert "✅ 1 工具" in final_edit["content"]
    assert "答案见下方" in final_edit["content"]
    assert "Answer ready." not in final_edit["content"]
    # `curl` subclassifies terminal to 🌐 (network fetch).
    assert "✅ 🌐 `terminal` · curl ifconfig.me · 0.6s" in final_edit["content"]


@pytest.mark.asyncio
async def test_interim_messages_use_turn_status_card_when_tool_progress_env_is_off(monkeypatch, tmp_path):
    adapter = TurnStatusCaptureAdapter()
    runner = make_gateway_runner(adapter)
    install_gateway_fakes(
        monkeypatch,
        tmp_path,
        agent_cls=InterimTurnStatusAgent,
        env_tool_progress="off",
        dingtalk_display={"streaming": False},
    )

    source = SessionSource(platform=Platform.DINGTALK, chat_id="chat-1")
    result = await runner._run_agent(
        message="check logs",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:dingtalk:dm:chat-1",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["expect_edits"] is True
    assert adapter.sent[0]["content"].startswith("**进度**")
    assert "inspect the current state" in adapter.sent[0]["content"]
    final_edit = adapter.edits[-1]
    assert final_edit["finalize"] is True
    # No tools, just commentary — final summary has no tool count.
    assert "答案见下方" in final_edit["content"]
    assert "Answer ready." not in final_edit["content"]
