"""Dynamic ``/agent use`` source bindings, resolved for an inbound source (fork).

The binding store (``gateway.source_agent_binding``) maps a chat to a profile; this mixin is the
GatewayRunner side that reads it for ``_profile_name_for_source``.
"""
from __future__ import annotations

import logging
from typing import Optional

from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewaySourceBindingMixin:
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
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Could not canonicalize binding %s: %s", key, exc)
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
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "binding store lookup failed for source %s: %s",
                getattr(source, "chat_id", "?"), exc,
            )
        return None

