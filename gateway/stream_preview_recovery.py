"""Durable identity and post-crash retirement for accepted streaming previews."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Callable, Optional

from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")

_RETIRE_TIMEOUT_SECONDS = 10.0
_INTERRUPTION_PLACEHOLDER = "Reply interrupted before delivery completed."


def stream_preview_checkpoint(store, session_key: Optional[str], source: SessionSource) -> Optional[Callable[[set[str]], Any]]:
    if store is None or not session_key:
        return None
    entry = store.lookup_by_session_key(session_key)
    token = entry.active_turn_token if entry is not None else None
    if not token:
        return None
    source_payload = source.to_dict()

    def _checkpoint(message_ids: set[str]) -> None:
        store.checkpoint_stream_preview(session_key, token, source_payload, message_ids)

    return _checkpoint


async def _retire_message(adapter, chat_id: str, message_id: str) -> bool:
    try:
        deleted = await asyncio.wait_for(
            adapter.delete_message(chat_id, message_id), timeout=_RETIRE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Could not delete orphaned stream preview %s", message_id, exc_info=True)
        return False
    if deleted:
        return True
    try:
        result = await asyncio.wait_for(
            adapter.edit_message(
                chat_id, message_id, _INTERRUPTION_PLACEHOLDER, finalize=True,
            ),
            timeout=_RETIRE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Could not replace orphaned stream preview %s", message_id, exc_info=True)
        return False
    return bool(getattr(result, "success", False) or getattr(result, "error_kind", None) == "not_found")


async def cleanup_orphaned_stream_previews(runner, platform=None) -> int:
    pending = await runner.async_session_store.pending_stream_previews()
    retired = 0
    for entry, token, checkpoint in pending:
        source = SessionSource.from_dict(checkpoint["source"])
        if platform is not None and source.platform != platform:
            continue
        restored = runner._restored_source(dataclasses.replace(entry, origin=source))
        adapter = runner._delivery_adapter_for(restored)
        if adapter is None:
            continue
        results = [
            await _retire_message(adapter, source.chat_id, message_id)
            for message_id in checkpoint["message_ids"]
        ]
        if not all(results):
            continue
        if await runner.async_session_store.clear_orphaned_stream_preview(entry.session_key, token):
            retired += 1
    return retired
