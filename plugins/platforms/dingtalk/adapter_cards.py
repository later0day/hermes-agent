"""DingTalk adapter: AI Card delivery: create/stream/finalize cards, sibling-card cleanup and card parameters.

Mixed into :class:`~plugins.platforms.dingtalk.adapter.DingTalkAdapter`; SDK modules are read through
the adapter module (``_dt()``) so the facade stays the one patch seam."""

import json
import logging
import uuid
from typing import Any, Dict, Optional

from agent.i18n import t
from gateway.platforms.base import SendResult


logger = logging.getLogger(__name__)

# SDK-proven default AI Card template + content key, used when config doesn't specify a custom one.
DEFAULT_AI_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"
DEFAULT_AI_CARD_CONTENT_KEY = "msgContent"


def _dt():
    from plugins.platforms.dingtalk import adapter
    return adapter


class DingTalkCardsMixin:
    async def _close_streaming_siblings(self, chat_id: str) -> None:
        """Finalize open streaming cards for this chat at the start of every ``send()`` — the gateway has no "turn end" signal, so this is what closes lingering tool-progress cards."""
        cards = self._streaming_cards.pop(chat_id, None)
        token = await self._get_access_token() if cards else None
        for out_track_id, last_content in list(cards.items()) if token else ():
            try:
                await self._stream_card_content(out_track_id, token, last_content, finalize=True)
                logger.debug("[%s] AI Card sibling closed: %s", self.name, out_track_id)
            except Exception as e:
                logger.debug("[%s] Sibling close failed for %s: %s", self.name, out_track_id, e)

    async def _send_degraded_progress_notice(
        self,
        chat_id: str,
        session_webhook: Optional[str],
    ) -> None:
        """Send a one-shot text notice when the editable AI Card path fails.

        The notice intentionally does NOT return a ``message_id`` to the caller —
        the outer ``send()`` still returns ``success=False`` so the turn-status
        coordinator disables itself. All errors are best-effort.
        """
        if not session_webhook:
            webhook_info = self._get_valid_webhook(chat_id)
            if webhook_info:
                session_webhook, _ = webhook_info
        if not session_webhook or not self._http_client:
            logger.debug(
                "[%s] Degraded progress notice skipped (no webhook): chat=%s",
                self.name, chat_id,
            )
            return
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "Hermes",
                "text": t("dingtalk.degraded_progress"),
            },
        }
        try:
            resp = await self._http_client.post(
                session_webhook, json=payload, timeout=10.0,
            )
            if resp.status_code >= 300:
                logger.debug(
                    "[%s] Degraded progress notice HTTP %d: %s",
                    self.name, resp.status_code, str(resp.text)[:200],
                )
                return
            logger.info(
                "[%s] Degraded progress notice delivered to chat=%s",
                self.name, chat_id,
            )
        except Exception as exc:
            logger.debug(
                "[%s] Degraded progress notice send failed: %s",
                self.name, exc, exc_info=True,
            )

    async def _create_and_stream_card(
        self,
        chat_id: str,
        message: Any,
        content: str,
        *,
        finalize: bool = True,
        at_users: Optional[Dict[str, str]] = None,
    ) -> Optional[SendResult]:
        """Create, deliver and stream an AI Card; ``finalize=False`` leaves it open for ``edit_message`` by out_track_id.

        When *at_users* is provided, their IDs are passed as ``card_at_user_ids`` on card
        creation (DingTalk supports structured @ within card delivery, not just webhook text).
        """
        try:
            token = await self._get_access_token()
            if not token:
                return None
            out_track_id, models = f"hermes_{uuid.uuid4().hex[:12]}", _dt().dingtalk_card_models
            is_group = str(getattr(message, "conversation_type", "1")) == "2"
            sender_staff_id = getattr(message, "sender_staff_id", "") or ""
            create_kwargs: Dict[str, Any] = {
                "card_template_id": self._card_template_id,
                "out_track_id": out_track_id,
                "card_data": models.CreateCardRequestCardData(card_param_map=self._card_initial_param_map()),
                "callback_type": "STREAM",
                "im_group_open_space_model": models.CreateCardRequestImGroupOpenSpaceModel(support_forward=True),
                "im_robot_open_space_model": models.CreateCardRequestImRobotOpenSpaceModel(support_forward=True),
            }
            if at_users:
                create_kwargs["card_at_user_ids"] = list(at_users.keys())
            create_request = models.CreateCardRequest(**create_kwargs)
            await self._sdk_call(self._card_sdk.create_card_with_options_async, create_request, models.CreateCardHeaders, token)
            if is_group:
                open_space_id = f"dtv1.card//IM_GROUP.{getattr(message, 'conversation_id', '') or ''}"
                deliver_kwargs: Dict[str, Any] = {"robot_code": self._robot_code}
                if at_users:
                    deliver_kwargs["at_user_ids"] = at_users
                deliver_model = {"im_group_open_deliver_model": models.DeliverCardRequestImGroupOpenDeliverModel(**deliver_kwargs)}
            elif sender_staff_id:
                open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
                deliver_model = {"im_robot_open_deliver_model": models.DeliverCardRequestImRobotOpenDeliverModel(space_type="IM_ROBOT")}
            else:
                return logger.warning("[%s] AI Card skipped: missing sender_staff_id for DM", self.name)
            deliver_request = models.DeliverCardRequest(out_track_id=out_track_id, user_id_type=1, open_space_id=open_space_id, **deliver_model)
            await self._sdk_call(self._card_sdk.deliver_card_with_options_async, deliver_request, models.DeliverCardHeaders, token)
            await self._stream_card_content(out_track_id, token, content, finalize=finalize)
            logger.info("[%s] AI Card %s: %s", self.name, "created+finalized" if finalize else "created (streaming)", out_track_id)
            return SendResult(success=True, message_id=out_track_id)
        except Exception as e:
            logger.warning("[%s] AI Card create failed: %s", self.name, e, exc_info=True)
            return None

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        """Stream updated content to an AI Card; ``message_id`` is the creating ``send()``'s out_track_id (callers track their own ids so parallel flows on one chat don't interfere)."""
        if not self.SUPPORTS_MESSAGE_EDITING:
            return SendResult(success=False, error="AI Cards are not configured for message editing")
        token = await self._get_access_token() if message_id else None
        if not token:
            return SendResult(success=False, error="message_id required" if not message_id else "No access token")
        try:
            await self._stream_card_content(message_id, token, content, finalize=finalize)
            if finalize:  # canonical "response ended" signal from the stream consumer's final edit
                self._streaming_cards.get(chat_id, {}).pop(message_id, None)
                if not self._streaming_cards.get(chat_id):
                    self._streaming_cards.pop(chat_id, None)
                logger.debug("[%s] AI Card finalized (edit): %s", self.name, message_id)
                self._fire_done_reaction(chat_id)
            else:  # non-final edit reopens the card into streaming state — track for sibling close
                self._streaming_cards.setdefault(chat_id, {})[message_id] = content
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.warning("[%s] Card edit failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    @staticmethod
    async def _sdk_call(method, request, headers_cls, token: str):
        """``await method(request, headers_cls(token), RuntimeOptions())`` — the alibabacloud SDK call shape."""
        return await method(request, headers_cls(x_acs_dingtalk_access_token=token), _dt().tea_util_models.RuntimeOptions())

    async def _stream_card_content(self, out_track_id: str, token: str, content: str, finalize: bool = False) -> None:
        """Stream content to an existing AI Card."""
        card_content_key = self._current_card_content_key()
        stream_request = _dt().dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id, guid=str(uuid.uuid4()), key=card_content_key, content=content[: self.MAX_MESSAGE_LENGTH],
            is_full=True, is_finalize=finalize, is_error=False,
        )
        await self._sdk_call(self._card_sdk.streaming_update_with_options_async, stream_request, _dt().dingtalk_card_models.StreamingUpdateHeaders, token)

    def _current_card_content_key(self) -> str:
        """The AI Card content variable: ``card_content_key`` from this adapter's own config
        (``extra``, seeded from the profile's ``dingtalk:`` block), else DingTalk's default."""
        return self._card_content_key_override or DEFAULT_AI_CARD_CONTENT_KEY

    def _card_initial_param_map(self) -> Dict[str, str]:
        """Return initial card data for custom templates or the SDK default."""
        if not self._card_uses_default_template:
            return {self._current_card_content_key(): ""}
        order = [
            "msgTitle",
            "msgContent",
            "staticMsgContent",
            "msgTextList",
            "msgImages",
            "msgSlider",
            "msgButtons",
        ]
        return {
            "msgContent": "",
            "staticMsgContent": "",
            "flowStatus": "1",
            "sys_full_json_obj": json.dumps({"order": order}, ensure_ascii=False),
        }

    @staticmethod
    def _image_card_param_map(media_id: str, caption: Optional[str]) -> Dict[str, str]:
        """Card param map for a DM image delivery via card_1_0."""
        content = caption or ""
        order = [
            "msgTitle",
            "msgContent",
            "staticMsgContent",
            "msgImages",
            "msgButtons",
        ]
        return {
            "msgTitle": "Hermes",
            "msgContent": content,
            "staticMsgContent": content,
            "flowStatus": "2",
            "sys_full_json_obj": json.dumps(
                {
                    "order": order,
                    "msgImages": [media_id],
                },
                ensure_ascii=False,
            ),
        }
