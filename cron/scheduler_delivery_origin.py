"""Relay egress discriminators a cron fire carries from its persisted job origin."""

from __future__ import annotations

from typing import Any


def stamp_origin_discriminators(t: Any, route_metadata: dict, media_metadata: dict) -> None:
    """Stamp the origin's ``scope_id`` / ``user_id`` / ``chat_type`` / ``user_id_alt`` onto a live send's metadata.

    Relay egress is fail-closed on a discriminator and the RelayAdapter's caches are cold after every
    boot, so the persisted origin supplies them. Origin targets only (a fan-out target's recipient is not
    the origin's author); ``setdefault`` never overrides router or home stamping; ``user_id`` is read by
    relay transports only.
    """
    origin = t.origin if t.origin_target else {}
    discriminators = (
        ("scope_id", origin.get("scope_id")),
        ("user_id", t.origin_user_id if t.is_relay else None),
        # Conversation kind + alternate sender id: an adapter whose send API differs for DMs (DingTalk
        # 1:1 vs group robot send) otherwise guesses "group" once a restart cleared its message cache.
        ("chat_type", origin.get("chat_type")),
        ("user_id_alt", origin.get("user_id_alt")),
    )
    for key, value in discriminators:
        if value:
            route_metadata.setdefault(key, str(value))
            media_metadata.setdefault(key, str(value))
