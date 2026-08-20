"""In-process Agent Room runner for the dashboard.

The gateway's GatewayRunner._process_message_via_room_if_bound only fires
on IM-inbound messages, and it lives in the gateway PROCESS. The dashboard
runs in a SEPARATE process, so its /api/rooms/{id}/dispatch endpoint used
to fall back to a lightweight aux-LLM path that skipped the observer and
never exercised M1-M4 (no real routing, no decompose, no synthesis, and a
thin history).

This module gives the dashboard the REAL pipeline: it constructs the same
gateway.agent_room_router.AgentRoomRouter with LLM-backed closures built
on a standalone AIAgent, so a dashboard chat message runs the identical
observer -> route/decompose -> member turns -> synthesis flow, persists
every step to the shared agent_room_messages store, and returns the
per-member replies + any synthesized final reply for inline display.

Key differences from the gateway closures (gateway/run.py):
  * No SessionSource / adapter / webhook — replies are returned in the
    HTTP response, not pushed to an IM channel. The ack/edit callables
    are no-ops.
  * LLM turns run via AIAgent.run_conversation in a thread executor
    (run_agent is sync), under the target profile's HERMES_HOME +
    secret scope, exactly like the gateway's _run_agent_inner.
  * Observer decision parsing is delegated to the shared
    gateway.agent_room_observer_parse.parse_observer_decision so the
    qwen text-form fallbacks stay in one place.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Optional

from gateway.agent_room_router import AgentRoomRouter, ClassifierResult
from gateway.agent_room_store import AgentRoom, AgentRoomStore
from gateway.agent_room_messages_store import AgentRoomMessagesStore

logger = logging.getLogger(__name__)


def _profile_home(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    return Path(get_profile_dir(profile))


def _default_home_model() -> str:
    """Model id from the DEFAULT home's config.yaml.

    Member profiles inherit from the default home and carry no config.yaml,
    so their scoped load_config() returns an empty model. We read the
    default home's config directly (bypassing any home override) to supply
    the inherited model id. Credentials still resolve from the member's own
    secret scope.
    """
    try:
        from hermes_constants import get_default_hermes_root
        import yaml
        cfg_path = Path(get_default_hermes_root()) / "config.yaml"
        if not cfg_path.is_file():
            return ""
        data = yaml.safe_load(cfg_path.read_text()) or {}
        model_cfg = data.get("model") or {}
        if isinstance(model_cfg, str):
            return model_cfg
        return str(model_cfg.get("default") or model_cfg.get("model") or "")
    except Exception:
        return ""


# Text-form tool-call markup that some models (Gemini/qwen family) emit as
# PROSE instead of a structured function_call. Even with every tool stripped
# from the member turn (_lock_agent_tools_to_none), the model can still *type*
# these blocks into its reply — they carry no execution (the tools are gone)
# but they leak raw ``<tool_code>{"name":"write_file",...}`` into the room and
# make the member look like it "did work". We defensively strip them from a
# member's reply before it is persisted/returned. This is belt-and-suspenders
# alongside the system-prompt rule that forbids them.
_TOOLCODE_BLOCK_RE = re.compile(
    r"<tool_code>.*?(?:</tool_code>|\Z)", re.DOTALL | re.IGNORECASE
)
# Some models use ```tool_code fenced blocks instead of <tool_code> tags.
_TOOLCODE_FENCE_RE = re.compile(
    r"```(?:tool_code|tool_call|json_tool)\b.*?(?:```|\Z)", re.DOTALL | re.IGNORECASE
)


def _strip_toolcode_text(reply: str) -> str:
    """Remove text-form tool-call markup a conversation-only member should
    never have emitted. Returns the cleaned prose (may be empty if the whole
    reply was a tool-call block, which is itself a signal the turn produced
    no real conversational content)."""
    if not reply or "tool_code" not in reply.lower():
        return reply
    cleaned = _TOOLCODE_BLOCK_RE.sub("", reply)
    cleaned = _TOOLCODE_FENCE_RE.sub("", cleaned)
    # collapse the blank runs the removal leaves behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _read_member_desc(name: str) -> str:
    try:
        from hermes_cli.profiles import read_profile_meta, get_profile_dir
        meta = read_profile_meta(get_profile_dir(name))
        return str(meta.get("description") or "")
    except Exception:
        return ""


def _lock_agent_tools(agent: Any, toolsets: list[str]) -> None:
    """M1.7 parity: present ONLY the given toolsets' RAW tools.

    Two problems this solves:

    1. In a standalone process the room tools may collapse into
       tool_search's meta-tools (tool_search/tool_describe/tool_call)
       because the observer profile's ``tools.tool_search.enabled: off``
       is read from a config cache that ignores set_hermes_home_override.
       So we bypass assembly and fetch the RAW schemas directly via
       ``skip_tool_search_assembly=True``.
    2. tool_search's tier-1 mechanism otherwise keeps _HERMES_CORE_TOOLS
       alive; the observer must see EXACTLY route_to_member and nothing
       else (M4 decompose_and_route is disabled — member coordination is
       handled by the @mention handoff chain in agent_room_router).

    We install a list subclass that rejects re-adds during
    run_conversation's internal tool_search rebuild — same technique as
    gateway/run.py's _toolsets_override.
    """
    # Force the room tool modules to register (idempotent import).
    try:
        import tools.room_router_tool  # noqa: F401
        import tools.room_decompose_tool  # noqa: F401
    except Exception as exc:
        logger.warning("inproc: room tool import failed: %s", exc)

    from model_tools import get_tool_definitions
    from toolsets import resolve_toolset as _resolve_ts

    allowed: set = set()
    for ts in toolsets:
        allowed.update(_resolve_ts(ts))

    raw_defs = get_tool_definitions(
        enabled_toolsets=list(toolsets),
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    locked = [
        t for t in raw_defs
        if t.get("function", {}).get("name") in allowed
    ]
    if not locked:
        logger.warning(
            "inproc: no raw tools resolved for toolsets %s — observer will "
            "have to emit a text-form decision", toolsets,
        )

    class _LockedTools(list):
        def append(self, item):  # noqa: D401 — silently reject
            pass

        def extend(self, items):
            pass

        def __setitem__(self, key, value):
            pass

        # copy.deepcopy repopulates a fresh instance via extend/append, both
        # of which we no-op above — that would silently empty the list. The
        # prompt-cache plan deep-copies agent.tools before every API call
        # (agent/prompt_caching.py strip_anthropic_tool_cache_control), so
        # without these the observer would ship ZERO tools and tool_choice=
        # required becomes a no-op (qwen then emits prose, never a tool call).
        # Return a plain list copy: the lockdown only needs to protect the
        # live agent.tools from mid-run tool_search re-adds, not the transient
        # per-request copy.
        def __deepcopy__(self, memo):
            import copy as _copy
            return [_copy.deepcopy(x, memo) for x in list(self)]

        def __copy__(self):
            return list(self)

    agent.tools = _LockedTools(locked)
    # Keep valid_tool_names in sync so the executor accepts the calls.
    try:
        agent.valid_tool_names = {
            t["function"]["name"] for t in locked
        }
    except Exception:
        pass
    logger.info(
        "inproc: observer tools locked to %d schemas: %s",
        len(locked),
        [t.get("function", {}).get("name") for t in locked],
    )


def _lock_agent_tools_to_none(agent: Any) -> None:
    """Strip EVERY tool from a member agent — a pure-conversation turn.

    Group-chat members are chat roles: they discuss, they don't execute. A
    member that keeps its profile's default file/terminal/execute_code tools
    will actually run commands against the host and leak raw ``<tool_code>``
    blocks into the shared room history (observed in live testing:
    ``uv pip install``, repo reads/writes, WeasyPrint runs). We install the
    same re-add-rejecting list subclass used by ``_lock_agent_tools`` but with
    an EMPTY allow-list, so run_conversation's mid-run tool_search rebuild
    can't repopulate it. tool_choice stays ``auto`` and, with zero tools on
    the wire, the model can only answer in prose."""

    class _NoTools(list):
        def append(self, item):  # noqa: D401 — silently reject re-adds
            pass

        def extend(self, items):
            pass

        def __setitem__(self, key, value):
            pass

        def __deepcopy__(self, memo):
            return []

        def __copy__(self):
            return []

    agent.tools = _NoTools()
    try:
        agent.valid_tool_names = set()
    except Exception:
        pass
    # Belt-and-suspenders: some run loops consult these flags to decide
    # whether to send a tools array / tool_choice at all.
    for attr in ("enabled_toolsets", "toolsets"):
        try:
            setattr(agent, attr, [])
        except Exception:
            pass
    logger.info("inproc: member tools locked to NONE (conversation-only turn)")


def _run_agent_blocking(
    *,
    profile: str,
    message: str,
    system_message: Optional[str],
    context_prompt: str,
    history: list[dict],
    session_id: str,
    toolsets: Optional[list[str]],
) -> dict:
    """Run a single AIAgent turn under *profile*'s home + secret scope.

    Mirrors gateway/run.py's _profile_runtime_scope + hermes_cli.oneshot's
    resolve_runtime_provider so the LLM call resolves the right model,
    credentials, SOUL, config and (for the observer) toolset lockdown.

    The observer turn does NOT force tool_choice=required: qwen3.7-max runs
    in thinking mode and dashscope rejects ``tool_choice=required`` there
    with HTTP 400 ("does not support being set to required … in thinking
    mode"). It isn't needed anyway — once the room tools actually reach the
    wire (see _LockedTools.__deepcopy__), the observer emits a native
    decompose_and_route / route_to_member call on its own under the default
    tool_choice=auto. The text-form fallback parser stays as a safety net.
    """
    home = _profile_home(profile)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import (
        build_profile_secret_scope,
        set_secret_scope,
        reset_secret_scope,
    )

    hydrate_profile_secret_sources(home)
    home_tok = set_hermes_home_override(str(home))
    sec_tok = set_secret_scope(build_profile_secret_scope(home))
    # Scope file/terminal operations to the profile's own workspace so a
    # member turn that writes files can't pollute the repo cwd (file_tools
    # resolves relative paths against TERMINAL_CWD). Restored in finally.
    import os as _os
    workspace = home / "workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _prev_terminal_cwd = _os.environ.get("TERMINAL_CWD")
    _os.environ["TERMINAL_CWD"] = str(workspace)
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.fallback_config import get_fallback_chain
        # AIAgent is constructed through the adapter (agent_room_runner_adapter)
        # so the 13-kwarg coupling to the official constructor is validated in
        # one place and fails loudly if an upstream signature drifts.
        from gateway.agent_room_runner_adapter import build_agent

        cfg = load_config()
        model_cfg = cfg.get("model") or {}
        if isinstance(model_cfg, str):
            effective_model = model_cfg
        else:
            effective_model = (
                model_cfg.get("default") or model_cfg.get("model") or ""
            )
        # Member profiles inherit from the default home — they carry no
        # config.yaml of their own, so load_config() under their home
        # override yields an empty model. Fall back to the default home's
        # config for the model id (credentials still come from the
        # member's secret scope, installed above).
        if not effective_model:
            effective_model = _default_home_model()
        runtime = resolve_runtime_provider(
            requested=None,
            target_model=effective_model or None,
        )
        try:
            fb = get_fallback_chain(cfg)
        except Exception:
            fb = None

        kwargs: dict[str, Any] = {
            "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"),
            "provider": runtime.get("provider"),
            "requested_provider": runtime.get("requested_provider"),
            "api_mode": runtime.get("api_mode"),
            "model": effective_model,
            "max_iterations": 4,
            "quiet_mode": True,
            "session_id": session_id,
            "platform": "cli",
            "credential_pool": runtime.get("credential_pool"),
            "fallback_model": fb or None,
        }
        # ``toolsets`` semantics:
        #   * None            → inherit the profile's default toolset (legacy).
        #   * ["a","b",...]   → lock to exactly those toolsets' raw tools.
        #   * []  (empty list)→ a PURE-CONVERSATION turn: no tools at all.
        #     Group-chat members are chat roles, not workers — without this
        #     they inherit file/terminal/execute_code from the default home
        #     and "really do the work" (run `uv pip install`, read/write the
        #     repo, and leak raw <tool_code> text into the room). An empty
        #     enabled_toolsets + a hard tool lockdown makes the member reply
        #     with prose only.
        if toolsets:
            kwargs["enabled_toolsets"] = list(toolsets)
        elif toolsets is not None:  # explicit [] → conversation-only
            kwargs["enabled_toolsets"] = []
        agent = build_agent(**kwargs)
        if toolsets:
            _lock_agent_tools(agent, list(toolsets))
        elif toolsets is not None:  # [] → strip every tool
            _lock_agent_tools_to_none(agent)

        combined_system = system_message or ""
        if context_prompt:
            combined_system = (
                f"{context_prompt}\n\n{combined_system}" if combined_system
                else context_prompt
            )
        try:
            return agent.run_conversation(
                message,
                system_message=combined_system or None,
                conversation_history=history or None,
            )
        finally:
            try:
                agent.close()
            except Exception:
                pass
    finally:
        reset_secret_scope(sec_tok)
        reset_hermes_home_override(home_tok)
        if _prev_terminal_cwd is None:
            _os.environ.pop("TERMINAL_CWD", None)
        else:
            _os.environ["TERMINAL_CWD"] = _prev_terminal_cwd


class InProcessRoomRunner:
    """Builds an AgentRoomRouter wired to real LLM closures + shared store.

    One instance per dispatch call is fine (cheap); the router's
    last_routed cache is per-instance, which is acceptable for dashboard
    chat (each HTTP call is an independent conversation turn and the N4
    reuse fast-path simply won't trigger — the observer always runs).
    """

    def __init__(self, store: AgentRoomStore, msgs_store: AgentRoomMessagesStore):
        self._store = store
        self._msgs = msgs_store
        self._member_replies: dict[str, str] = {}
        self._synth_reply: Optional[str] = None

    # ── LLM-backed closures ─────────────────────────────────────────

    def _make_router(self, room: AgentRoom) -> AgentRoomRouter:
        store = self._store
        msgs = self._msgs
        loop = asyncio.get_event_loop()

        async def _ack_sender(src, text: str):
            return None  # dashboard: no IM ack

        async def _ack_editor(handle, text: str):
            return None

        async def _classifier(history_tail, last_routed):
            # Dashboard always runs a full observer turn — no N4 reuse.
            return ClassifierResult(is_new_topic=True, confidence=1.0)

        async def _observer_runner(observer_profile, session_id, _src, hist, msg):
            from gateway.agent_room_projection import project_for_observer
            from gateway.agent_room_observer_parse import parse_observer_decision

            room_msgs = msgs.list_messages(room.room_id)
            projected = project_for_observer(room_msgs[:-1] if room_msgs else [])
            projected_hist = [p.to_openai() for p in projected]

            soul_path = _profile_home(observer_profile) / "SOUL.md"
            soul = soul_path.read_text() if soul_path.is_file() else ""

            run_result = await loop.run_in_executor(
                None,
                lambda: _run_agent_blocking(
                    profile=observer_profile,
                    message=msg,
                    system_message=soul,
                    context_prompt="",
                    history=projected_hist,
                    session_id=session_id,
                    toolsets=["room_observer"],
                ),
            )
            decision = parse_observer_decision(run_result)

            # Persist the decision to the shared store (route or decompose).
            try:
                if decision.is_decompose:
                    self._persist_observer_decompose(room.room_id, observer_profile, decision)
                elif decision.target_member:
                    self._persist_observer_route(room.room_id, observer_profile, decision)
            except Exception as exc:
                logger.warning("inproc: observer decision persist failed: %s", exc)
            return decision

        async def _member_dispatcher(member_profile, session_id, _src, hist, msg):
            from gateway.agent_room_projection import project_for_member

            room_msgs = msgs.list_messages(room.room_id)
            projected = project_for_member(
                room_msgs[:-1] if room_msgs else [],
                target_member=member_profile,
            )
            projected_hist = [p.to_openai() for p in projected]

            context_prompt = self._member_room_prompt(room, member_profile)
            run_result = await loop.run_in_executor(
                None,
                lambda: _run_agent_blocking(
                    profile=member_profile,
                    message=msg,
                    system_message=None,
                    context_prompt=context_prompt,
                    history=projected_hist,
                    session_id=session_id,
                    # [] → pure-conversation: a group-chat member discusses,
                    # it does not run terminal/file/execute_code tools (which
                    # would leak <tool_code> and mutate the host). See
                    # _lock_agent_tools_to_none.
                    toolsets=[],
                ),
            )
            reply = run_result.get("final_response", "") if run_result else ""
            # Strip any text-form <tool_code> markup the model typed as prose
            # (belt-and-suspenders with the tool lockdown + prompt rule).
            _raw_len = len(reply or "")
            reply = _strip_toolcode_text(reply or "")
            if _raw_len and len(reply) != _raw_len:
                logger.info(
                    "inproc: stripped tool_code markup from %s reply (%d→%d chars)",
                    member_profile, _raw_len, len(reply),
                )
            if reply:
                try:
                    msgs.append(
                        room.room_id,
                        sender_kind="member",
                        sender_name=member_profile,
                        content=reply,
                    )
                except Exception as exc:
                    logger.warning("inproc: member reply persist failed: %s", exc)
                self._member_replies[member_profile] = reply
            return reply

        async def _synthesis_runner(observer_profile, session_id, _src, rendered):
            synthesis_prompt = (
                "以下是你刚才拆分的子任务由各成员执行后的结果。"
                "请你综合这些结果，为用户写一条最终的、完整连贯的中文回复。"
                "不要再调用任何工具，不要再拆分或转交；直接输出面向用户的最终答复文本。"
                "如果有子任务失败或被跳过，也请在回复中如实、简洁地说明。\n\n"
                f"{rendered}"
            )
            run_result = await loop.run_in_executor(
                None,
                lambda: _run_agent_blocking(
                    profile=observer_profile,
                    message=synthesis_prompt,
                    system_message=None,
                    context_prompt="",
                    history=[],
                    session_id=session_id,
                    toolsets=["room_observer"],
                ),
            )
            final = run_result.get("final_response", "") if run_result else ""
            if final:
                try:
                    msgs.append(
                        room.room_id,
                        sender_kind="observer",
                        sender_name=observer_profile,
                        content=final,
                    )
                except Exception as exc:
                    logger.warning("inproc: synthesis persist failed: %s", exc)
                self._synth_reply = final
            return final

        async def _first_hop_runner(msg, room_arg):
            """Stateless first-hop classifier (dashboard). NO history, NO
            tool_calls — replaces the observer agent turn. Reads only the
            current message + roster descriptions and returns a
            FirstHopResult (roster-validated member list + a ``matched``
            flag distinguishing a real domain match from a forced
            no-match fallback; see agent_room_firsthop.py)."""
            from gateway.agent_room_firsthop import classify_first_hop
            from agent.auxiliary_client import async_call_llm
            from hermes_cli.profiles import _read_config_model

            members_desc = [(m, _read_member_desc(m)) for m in room_arg.members]
            default_member = room_arg.resolve_default_member()
            observer_profile = room_arg.observer_profile
            try:
                _model, _provider = _read_config_model(_profile_home(observer_profile))
            except Exception:
                _model, _provider = None, None

            async def _call_raw(messages):
                resp = await async_call_llm(
                    provider=_provider,
                    model=_model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=256,
                    timeout=30,
                    extra_body={"response_format": {"type": "json_object"}},
                )
                try:
                    return resp.choices[0].message.content or ""
                except Exception:
                    return ""

            hop_result = await classify_first_hop(
                message=msg,
                members=members_desc,
                default_member=default_member,
                call_raw=_call_raw,
            )
            # Persist the routing decision so the room history records WHY
            # this turn was routed the way it was — parity with the observer
            # path's _persist_observer_route. Without this, first-hop-routed
            # turns leave ZERO observer/routing rows in the store, so there's
            # no audit trail of the routing reasoning (observed in live
            # testing: 0 observer rows across a whole conversation). Happens
            # BEFORE member dispatch, so it lands chronologically ahead of the
            # member replies. Defensive: a persistence failure must not break
            # routing.
            try:
                self._persist_firsthop_route(
                    room_arg.room_id, observer_profile, hop_result, default_member,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("inproc: first-hop route persist failed: %s", exc)
            return hop_result

        return AgentRoomRouter(
            store=store,
            ack_sender=_ack_sender,
            ack_editor=_ack_editor,
            classifier=_classifier,
            first_hop_runner=_first_hop_runner,
            member_dispatcher=_member_dispatcher,
            # M4 decompose/synthesis disabled; member coordination via
            # @mention handoff chain. observer_runner/_synthesis_runner
            # left defined above but no longer wired (first_hop replaces
            # the observer agent turn).
            synthesis_runner=None,
        )

    # ── persistence helpers (mirror gateway/run.py) ─────────────────

    def _persist_observer_route(self, room_id, observer_profile, decision):
        import json
        member = decision.target_member
        self._msgs.append(
            room_id,
            sender_kind="observer",
            sender_name=observer_profile,
            content="",
            tool_calls=[{
                "id": "call_obs_inproc",
                "function": {
                    "name": "route_to_member",
                    "arguments": json.dumps({
                        "member": member,
                        "reason": decision.reason,
                        "is_new_topic": decision.is_new_topic,
                    }, ensure_ascii=False),
                },
            }],
        )

    def _persist_firsthop_route(
        self, room_id, observer_profile, hop_result, default_member,
    ):
        """Record a first-hop routing decision as an observer row (parity
        with _persist_observer_route). ``hop_result`` is a FirstHopResult:
        a roster-validated member list + a ``matched`` flag (real domain
        match vs. forced no-match fallback)."""
        import json
        members = [m for m in (getattr(hop_result, "members", None) or [])]
        matched = bool(getattr(hop_result, "matched", True))
        reason = str(getattr(hop_result, "reason", "") or "")
        if not members:
            members = [default_member]
        routed = members if len(members) > 1 else members[0]
        self._msgs.append(
            room_id,
            sender_kind="observer",
            sender_name=observer_profile,
            content="",
            tool_calls=[{
                "id": "call_obs_firsthop_inproc",
                "function": {
                    "name": "route_to_member",
                    "arguments": json.dumps({
                        "member": routed,
                        "reason": (
                            f"first-hop classifier ({'match' if matched else 'no match'})"
                            + (f": {reason}" if reason else "")
                        ),
                        "is_new_topic": True,
                        "matched": matched,
                    }, ensure_ascii=False),
                },
            }],
        )

    def _persist_observer_decompose(self, room_id, observer_profile, decision):
        import json
        self._msgs.append(
            room_id,
            sender_kind="observer",
            sender_name=observer_profile,
            content="",
            tool_calls=[{
                "id": "call_obs_decompose_inproc",
                "function": {
                    "name": "decompose_and_route",
                    "arguments": json.dumps({
                        "tasks": decision.decompose_tasks or [],
                        "reason": decision.reason,
                        "is_new_topic": decision.is_new_topic,
                    }, ensure_ascii=False),
                },
            }],
        )

    @staticmethod
    def _member_room_prompt(room: AgentRoom, member_profile: str) -> str:
        desc_lookup = {m: _read_member_desc(m) for m in room.members}
        other_lines = [
            f"- {n}: {desc_lookup.get(n, '') or '专业助手'}"
            for n in room.members if n != member_profile
        ]
        other_section = "\n".join(other_lines) if other_lines else "- 无"
        own_desc = desc_lookup.get(member_profile, "") or "专业的 AI 助手"
        return (
            f"你是 \"{member_profile}\"，群聊房间 \"{room.room_name}\" 中的 AI 成员之一。\n\n"
            f"你的角色：{own_desc}\n\n"
            f"房间描述：{room.description or '通用协作团队'}\n\n"
            f"当前房间其他 AI 成员（每个成员在自己独立的对话气泡里回复）：\n"
            f"{other_section}\n\n"
            "群聊路由规则（重要）：\n"
            "- 系统已经判断你需要回复本条消息；请直接回应，不要输出空回复。\n"
            "- 你只代表你自己一个人回答；其他成员会分别回复，不要替他们说话。\n"
            "- 回答简洁、对群聊有帮助；使用与用户相同的语言。\n"
            "- 不要假装是人类；需要时明确表明自己是 AI。\n"
            f"- 历史里的\"[发送者]: ...\"是系统归属标记，不要复述或模仿；也不要输出\"[{member_profile}]:\"前缀。\n"
            "- 如果需要另一位成员接手或补充，在回复中 @ 对方名字（如 @finance），系统会自动转交；@ 时说清楚请对方做什么。\n"
            "- 如果你已能完整回答，直接回答，不要在结尾无意义地 @ 其他成员接力。\n"
            "- 你是在群里【讨论】，不是在【执行】：不要调用任何工具，不要读写文件、"
            "不要运行终端/代码，也不要输出 <tool_code>、```tool_code 之类的工具调用文本。"
            "需要写代码或方案时，直接把代码/方案作为普通聊天内容贴出来即可。\n"
        )

    # ── public entry ────────────────────────────────────────────────

    async def dispatch(self, room: AgentRoom, message: str) -> dict:
        """Run the full room pipeline for one user message. Returns a dict
        with the routing outcome + per-member replies + synthesized reply."""
        self._member_replies = {}
        self._synth_reply = None
        router = self._make_router(room)
        result = await router.process_message(
            source=object(),
            message=message,
            history=[],
            room=room,
        )
        return {
            "routing": result,
            "replies": dict(self._member_replies),
            "synthesis": self._synth_reply,
        }
