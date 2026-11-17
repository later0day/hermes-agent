"""Dynamic ``/agent use`` source bindings, resolved for an inbound source (fork).

The binding store (``gateway.source_agent_binding``) maps a chat to a profile; this mixin is the
GatewayRunner side that reads it for ``_profile_name_for_source``.
"""
from __future__ import annotations

import functools
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewaySourceBindingMixin:
    @functools.cached_property
    def _profiles_being_deleted(self) -> set[str]:
        """Profiles inside the destructive ``/agent delete`` window: ingress rejects their new turns
        until the tombstone is in place, closing the check-then-delete race with the multiplexer."""
        return set()

    @functools.cached_property
    def _agent_audit_path(self) -> Path:
        """Append-only JSONL audit trail of ``/agent`` actions, under the root Hermes dir."""
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root() / "agent-audit.jsonl"

    def _reject_profile_being_deleted(self, profile: str, route_name: str) -> None:
        if profile in self._profiles_being_deleted:
            from gateway.profile_routing import ProfileRouteRejected
            raise ProfileRouteRejected(route_name)

    def _bound_profile_for_source(self, source: SessionSource, adapter_profile: Optional[str],
                                  config: Any) -> Optional[str]:
        """The profile an ``/agent use`` binding stamps onto *source*, ahead of ``profile_routes``.

        The single stamping path: the result selects BOTH the session-key namespace
        (``agent:<profile>``) and the profile the turn runs under, so the two always agree. A
        binding is made in a chat of the primary bot, so like a route without ``bot_profile`` it
        applies to the primary bot's chats only (a secondary profile's own bot keeps its chats) and,
        like any route, only to a profile this gateway serves; otherwise ``ProfileRouteRejected``.
        """
        primary = getattr(self, "_primary_profile_name", None) or "default"
        if adapter_profile not in (None, primary):
            return None
        bound = self._binding_profile_for_source(source)
        if not bound:
            return None
        from gateway.profile_routing import ProfileRouteRejected
        from gateway.run import _multiplex_profile_homes
        self._reject_profile_being_deleted(bound, bound)
        try:
            served = {name for name, _home in _multiplex_profile_homes(config)}
        except Exception as exc:
            logger.warning("Rejecting binding to %r: the served-profile set could not be resolved", bound,
                           exc_info=True)
            raise ProfileRouteRejected(bound) from exc
        if bound not in served:
            logger.warning("Rejecting binding of %s/%s: profile %r is not served by this gateway",
                           source.platform, source.chat_id, bound)
            raise ProfileRouteRejected(bound)
        return bound

    def _binding_profile_for_source(self, source: SessionSource) -> Optional[str]:
        """Return the profile a source->agent binding maps this source to, or None.

        Single source of truth for binding-store profile resolution, shared by the
        ingress stamping path (``_profile_name_for_source``) and the turn home
        resolver (``_resolve_profile_home_for_source``) so both agree on which
        profile owns a bound conversation. Best-effort: any lookup error returns
        None so routing falls through to profile_routes / the active profile
        rather than dropping the message.

        Lookup order:
          1. Exact key under the configured group/thread isolation settings.
          2. Chat-level key (no participant suffix) — fallback for group members
             who did not run ``/agent use`` themselves.

        Invalid/stale rows (unparseable or nonexistent profile) are ignored and
        removed so a deleted profile cannot stamp one namespace while falling
        back to another home. Legacy mixed-case rows are canonicalized.
        """
        try:
            store = getattr(self, "_source_agent_binding_store", None)
            if store is None:
                from gateway.source_agent_binding import SourceAgentBindingStore

                store = SourceAgentBindingStore()
                self._source_agent_binding_store = store
            from gateway.session import build_source_binding_key
            from hermes_cli.profiles import (
                normalize_profile_name,
                profile_exists,
                validate_profile_name,
            )

            config = getattr(self, "config", None)
            group_per_user = bool(getattr(config, "group_sessions_per_user", True))
            thread_per_user = bool(getattr(config, "thread_sessions_per_user", False))

            def _valid_profile(binding, key: str) -> Optional[str]:
                if not binding or not binding.profile_name:
                    return None
                try:
                    profile = normalize_profile_name(binding.profile_name)
                    validate_profile_name(profile)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid source-agent binding %s -> %r",
                        key, binding.profile_name,
                    )
                    store.delete_binding(key)
                    return None
                if not profile_exists(profile):
                    logger.warning(
                        "Ignoring stale source-agent binding %s -> %s: profile does not exist",
                        key, binding.profile_name,
                    )
                    store.delete_binding(key)
                    return None
                if profile != binding.profile_name:
                    try:
                        store.set_binding(
                            key, profile, agent_id=binding.agent_id,
                            fallback_target=binding.fallback_target,
                            fallback_extra=binding.fallback_extra,
                        )
                    except sqlite3.Error:
                        logger.debug("Could not canonicalize binding %s", key, exc_info=True)
                return profile

            key = build_source_binding_key(
                source,
                group_sessions_per_user=group_per_user,
                thread_sessions_per_user=thread_per_user,
            )
            profile = _valid_profile(store.get_binding(key), key)
            if profile:
                return profile
            chat_type = str(getattr(source, "chat_type", None) or "group")
            if chat_type != "dm":
                chat_key = build_source_binding_key(
                    source,
                    group_sessions_per_user=False,
                    thread_sessions_per_user=False,
                )
                if chat_key != key:
                    profile = _valid_profile(store.get_binding(chat_key), chat_key)
                    if profile:
                        return profile
        except Exception:
            # Ingress boundary: a broken store must not drop the message; it routes to the
            # default profile instead, so log loudly enough to notice.
            logger.warning(
                "binding store lookup failed for source %s", getattr(source, "chat_id", "?"), exc_info=True,
            )
        return None

