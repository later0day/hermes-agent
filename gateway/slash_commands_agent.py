"""/agent — bind a chat to an agent profile, and manage profiles from chat (fork).

``use``/``clear``/``status`` bind the chat (``gateway.source_agent_binding``); ``webhook``,
``list``, ``create`` and ``delete`` manage profiles. Everything but ``status``/``list`` needs an
explicitly configured gateway admin. Mixed into GatewayRunner via GatewaySlashCommandsMixin.
"""
from __future__ import annotations

import dataclasses
import functools
import logging
from typing import Any, Optional

from agent.i18n import t

logger = logging.getLogger("gateway.run")

_USAGE = "Usage: /agent <use|clear|status|webhook|list|create|delete> ..."


@dataclasses.dataclass(frozen=True)
class _AgentCommand:
    """What every /agent subcommand works from, resolved once by _handle_agent_command."""
    event: Any
    source: Any
    config: Any
    store: Any
    args: list
    source_key: str
    chat_level_key: str
    actor_user_id: Optional[str]
    actor_user_name: Optional[str]


class GatewayAgentCommandsMixin:
    def _append_agent_audit(self, action, **kwargs):
        """Best-effort append-only audit of /agent binding actions.

        Writes one JSON line per action to ``self._agent_audit_path`` (set on the
        GatewayRunner). Silently no-ops if the path was never configured, so this
        mixin method is safe on any host object.
        """
        if not getattr(self, "_agent_audit_path", None):
            return
        import json as _json
        import os as _os
        import time as _time

        entry = {"action": action, "timestamp": _time.time(), **kwargs}
        try:
            with open(self._agent_audit_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry) + "\n")
            if _os.name == "posix":
                try:
                    _os.chmod(self._agent_audit_path, 0o600)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    async def _handle_agent_command(self, event) -> str:
        """Handle /agent — bind THIS chat/source to an agent profile.

        Subcommands (phase 1): ``use`` / ``clear`` / ``status``. Binding a source
        routes every subsequent inbound turn from that source to the named profile
        (its own config/skills/memory/.env), while the shared gateway keeps a
        single inbound stream. Resolution flows through
        ``GatewayRunner._binding_profile_for_source`` so the session-key namespace
        and the turn profile home stay in agreement.
        """
        import shlex

        from gateway.session import build_source_binding_key

        source = event.source
        actor_user_id = getattr(source, "user_id", None)
        actor_user_name = getattr(source, "user_name", None)
        config = getattr(self, "config", None)
        group_per_user = bool(getattr(config, "group_sessions_per_user", True))
        thread_per_user = bool(getattr(config, "thread_sessions_per_user", False))
        source_key = build_source_binding_key(
            source,
            group_sessions_per_user=group_per_user,
            thread_sessions_per_user=thread_per_user,
        )
        chat_type = str(getattr(source, "chat_type", None) or "group")
        chat_level_key = source_key
        if chat_type != "dm":
            chat_level_key = build_source_binding_key(
                source,
                group_sessions_per_user=False,
                thread_sessions_per_user=False,
            )

        store = getattr(self, "_source_agent_binding_store", None)
        if store is None:
            from gateway.source_agent_binding import SourceAgentBindingStore

            store = SourceAgentBindingStore()
            self._source_agent_binding_store = store

        text = (event.text or "").strip()
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()

        if len(parts) < 2:
            return _USAGE

        action = parts[1].lower()
        args = parts[2:]

        if not bool(getattr(config, "multiplex_profiles", False)):
            return (
                "Dynamic profile binding is disabled. "
                "Enable `gateway.multiplex_profiles` first."
            )
        # Rebinding a chat hands it another profile's secrets, memory and terminal, and
        # create/delete reshape profiles: same bar as cross-origin /resume — an EXPLICITLY
        # configured admin, never "everyone" under the default ungated slash policy.
        if action not in ("status", "list") and not self._resume_caller_is_admin(source):
            return t("gateway.agent.admin_only", action=action)
        if action in ("use", "clear"):
            owner = self._transport_owner(source) if callable(getattr(source, "_transport_adapter_ref", None)) else None
            bot_profile = owner[1] if isinstance(owner, tuple) else None
            if bot_profile not in (None, getattr(self, "_primary_profile_name", None) or "default"):
                return (f"This chat is served by profile `{bot_profile}`'s own bot; /agent bindings "
                        "apply only to chats of the primary gateway bot.")

        handler = {
            "status": self._agent_status, "clear": self._agent_clear, "use": self._agent_use,
            "webhook": self._agent_webhook, "list": self._agent_list, "create": self._agent_create,
            "delete": self._agent_delete,
        }.get(action)
        if handler is None:
            return _USAGE
        return await handler(_AgentCommand(
            event=event, source=source, config=config, store=store, args=args, source_key=source_key,
            chat_level_key=chat_level_key, actor_user_id=actor_user_id, actor_user_name=actor_user_name,
        ))

    async def _agent_status(self, c: "_AgentCommand") -> str:
        source, store, source_key, chat_level_key = c.source, c.store, c.source_key, c.chat_level_key
        profile = self._binding_profile_for_source(source)
        if profile:
            binding = store.get_binding(source_key) or store.get_binding(chat_level_key)
            fb = (binding.fallback_extra if binding else None) or {}
            wh_status = "present" if fb.get("session_webhook") else "missing"
            return (
                f"Profile: `{profile}`\n"
                f"DingTalk fallback webhook: {wh_status}"
            )
        from hermes_cli.profiles import get_active_profile_name

        effective = self._profile_name_for_source(source)
        effective = effective or get_active_profile_name() or "default"
        return f"No dynamic binding. Effective profile: `{effective}`."

    async def _agent_clear(self, c: "_AgentCommand") -> str:
        source, store, source_key, chat_level_key, actor_user_id, actor_user_name = c.source, c.store, c.source_key, c.chat_level_key, c.actor_user_id, c.actor_user_name
        removed = store.delete_binding(source_key)
        if chat_level_key != source_key:
            removed = store.delete_binding(chat_level_key) or removed
        self._append_agent_audit(
            "agent.clear",
            source_key=source_key,
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
        )
        from hermes_cli.profiles import get_active_profile_name

        effective = self._profile_name_for_source(source)
        effective = effective or get_active_profile_name() or "default"
        prefix = "Cleared dynamic binding." if removed else "No dynamic binding existed."
        return f"{prefix} Effective profile: `{effective}`."

    async def _agent_use(self, c: "_AgentCommand") -> str:
        event, source, store, args, source_key, chat_level_key, actor_user_id, actor_user_name = c.event, c.source, c.store, c.args, c.source_key, c.chat_level_key, c.actor_user_id, c.actor_user_name
        if not args:
            return "Usage: /agent use <profile>"
        from hermes_cli.profiles import (
            normalize_profile_name,
            profile_exists,
            validate_profile_name,
        )

        try:
            target = normalize_profile_name(args[0])
            validate_profile_name(target)
        except (TypeError, ValueError) as exc:
            return f"Invalid profile: {exc}"
        if not profile_exists(target):
            return f"Profile `{target}` does not exist."
        fb_extra = {}
        raw = getattr(event, "raw_message", None)
        if raw is not None:
            if hasattr(raw, "session_webhook"):
                fb_extra["session_webhook"] = raw.session_webhook
            if hasattr(raw, "session_webhook_expired_time"):
                fb_extra["session_webhook_expired_time"] = raw.session_webhook_expired_time
        store.set_binding(
            source_key,
            target,
            agent_id=target,
            fallback_target=source.to_dict(),
            fallback_extra=fb_extra,
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
        )
        # Also create a chat-level binding (without user_id) so ALL users in
        # this group route to the same profile, not just the person who ran
        # /agent use. DMs already omit user_id so this only matters for groups.
        if chat_level_key != source_key:
            store.set_binding(
                chat_level_key,
                target,
                agent_id=target,
                fallback_target=source.to_dict(),
                fallback_extra=fb_extra,
                actor_user_id=actor_user_id,
                actor_user_name=actor_user_name,
            )
        self._append_agent_audit(
            "agent.use",
            source_key=source_key,
            profile=target,
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
        )
        wh_status = "present" if fb_extra.get("session_webhook") else "missing"
        return (
            f"Bound this chat to agent `{target}`.\n"
            f"DingTalk fallback webhook is {wh_status}."
        )

    async def _agent_webhook(self, c: "_AgentCommand") -> str:
        event, store, source_key, chat_level_key, actor_user_id, actor_user_name = c.event, c.store, c.source_key, c.chat_level_key, c.actor_user_id, c.actor_user_name
        binding = store.get_binding(source_key)
        if not binding:
            return "No agent bound. Use `/agent use` first."
        raw = getattr(event, "raw_message", None)
        if raw is not None:
            fb_extra = dict(binding.fallback_extra or {})
            if hasattr(raw, "session_webhook"):
                fb_extra["session_webhook"] = raw.session_webhook
            if hasattr(raw, "session_webhook_expired_time"):
                fb_extra["session_webhook_expired_time"] = raw.session_webhook_expired_time
            store.set_binding(
                source_key,
                binding.profile_name,
                agent_id=binding.agent_id,
                fallback_target=binding.fallback_target,
                fallback_extra=fb_extra,
                actor_user_id=actor_user_id,
                actor_user_name=actor_user_name,
            )
            # Also refresh the chat-level row (groups) so a webhook update
            # from any member benefits every user routed via chat scope.
            if chat_level_key != source_key:
                chat_binding = store.get_binding(chat_level_key)
                if chat_binding is not None:
                    chat_fb = dict(chat_binding.fallback_extra or {})
                    if hasattr(raw, "session_webhook"):
                        chat_fb["session_webhook"] = raw.session_webhook
                    if hasattr(raw, "session_webhook_expired_time"):
                        chat_fb["session_webhook_expired_time"] = raw.session_webhook_expired_time
                    store.set_binding(
                        chat_level_key,
                        chat_binding.profile_name,
                        agent_id=chat_binding.agent_id,
                        fallback_target=chat_binding.fallback_target,
                        fallback_extra=chat_fb,
                    )
            return f"Stored DingTalk fallback webhook for agent `{binding.profile_name}`."
        return "No webhook found in raw message."

    async def _agent_list(self, c: "_AgentCommand") -> str:
        from hermes_cli.profiles import (
            get_profile_dir,
            list_profiles,
            read_profile_meta,
        )

        lines = []
        for p in list_profiles():
            meta = read_profile_meta(get_profile_dir(p.name))
            tag = ", template" if meta.get("template") else ""
            # Real model/skill counts: read the profile's config.yaml
            # (model) and count the skills dir (installed skills). Both
            # are best-effort; profiles without either resolve to "unset"
            # / 0 rather than crashing the list.
            model_name = "unset"
            try:
                cfg_path = get_profile_dir(p.name) / "config.yaml"
                if cfg_path.is_file():
                    from hermes_cli.config import read_user_config_raw

                    cfg = read_user_config_raw(cfg_path)
                    model_name = str(cfg.get("model") or cfg.get("default_model") or "unset")
            except Exception:  # noqa: BLE001
                pass
            skill_count = 0
            try:
                skills_dir = get_profile_dir(p.name) / "skills"
                if skills_dir.is_dir():
                    skill_count = sum(1 for _ in skills_dir.iterdir() if _.is_dir())
            except Exception:  # noqa: BLE001
                pass
            lines.append(f"- `{p.name}` (model {model_name}, skills: {skill_count}{tag})")
        return "\n".join(lines) or "No profiles."

    async def _agent_create(self, c: "_AgentCommand") -> str:
        args, actor_user_id, actor_user_name = c.args, c.actor_user_id, c.actor_user_name
        import argparse

        from hermes_cli.profiles import (
            create_profile,
            get_profile_dir,
            read_profile_meta,
            write_profile_meta,
        )

        parser = argparse.ArgumentParser(prog="/agent create", add_help=False)
        parser.add_argument("name")
        parser.add_argument("--description", nargs="+", default=[])
        parser.add_argument("--orchestrator", action="store_true")
        parser.add_argument("--with-env", action="store_true")
        parser.add_argument("--from-template", type=str)
        parser.add_argument("--template", action="store_true")
        try:
            parsed, _ = parser.parse_known_args(args)
        except SystemExit:
            return (
                "Usage: /agent create <name> [--description ...] "
                "[--orchestrator] [--with-env] [--from-template <profile>] [--template]"
            )
        target_name = parsed.name
        try:
            if parsed.from_template:
                tmpl_meta = read_profile_meta(get_profile_dir(parsed.from_template))
                if not tmpl_meta.get("template"):
                    return f"Profile `{parsed.from_template}` is not marked as a template."
                # create/delete stop processes, copy trees and may spawn skill installs: off the loop.
                await self._run_in_executor_with_context(functools.partial(
                    create_profile, target_name, clone_from=parsed.from_template,
                    clone_env=parsed.with_env, clone_skills=parsed.with_env))
                self._append_agent_audit(
                    "agent.template_clone",
                    profile_name=target_name,
                    source=parsed.from_template,
                    actor_user_id=actor_user_id,
                    actor_user_name=actor_user_name,
                )
            else:
                await self._run_in_executor_with_context(functools.partial(
                    create_profile, target_name, clone_from="default",
                    clone_env=parsed.with_env, clone_skills=parsed.with_env))
                self._append_agent_audit(
                    "agent.create",
                    after={"profile_name": target_name, "orchestrator": parsed.orchestrator},
                    actor_user_id=actor_user_id,
                    actor_user_name=actor_user_name,
                )
            p_dir = get_profile_dir(target_name)
            if parsed.description:
                write_profile_meta(p_dir, description=" ".join(parsed.description))
            if parsed.template:
                write_profile_meta(p_dir, template=True)
            if parsed.orchestrator:
                from hermes_cli.config import read_user_config_raw
                from utils import atomic_yaml_write

                cfg_path = p_dir / "config.yaml"
                if cfg_path.exists():
                    cfg = read_user_config_raw(cfg_path)
                    ts = cfg.get("toolsets", [])
                    if "kanban" not in ts:
                        ts.append("kanban")
                        cfg["toolsets"] = ts
                        atomic_yaml_write(cfg_path, cfg)
            out = f"Created agent profile `{target_name}`."
            out += (
                " .env copied. skills copied."
                if parsed.with_env
                else " .env not copied. no skills copied."
            )
            if parsed.orchestrator:
                out += " kanban orchestrator tools enabled."
            if parsed.template:
                out += " Marked as template."
            return out
        except Exception as exc:  # noqa: BLE001
            return f"Failed: {exc}"

    async def _agent_delete(self, c: "_AgentCommand") -> str:
        source, store, args, source_key, actor_user_id, actor_user_name = c.source, c.store, c.args, c.source_key, c.actor_user_id, c.actor_user_name
        import secrets as _secrets
        import time as _t

        from gateway.session import SessionSource
        from gateway.session_identity import restore_identity
        from hermes_cli.profiles import delete_profile

        if not args:
            return "Usage: /agent delete <profile> [confirm-code]"
        target_name = args[0]
        if target_name == "default":
            return "Refusing to delete `default`."
        if not hasattr(self, "_agent_delete_confirmations"):
            self._agent_delete_confirmations = {}
        code = args[1] if len(args) > 1 else None
        if not code:
            code = getattr(
                self,
                "_agent_delete_code_factory",
                lambda: _secrets.token_hex(3).upper(),
            )()
            self._agent_delete_confirmations[source_key] = {
                "profile": target_name,
                "code": code,
                "expires_at": _t.time() + 300,
            }
            self._append_agent_audit(
                "agent.delete.request",
                profile=target_name,
                actor_user_id=actor_user_id,
                actor_user_name=actor_user_name,
            )
            return f"Confirm within 5m: `/agent delete {target_name} {code}`"
        req = self._agent_delete_confirmations.get(source_key)
        if not req or req.get("profile") != target_name:
            return "No pending deletion request for that profile."
        if _t.time() > req.get("expires_at", 0):
            self._agent_delete_confirmations.pop(source_key, None)
            return "Confirmation expired. Run /agent delete again."
        if code != req.get("code"):
            self._append_agent_audit(
                "agent.delete.failed",
                profile=target_name,
                actor_user_id=actor_user_id,
                actor_user_name=actor_user_name,
            )
            return "Code incorrect."
        self._agent_delete_confirmations.pop(source_key, None)
        # Snapshot bindings BEFORE deleting them so we can notify the other
        # IM sessions bound to the same agent.
        bindings = store.list_bindings(profile_name=target_name)
        removed_count = len(
            {
                (b.fallback_target or {}).get("chat_id")
                for b in bindings
                if b.fallback_target
            }
        )
        # Delete the profile FIRST; only clean bindings if the deletion
        # succeeded — a failed delete must not silently unbind existing
        # chats.
        _profiles_being_deleted = getattr(self, "_profiles_being_deleted", None)
        if _profiles_being_deleted is not None:
            _profiles_being_deleted.add(target_name)
        try:
            await self._run_in_executor_with_context(delete_profile, target_name, True)
        except Exception as exc:  # noqa: BLE001
            return f"Failed: {exc}"
        finally:
            if _profiles_being_deleted is not None:
                _profiles_being_deleted.discard(target_name)
        store.delete_bindings_for_profile(target_name)
        self._append_agent_audit(
            "agent.delete",
            profile=target_name,
            extra={"removed_bindings": removed_count},
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
        )
        # Best-effort notify OTHER bound sessions (never the one that
        # issued the delete, each other chat at most once).
        issuing_chat_id = getattr(source, "chat_id", None)
        notified_chats: set = set()
        for b in bindings:
            if b.source_binding_key == source_key or not b.fallback_target:
                continue
            target_chat_id = b.fallback_target.get("chat_id")
            if target_chat_id == issuing_chat_id or target_chat_id in notified_chats:
                continue
            notified_chats.add(target_chat_id)
            metadata_to_send: dict = {}
            if b.fallback_extra and "session_webhook" in b.fallback_extra:
                metadata_to_send["session_webhook"] = b.fallback_extra["session_webhook"]
            try:
                # Back through the bot that served the bound chat (bindings are primary-bot only).
                target = SessionSource.from_dict(b.fallback_target)
                restore_identity(target, runner=self, transport_profile=None)
                adapter = self._delivery_adapter_for(target)
                if adapter is not None:
                    await adapter.send(
                        target_chat_id,
                        f"Agent profile `{target_name}` was deleted. This chat has been unbound.",
                        metadata=metadata_to_send,
                    )
            except Exception as _notify_exc:  # noqa: BLE001
                logger.debug("/agent delete: failed to notify binding %s: %s", target_chat_id, _notify_exc)
        return f"Deleted agent profile `{target_name}`."
