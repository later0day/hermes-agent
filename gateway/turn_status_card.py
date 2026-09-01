"""Turn-scoped status-card coordinator for gateway platforms.

This is presentation-only state. It consumes agent callback events, keeps a
compact per-turn view of assistant progress + tool lifecycle, and updates one
editable platform message/card. It does not mutate agent history, prompts, or
tool schemas.
"""

from __future__ import annotations

import inspect
import logging
import queue
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.turn_status_card")

_DONE = object()


# -----------------------------------------------------------------
# Per-tool emoji subclassification — picks a more specific glyph
# for high-volume tools so a row of N terminal calls or N file
# reads isn't a wall of the same icon.
#
# Resolution order in ``_resolve_tool_emoji`` is:
#   1. Skin override on the tool name (highest priority — user
#      preference wins).
#   2. Subclassified emoji for ``terminal`` / ``read_file`` based on
#      the call's preview text.
#   3. Tool-registry default emoji.
#   4. ⚡ fallback.
# Only ``terminal`` and ``read_file`` get subclassified today; both
# fire dozens of times per turn for typical sessions and benefit
# the most from a richer vocabulary.
# -----------------------------------------------------------------

_TERMINAL_COMMAND_EMOJI: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*git(\s|$)"), "🌳"),
    (re.compile(r"^\s*(pytest|unittest|jest|vitest|mocha|cargo\s+test|go\s+test|mvn\s+test|npm\s+test|pnpm\s+test|yarn\s+test)\b"), "🧪"),
    (re.compile(r"^\s*(pip|pip3|uv|pnpm|yarn|npm|cargo|brew|apt|apt-get)\s+(install|add|i|sync|update|upgrade)\b"), "📦"),
    (re.compile(r"^\s*(docker|docker-compose|podman|kubectl|helm|nerdctl)\b"), "🐳"),
    (re.compile(r"^\s*(curl|wget|http|httpie)\b"), "🌐"),
    (re.compile(r"^\s*(ls|find|tree|fd|locate)\b"), "🗂️"),
    (re.compile(r"^\s*(cat|head|tail|less|more|bat)\b"), "👀"),
    (re.compile(r"^\s*(grep|rg|ripgrep|ag|ack)\b"), "🔍"),
    (re.compile(r"^\s*(rm|mv|cp|chmod|chown|mkdir|touch|ln)\b"), "🧹"),
    (re.compile(r"^\s*(ps|kill|top|htop|lsof|netstat|ss|jobs|launchctl|systemctl)\b"), "🔧"),
    (re.compile(r"^\s*(python|python3|node|deno|ruby|bash|sh|zsh|fish|tsx|ts-node)\b"), "▶️"),
    (re.compile(r"^\s*(make|cmake|gradle|mvn|bazel|just)\b"), "🔨"),
    (re.compile(r"^\s*(echo|printf|pwd|whoami|date|env|export|source)\b"), "🪶"),
]

_READ_FILE_EXT_EMOJI: dict[str, str] = {
    # Python
    ".py": "🐍",
    ".pyi": "🐍",
    # TypeScript — brand blue
    ".ts": "🔷",
    ".tsx": "🔷",
    # JavaScript — brand yellow
    ".js": "🟡",
    ".jsx": "🟡",
    ".mjs": "🟡",
    ".cjs": "🟡",
    # Docs / prose
    ".md": "📝",
    ".rst": "📝",
    ".txt": "📝",
    # Config / data
    ".yaml": "⚙️",
    ".yml": "⚙️",
    ".toml": "⚙️",
    ".ini": "⚙️",
    ".cfg": "⚙️",
    ".conf": "⚙️",
    ".json": "🗂️",
    ".xml": "🏷️",
    ".csv": "📊",
    # Web
    ".html": "🌐",
    ".htm": "🌐",
    ".css": "🎨",
    ".scss": "🎨",
    ".sass": "🎨",
    ".less": "🎨",
    # Frameworks
    ".vue": "💚",
    ".svelte": "🔥",
    # Systems / compiled
    ".go": "🐹",
    ".rs": "🦀",
    ".c": "⚙️",
    ".h": "⚙️",
    ".cpp": "⚙️",
    ".cc": "⚙️",
    ".cxx": "⚙️",
    ".hpp": "⚙️",
    ".zig": "⚡",
    ".wasm": "🧩",
    # JVM
    ".java": "☕",
    ".kt": "🟣",
    ".kts": "🟣",
    ".scala": "🔴",
    ".sc": "🔴",
    ".clj": "🔵",
    ".cljs": "🔵",
    # .NET
    ".cs": "🎵",
    ".fs": "🎵",
    ".fsi": "🎵",
    ".vb": "🎵",
    # Ruby
    ".rb": "💎",
    ".erb": "💎",
    # Shell — shell = 贝壳
    ".sh": "🐚",
    ".bash": "🐚",
    ".zsh": "🐚",
    ".fish": "🐚",
    # Apple
    ".swift": "🐦",
    ".m": "🍎",
    ".mm": "🍎",
    # Web / mobile scripting
    ".php": "🐘",
    ".dart": "🎯",
    ".lua": "🌙",
    # Functional
    ".ex": "⚗️",
    ".exs": "⚗️",
    ".erl": "📡",
    ".hrl": "📡",
    ".hs": "λ",
    ".lhs": "λ",
    ".ml": "🐫",
    ".mli": "🐫",
    ".pl": "🐪",
    ".pm": "🐪",
    ".jl": "💜",
    # Data / analytics
    ".r": "📈",
    ".R": "📈",
    ".ipynb": "🪐",
    ".sql": "🗃️",
    # Infra / cloud
    ".tf": "🌍",
    ".tfvars": "🌍",
    ".nix": "❄️",
    ".proto": "📡",
    ".graphql": "🔮",
    ".gql": "🔮",
    # Web3
    ".sol": "💎",
    # Meta
    ".log": "📋",
    ".lock": "🔒",
    ".env": "🔑",
    ".pem": "🔑",
    ".key": "🔑",
    ".cert": "🔐",
    ".crt": "🔐",
}

_READ_FILE_BASENAME_EMOJI: dict[str, str] = {
    # Containers
    "dockerfile": "🐳",
    "docker-compose.yml": "🐳",
    "docker-compose.yaml": "🐳",
    ".dockerignore": "🐳",
    # Build
    "makefile": "🔨",
    "cmakelists.txt": "🔨",
    "build.gradle": "🐘",
    "build.gradle.kts": "🟣",
    "pom.xml": "☕",
    # Ruby
    "rakefile": "💎",
    "gemfile": "💎",
    "gemfile.lock": "🔒",
    # Python ecosystem
    "pipfile": "🐍",
    "pipfile.lock": "🔒",
    "pyproject.toml": "🐍",
    "setup.py": "🐍",
    "setup.cfg": "🐍",
    "requirements.txt": "🐍",
    # Node ecosystem
    "package.json": "📦",
    "package-lock.json": "🔒",
    "yarn.lock": "🔒",
    "pnpm-lock.yaml": "🔒",
    "tsconfig.json": "🔷",
    "vite.config.ts": "⚡",
    "vite.config.js": "⚡",
    "webpack.config.js": "📦",
    "jest.config.js": "🧪",
    "jest.config.ts": "🧪",
    ".eslintrc": "🔍",
    ".eslintrc.json": "🔍",
    ".prettierrc": "🎨",
    # Go ecosystem
    "go.mod": "🐹",
    "go.sum": "🐹",
    # Rust ecosystem
    "cargo.toml": "🦀",
    "cargo.lock": "🔒",
    # Git
    ".gitignore": "🌳",
    ".gitattributes": "🌳",
    ".gitmodules": "🌳",
    # Env / secrets
    ".env": "🔑",
    ".env.local": "🔑",
    ".env.example": "🔑",
    # Docs
    "readme": "📖",
    "readme.md": "📖",
    "license": "⚖️",
    "license.md": "⚖️",
    "changelog": "📝",
    "changelog.md": "📝",
    # System
    "procfile": "📜",
    "vagrantfile": "📜",
}


def _terminal_emoji_for_command(preview: str) -> str:
    """Pick a command-aware emoji for a ``terminal`` call.

    Falls back to 💻 for commands not in the table. The table covers
    the patterns we see most often in real sessions (git, package
    managers, test runners, containers, file utilities) so a row of
    mixed commands becomes visually distinguishable at a glance.
    """
    if not preview:
        return "💻"
    for pattern, emoji in _TERMINAL_COMMAND_EMOJI:
        if pattern.match(preview):
            return emoji
    return "💻"


def _read_file_emoji_for_path(preview: str) -> str:
    """Pick an extension/basename-aware emoji for a ``read_file`` call."""
    if not preview:
        return "📖"
    path = preview.strip().strip('"').strip("'")
    base = path.rsplit("/", 1)[-1].lower()
    special = _READ_FILE_BASENAME_EMOJI.get(base)
    if special:
        return special
    if "." not in base:
        return "📖"
    ext = "." + base.rsplit(".", 1)[-1]
    return _READ_FILE_EXT_EMOJI.get(ext, "📖")


@dataclass
class ToolStatusEntry:
    key: str
    name: str
    preview: str = ""
    status: str = "running"
    duration: Optional[float] = None
    is_error: bool = False
    started_at: float = 0.0
    # Compact error summary (≤100 chars) extracted from the tool
    # result. Only populated when ``is_error=True``. Rendered after the
    # preview on failure lines so the user does not need to dig through
    # logs to see what went wrong.
    error_summary: str = ""


@dataclass
class TurnStatusCardConfig:
    edit_interval: float = 0.5
    preview_max_len: int = 40
    assistant_preview_max_len: int = 900
    max_tools: int = 12
    # Activate the status card after this many streaming-delta characters
    # accumulate, even if no tool / commentary / reasoning event has
    # arrived yet. Keeps short answers card-free while still giving long
    # streaming-only answers a "working on it" indicator. Set to 0 to
    # disable delta-driven activation entirely.
    delta_activation_threshold: int = 200
    # Max characters of the tool error summary appended to failed
    # tool lines. Keeps the card compact while still telling the
    # user "what went wrong" without forcing a log dive.
    error_summary_max_len: int = 100


class TurnStatusCardCoordinator:
    """Maintain and update one live status card for an agent turn."""

    _MEDIA_RE = re.compile(r"(?:\[\[audio_as_voice\]\]\s*)?MEDIA:\S+")

    # Single growing-dot spinner — 4 frames, one dot that gets progressively bigger.
    # · → • → ● → ⬤ conveys "building up" without the clutter of accumulating chars.
    _SPINNER: list[str] = ["·", "•", "●", "⬤"]

    def __init__(
        self,
        *,
        adapter: Any,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        config: Optional[TurnStatusCardConfig] = None,
    ) -> None:
        self.adapter = adapter
        self.chat_id = chat_id
        self.metadata = dict(metadata or {})
        self.config = config or TurnStatusCardConfig()
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._tools: Dict[str, ToolStatusEntry] = {}
        self._tool_order: list[str] = []
        self._assistant_text = ""
        self._commentary = ""
        self._message_id: Optional[str] = None
        self._active = False
        self._dirty = False
        self._finished = False
        self._disabled = False
        self._last_flush = 0.0
        self._fallback_counter = 0
        self._edit_accepts_metadata: Optional[bool] = None
        # Animation frame counter — incremented on every _render(), wraps at 12
        self._frame: int = 0
        # True when the only activation signal so far has been streaming
        # deltas. Used to avoid echoing the answer text into the status
        # card while the final-answer card has not yet been delivered.
        # Reverts to False as soon as a tool / commentary / reasoning
        # event arrives.
        self._streaming_only_activation = False

    @property
    def message_id(self) -> Optional[str]:
        return self._message_id

    @property
    def active(self) -> bool:
        return self._active

    def on_delta(self, text: Optional[str]) -> None:
        if text is None:
            self._queue.put(("segment",))
            return
        if text:
            self._queue.put(("delta", str(text)))

    def on_commentary(self, text: str) -> None:
        if text:
            self._queue.put(("commentary", str(text)))

    def on_tool_progress(
        self,
        event_type: str,
        tool_name: Optional[str] = None,
        preview: Optional[str] = None,
        args: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        self._queue.put((
            "tool",
            event_type,
            tool_name,
            preview,
            args,
            dict(kwargs),
        ))

    def finish(self) -> None:
        if self._finished:
            return
        self._queue.put(_DONE)

    async def run(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                if self._should_flush():
                    await self._flush(finalize=False)
                await self._sleep()
                continue

            if item is _DONE:
                self._finished = True
                self._drain_pending()
                if self._active:
                    await self._flush(finalize=True, force=True)
                return

            self._apply(item)
            if self._should_flush(immediate=self._message_id is None):
                await self._flush(finalize=False)

    async def _sleep(self) -> None:
        import asyncio

        await asyncio.sleep(0.1)

    def _drain_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _DONE:
                continue
            self._apply(item)

    def _apply(self, item: Any) -> None:
        if self._disabled:
            return
        kind = item[0] if isinstance(item, tuple) and item else None
        if kind == "delta":
            self._assistant_text += item[1]
            # Delta-driven activation: long streaming-only answers get a
            # "working on it" status card so the user is not staring at
            # silence between submit and the final-answer card.
            threshold = int(self.config.delta_activation_threshold or 0)
            if (
                not self._active
                and threshold > 0
                and len(self._assistant_text) >= threshold
            ):
                self._active = True
                self._streaming_only_activation = True
            if self._active:
                self._dirty = True
            return
        if kind == "commentary":
            self._commentary = item[1]
            self._active = True
            self._streaming_only_activation = False
            self._dirty = True
            return
        if kind == "segment":
            if self._active:
                self._dirty = True
            return
        if kind == "tool":
            _, event_type, tool_name, preview, args, kwargs = item
            self._apply_tool_event(event_type, tool_name, preview, args, kwargs)

    def _apply_tool_event(
        self,
        event_type: str,
        tool_name: Optional[str],
        preview: Optional[str],
        args: Optional[dict],
        kwargs: Dict[str, Any],
    ) -> None:
        if event_type == "tool.started":
            if not tool_name:
                return
            key = self._tool_key(tool_name, kwargs)
            entry = self._tools.get(key)
            if entry is None:
                entry = ToolStatusEntry(
                    key=key,
                    name=str(tool_name),
                    started_at=time.monotonic(),
                )
                self._tools[key] = entry
                self._tool_order.append(key)
            entry.preview = self._preview_text(preview, args)
            entry.status = "running"
            entry.is_error = False
            entry.duration = None
            self._active = True
            self._streaming_only_activation = False
            self._dirty = True
            return

        if event_type == "tool.completed":
            if not tool_name:
                return
            key = self._find_tool_key(tool_name, kwargs)
            if key is None:
                key = self._tool_key(tool_name, kwargs)
                self._tools[key] = ToolStatusEntry(
                    key=key,
                    name=str(tool_name),
                    started_at=time.monotonic(),
                )
                self._tool_order.append(key)
            entry = self._tools[key]
            entry.status = "failed" if bool(kwargs.get("is_error")) else "completed"
            entry.is_error = bool(kwargs.get("is_error"))
            duration = kwargs.get("duration")
            if isinstance(duration, (int, float)):
                entry.duration = float(duration)
            if entry.is_error:
                entry.error_summary = self._extract_error_summary(
                    kwargs.get("result"),
                )
            self._active = True
            self._streaming_only_activation = False
            self._dirty = True
            return

        if event_type in {"_thinking", "reasoning.available"}:
            text = preview or tool_name or ""
            if text:
                self._commentary = str(text)
                self._active = True
                self._streaming_only_activation = False
                self._dirty = True

    def _tool_key(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        key = self._identity_tool_key(kwargs)
        if key is not None:
            return key
        self._fallback_counter += 1
        return f"fallback:{self._fallback_counter}:{tool_name}"

    def _identity_tool_key(self, kwargs: Dict[str, Any]) -> Optional[str]:
        scope = kwargs.get("subagent_id") or kwargs.get("child_session_id") or ""
        scope_prefix = f"{scope}:" if scope else ""
        call_id = kwargs.get("tool_call_id") or kwargs.get("call_id")
        if call_id:
            return f"{scope_prefix}call:{call_id}"
        index = kwargs.get("index")
        if index is not None:
            return f"{scope_prefix}idx:{index}"
        return None

    def _find_tool_key(self, tool_name: str, kwargs: Dict[str, Any]) -> Optional[str]:
        key = self._identity_tool_key(kwargs)
        if key is not None and key in self._tools:
            return key
        for candidate in reversed(self._tool_order):
            entry = self._tools.get(candidate)
            if entry and entry.name == tool_name and entry.status == "running":
                return candidate
        for candidate in reversed(self._tool_order):
            entry = self._tools.get(candidate)
            if entry and entry.name == tool_name:
                return candidate
        return None

    def _extract_error_summary(self, result: Any) -> str:
        """Pull a short error line out of a tool result for the failure row.

        Tool results are typically JSON-encoded by the registry's
        error-wrapping path (``{"success": False, "error": "..."}``)
        or plain strings for legacy tools. We try the JSON ``error``
        field first, then fall back to the raw text, and finally
        compact + cap the result.
        """
        if result is None:
            return ""
        text = ""
        if isinstance(result, str):
            stripped = result.strip()
            if stripped.startswith("{"):
                try:
                    import json as _json
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        err = parsed.get("error") or parsed.get("message") or ""
                        if err:
                            text = str(err)
                except Exception:
                    text = stripped
            if not text:
                text = stripped
        elif isinstance(result, dict):
            text = str(
                result.get("error") or result.get("message") or result,
            )
        else:
            text = str(result)
        text = re.sub(r"\s+", " ", text).strip()
        cap = int(self.config.error_summary_max_len or 0)
        if cap > 0 and len(text) > cap:
            text = text[: max(0, cap - 1)] + "…"
        return text

    def _preview_text(self, preview: Optional[str], args: Optional[dict]) -> str:
        text = str(preview or "")
        if not text and isinstance(args, dict) and args:
            keys = ", ".join(str(k) for k in list(args.keys())[:4])
            text = f"args: {keys}"
        text = re.sub(r"\s+", " ", text).strip()
        cap = int(self.config.preview_max_len or 0)
        if cap > 0 and len(text) > cap:
            text = text[: max(0, cap - 3)] + "..."
        return text

    def _compact_text(self, text: str, cap: int) -> str:
        text = self._MEDIA_RE.sub("", text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if cap > 0 and len(text) > cap:
            text = "..." + text[-max(0, cap - 3):]
        return text


    def _render(self) -> str:
        # Advance animation frame on every render call
        self._frame = (self._frame + 1) % len(self._SPINNER)
        now = time.monotonic()

        lines: list[str] = []
        lines.append("**进度**")
        lines.append(self._status_line())

        if self._tool_order:
            lines.append("")
            # Show a spinner next to the header while any tool is still running
            running_any = any(
                self._tools[k].status == "running" for k in self._tool_order
            )
            header_spinner = f" {self._SPINNER[self._frame]}" if running_any else ""
            lines.append(f"**🛠 工具**{header_spinner}")
            order = self._tool_order[-max(1, int(self.config.max_tools or 12)):]
            hidden = max(0, len(self._tool_order) - len(order))
            if hidden:
                lines.append(f"- … 省略较早的 {hidden} 个工具")
            for key in order:
                lines.append(self._render_tool_line(self._tools[key], self._frame, now))
        return "\n".join(lines).strip()

    @staticmethod
    def _resolve_tool_emoji(entry: ToolStatusEntry) -> str:
        """Pick the per-tool emoji for a status-card row.

        Resolution order:

        1. Active skin override on the tool name (user preference wins).
        2. Subclassified emoji for ``terminal`` / ``read_file`` based
           on the call's preview text — breaks the "wall of identical
           icons" problem for the two highest-volume tools.
        3. Tool-registry default (``registry.get_emoji``).
        4. ⚡ fallback (matches ``get_tool_emoji``'s documented default).

        Lazy import keeps this module importable in non-CLI / non-gateway
        contexts.
        """
        try:
            from agent.display import _get_skin
            skin = _get_skin()
            if skin and getattr(skin, "tool_emojis", None):
                override = skin.tool_emojis.get(entry.name)
                if override:
                    return override
        except Exception:  # noqa: BLE001 - presentation must never raise
            pass
        if entry.name == "terminal":
            return _terminal_emoji_for_command(entry.preview)
        if entry.name == "read_file":
            return _read_file_emoji_for_path(entry.preview)
        try:
            from tools.registry import registry
            emoji = registry.get_emoji(entry.name, default="")
            if emoji:
                return emoji
        except Exception:  # noqa: BLE001
            pass
        return "⚡"

    @classmethod
    def _render_tool_line(
        cls,
        entry: ToolStatusEntry,
        frame: int = 0,
        now: Optional[float] = None,
    ) -> str:
        # Single dot grows · → • → ● → ⬤ to show activity without clutter.
        if entry.status == "running":
            icon = cls._SPINNER[frame % len(cls._SPINNER)]
        elif entry.status == "failed":
            icon = "❌"
        else:
            icon = "✅"
        tool_emoji = cls._resolve_tool_emoji(entry)
        parts: list[str] = [f"{icon} {tool_emoji} `{entry.name}`"]
        if entry.preview:
            parts.append(entry.preview)
        if entry.status == "running":
            if now is not None and entry.started_at:
                elapsed = now - entry.started_at
                parts.append(f"{elapsed:.1f}s…")
        elif entry.duration is not None:
            parts.append(f"{entry.duration:.1f}s")
        line = f"- {' · '.join(parts)}"
        if entry.status == "failed" and entry.error_summary:
            line += f"  ← {entry.error_summary}"
        return line

    def _status_line(self) -> str:
        if self._finished:
            return self._final_summary_line()
        if self._commentary:
            return self._compact_text(
                self._commentary, self.config.assistant_preview_max_len,
            )
        # Pure streaming-only activation: don't echo the answer text into
        # the status card, because the final-answer card will deliver the
        # full content. Showing it twice is the duplication the user
        # called out as "reading difficulty".
        if self._streaming_only_activation and not self._tool_order:
            return "⌛ 正在生成回答..."
        if self._assistant_text:
            return self._compact_text(
                self._assistant_text, self.config.assistant_preview_max_len,
            )
        return "工作中..."

    def _final_summary_line(self) -> str:
        n_tools = len(self._tool_order)
        n_failed = sum(
            1 for entry in self._tools.values() if entry.status == "failed"
        )
        if n_tools == 0:
            return "✅ 答案见下方"
        total_duration = 0.0
        for entry in self._tools.values():
            if entry.duration is not None:
                total_duration += float(entry.duration)
        # Prefix reflects the worst tool outcome; the card body still
        # lists each tool's status individually.
        prefix = "⚠️" if n_failed else "✅"
        bits = [f"{n_tools} 工具"]
        if n_failed:
            bits.append(f"{n_failed} 失败")
        if total_duration > 0:
            bits.append(f"{total_duration:.1f}s")
        bits.append("答案见下方")
        return f"{prefix} {' · '.join(bits)}"

    def _should_flush(self, *, immediate: bool = False) -> bool:
        if self._disabled:
            return False
        if not self._active or not self._dirty:
            return False
        if immediate:
            return True
        return (time.monotonic() - self._last_flush) >= max(
            0.1, float(self.config.edit_interval or 0.5),
        )

    async def _flush(self, *, finalize: bool, force: bool = False) -> None:
        if self._disabled:
            return
        if not self._active or (not self._dirty and not force):
            return
        content = self._render()
        try:
            if self._message_id is None:
                metadata = dict(self.metadata)
                metadata["expect_edits"] = True
                result = await self.adapter.send(
                    self.chat_id,
                    content,
                    metadata=metadata or None,
                )
                if not getattr(result, "success", False):
                    logger.debug("turn status card send failed: %s", getattr(result, "error", ""))
                    if not getattr(result, "retryable", False):
                        self._disabled = True
                        self._dirty = False
                    return
                message_id = getattr(result, "message_id", None)
                if message_id:
                    self._message_id = str(message_id)
                    logger.info(
                        "turn status card created: chat=%s message=%s tools=%d",
                        self.chat_id,
                        self._message_id,
                        len(self._tool_order),
                    )
            else:
                kwargs = {
                    "chat_id": self.chat_id,
                    "message_id": self._message_id,
                    "content": content,
                    "finalize": finalize,
                }
                if self.metadata and self._edit_message_accepts_metadata():
                    kwargs["metadata"] = self.metadata
                result = await self.adapter.edit_message(**kwargs)
                if not getattr(result, "success", False):
                    logger.debug("turn status card edit failed: %s", getattr(result, "error", ""))
                    if not getattr(result, "retryable", False):
                        self._disabled = True
                        self._dirty = False
                    return
                logger.info(
                    "turn status card %s: chat=%s message=%s tools=%d",
                    "finalized" if finalize else "updated",
                    self.chat_id,
                    self._message_id,
                    len(self._tool_order),
                )
            self._dirty = False
            self._last_flush = time.monotonic()
        except Exception:
            logger.debug("turn status card flush failed", exc_info=True)

    def _edit_message_accepts_metadata(self) -> bool:
        if self._edit_accepts_metadata is not None:
            return self._edit_accepts_metadata
        try:
            params = inspect.signature(self.adapter.edit_message).parameters
            self._edit_accepts_metadata = (
                "metadata" in params
                or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
            )
        except (TypeError, ValueError):
            self._edit_accepts_metadata = False
        return self._edit_accepts_metadata


__all__ = [
    "ToolStatusEntry",
    "TurnStatusCardConfig",
    "TurnStatusCardCoordinator",
]
