"""DingTalk adapter: stage-aware emoji reactions on the user's message: Thinking → tool stage → turn outcome.

Mixed into :class:`~plugins.platforms.dingtalk.adapter.DingTalkAdapter`; SDK modules are read through
the adapter module (``_dt()``) so the facade stays the one patch seam."""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from agent.i18n import t


logger = logging.getLogger(__name__)

# Extract ``[[emotion:...]]``/``[[dingtalk:emotion=...]]`` tags from agent output so the adapter
# can fire them as DingTalk reactions instead of rendering them as text.
_DINGTALK_EMOTION_TAG_RE = re.compile(
    r"\[\[(?:dingtalk[:_-])?emotion\s*[:=]\s*([^\]]+?)\s*\]\]",
    re.IGNORECASE,
)
_EMOTION_ID = "2659900"
_EMOTION_BG = "im_bg_1"
# recall? -> (TextEmotion model, Request model, Headers model, robot SDK method), resolved on ``dingtalk_robot_models`` at call time.
_EMOTION_SDK = {recall: (f"Robot{v}EmotionRequestTextEmotion", f"Robot{v}EmotionRequest", f"Robot{v}EmotionHeaders", f"robot_{v.lower()}_emotion_with_options_async")
                for recall, v in ((True, "Recall"), (False, "Reply"))}


def _dt():
    from plugins.platforms.dingtalk import adapter
    return adapter


class DingTalkReactionsMixin:
    # -- Stage-aware emoji reaction labels ----------------------------------
    # Default Thinking / Done / Error / Interrupted reactions fired on the
    # original user message.  ``notify_tool_started`` swaps Thinking for a
    # category-specific label; ``_fire_done_reaction`` recalls whatever label
    # was last fired so the final reaction lands on the correct anchor.
    # Texts live in this plugin's language pack (``locales/``) under dingtalk.*.
    @property
    def REACTION_THINKING(self) -> str:  # noqa: N802
        return t("dingtalk.reaction.thinking")

    @property
    def REACTION_DONE(self) -> str:  # noqa: N802
        return t("dingtalk.reaction.done")

    @property
    def REACTION_ERROR(self) -> str:  # noqa: N802
        return t("dingtalk.reaction.error")

    @property
    def REACTION_INTERRUPTED(self) -> str:  # noqa: N802
        return t("dingtalk.reaction.interrupted")

    # Tool -> broad stage (a dingtalk.stage.* key). Categories are coarse on purpose:
    # back-to-back terminal calls or back-to-back file reads should
    # not produce a flicker of swaps, only the FIRST call in a new
    # category triggers a label change. Tools missing from this map
    # do not swap the label (the previous stage label stays).
    _TOOL_STAGES: Dict[str, str] = {
        "terminal":           "command",
        "code_execution":     "command",
        "execute_code":       "code",
        "read_file":          "read",
        "write_file":         "write",
        "patch":              "edit",
        "search_files":       "find_files",
        "web_search":         "search",
        "web_extract":        "fetch",
        **dict.fromkeys((
            "browser_navigate", "browser_click", "browser_type", "browser_screenshot", "browser_back",
            "browser_scroll", "browser_press", "browser_vision", "browser_console", "browser_get_images",
        ), "browse"),
        "memory":             "memory",
        "delegate_task":      "delegate",
        "todo":               "todo",
        "clarify":            "clarify",
        "skill_manage":       "skill",
        "vision":             "vision",
        "image_generation":   "image",
        "video_generation":   "video",
    }

    # Terminal command -> more specific stage. Matched in order; first hit wins.
    _TERMINAL_STAGES: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"^\s*git\b"),                          "git"),
        (re.compile(r"^\s*(pytest|unittest|jest|vitest|mocha|cargo\s+test|go\s+test|npm\s+test|pnpm\s+test)\b"), "test"),
        (re.compile(r"^\s*(pip|pip3|uv|npm|pnpm|yarn|cargo|brew|apt|apt-get)\s+(install|add|i|sync)\b"), "install"),
        (re.compile(r"^\s*(docker|docker-compose|kubectl|helm)\b"),  "container"),
        (re.compile(r"^\s*(curl|wget|http)\b"),             "http"),
        (re.compile(r"^\s*(grep|rg|ripgrep|ag)\b"),         "search"),
        (re.compile(r"^\s*(python|python3|node|deno|ruby|bash|sh|tsx|ts-node)\b"), "script"),
        (re.compile(r"^\s*(make|cmake|cargo\s+build|go\s+build|mvn)\b"), "build"),
        (re.compile(r"^\s*(cat|head|tail|bat)\b"),           "read"),
        (re.compile(r"^\s*(ls|find|tree|fd)\b"),             "list"),
    ]

    def _fire_done_reaction(self, chat_id: str) -> None:
        """Swap the in-flight reaction for the turn outcome.

        Reads the pending reply state set by the gateway runner via
        :meth:`set_pending_reply_state`. Defaults to "success" so this
        is safe to call even when nothing set the state.
        """
        if chat_id in self._done_emoji_fired:
            return
        self._done_emoji_fired.add(chat_id)
        self._swap_final_reaction(chat_id, self._pending_reply_state.pop(chat_id, "success"))

    def _swap_final_reaction(self, chat_id: str, state: str) -> None:
        """Replace the label currently on the user's message with the one for ``state``."""
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return
        final_label = {"error": self.REACTION_ERROR, "interrupted": self.REACTION_INTERRUPTED}.get(
            state, self.REACTION_DONE)

        async def _swap() -> None:
            lock = self._stage_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                current = self._final_reaction_label.pop(chat_id, None) or self._current_stage_label.pop(
                    chat_id, self.REACTION_THINKING,
                )
                await self._send_emotion(
                    msg_id, conversation_id, current, recall=True,
                )
                await self._send_emotion(
                    msg_id, conversation_id, final_label, recall=False,
                )
                self._final_reaction_label[chat_id] = final_label

        self._spawn_bg(_swap())

    def set_pending_reply_state(self, chat_id: str, state: str) -> None:
        """Record the outcome of the agent run for the next final send.

        Called by the gateway runner once it knows whether the turn
        succeeded, failed, or was interrupted. The next ``send()`` that
        triggers a final reaction reads this and picks the matching
        completion label. Unknown / unset states fall back to
        ``"success"`` so we never silently lose the Done reaction.

        Valid states: ``"success"``, ``"error"``, ``"interrupted"``.
        """
        if not chat_id:
            return
        if state not in ("success", "error", "interrupted"):
            state = "success"
        if chat_id not in self._done_emoji_fired:
            self._pending_reply_state[chat_id] = state
        elif state != "success":
            # A streamed reply's final edit already fired the default Done before the runner knew
            # the outcome; correct it now rather than leave the state to mislabel the next turn.
            self._swap_final_reaction(chat_id, state)

    @classmethod
    def _stage_label_for_tool(
        cls, tool_name: Optional[str], preview: str = "",
    ) -> Optional[str]:
        """Return the stage label for *tool_name*, or None to keep current label.

        For ``terminal`` calls, *preview* (the command string) is used to
        pick a more specific label from ``_TERMINAL_STAGES``.
        """
        if not tool_name:
            return None
        stage = cls._TOOL_STAGES.get(tool_name)
        if tool_name == "terminal" and preview:
            stage = next((st for pattern, st in cls._TERMINAL_STAGES if pattern.match(preview)), stage)
        return t(f"dingtalk.stage.{stage}") if stage else None

    def notify_tool_started(
        self, chat_id: str, tool_name: Optional[str], preview: str = "",
    ) -> None:
        """Optionally swap the in-flight reaction to a stage-aware label.

        Called by the gateway runner on every ``tool.started`` event.
        Looks up a broad category label for the tool and, if the
        category has changed since the last swap on this chat, fires
        a recall+reply pair to update the visible label. Tools not in
        ``_TOOL_STAGES`` (or repeat calls of the same category)
        are no-ops, so a run of 5 back-to-back terminal calls costs
        exactly one swap.
        """
        if not chat_id:
            return
        new_label = self._stage_label_for_tool(tool_name, preview=preview)
        if new_label is None:
            return
        current = self._current_stage_label.get(chat_id, self.REACTION_THINKING)
        if current == new_label:
            return
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _swap() -> None:
            lock = self._stage_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                actual_current = self._current_stage_label.get(
                    chat_id, self.REACTION_THINKING,
                )
                if actual_current == new_label:
                    return
                await self._send_emotion(
                    msg_id, conversation_id, actual_current, recall=True,
                )
                await self._send_emotion(
                    msg_id, conversation_id, new_label, recall=False,
                )
                self._current_stage_label[chat_id] = new_label

        self._spawn_bg(_swap())

    async def _send_emotion(self, open_msg_id: str, open_conversation_id: str, emoji_name: str, *, recall: bool = False) -> None:
        """Add (or recall) an emoji reaction on a message."""
        if not self._robot_sdk or not open_msg_id or not open_conversation_id:
            return
        action = "recall" if recall else "reply"
        try:
            token = await self._get_access_token()
            if not token:
                return
            text_emotion_cls, request_cls, headers_cls, sdk_method = _EMOTION_SDK[recall]
            text_emotion = getattr(_dt().dingtalk_robot_models, text_emotion_cls)(emotion_id=_EMOTION_ID, emotion_name=emoji_name, text=emoji_name, background_id=_EMOTION_BG)
            request = getattr(_dt().dingtalk_robot_models, request_cls)(robot_code=self._robot_code, open_msg_id=open_msg_id, open_conversation_id=open_conversation_id,
                                                                  emotion_type=2, emotion_name=emoji_name, text_emotion=text_emotion)
            await self._sdk_call(getattr(self._robot_sdk, sdk_method), request, getattr(_dt().dingtalk_robot_models, headers_cls), token)
            logger.info("[%s] _send_emotion: %s %s on msg=%s", self.name, action, emoji_name, open_msg_id[:24])
        except Exception:
            logger.debug("[%s] _send_emotion %s failed", self.name, action, exc_info=True)

    @classmethod
    def _extract_emotion_tags(cls, content: str) -> tuple[str, List[str]]:
        """Extract ``[[emotion:...]]``/``[[dingtalk:emotion=...]]`` tags."""
        if not content:
            return content, []
        emotions: List[str] = []

        def _replace(match: re.Match) -> str:
            name = (match.group(1) or "").strip()
            if name:
                emotions.append(name[:64])
            return ""

        cleaned = _DINGTALK_EMOTION_TAG_RE.sub(_replace, content)
        return cleaned.strip(), emotions

    def _fire_custom_reactions(self, chat_id: str, emotion_names: List[str]) -> None:
        """Reply with custom DingTalk text emotions requested in message tags."""
        if not emotion_names:
            return
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _send_all() -> None:
            for emotion_name in emotion_names:
                await self._send_emotion(
                    msg_id, conversation_id, emotion_name, recall=False,
                )

        self._spawn_bg(_send_all())
