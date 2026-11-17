"""A cron job created in a DingTalk DM still delivers as a DM after the gateway restarted.

DingTalk's robot API has separate 1:1 and group sends. The adapter decided from the cached inbound
message; after a restart that cache is empty and it guessed "group", sending a DM job's report through
OrgGroupSend. The fork persisted the origin's chat type; the job origin now carries ``chat_type`` and
the staffId (``user_id_alt``), the delivery stamps them, and the adapter maps them to the 1:1 route.
"""

import asyncio
from types import SimpleNamespace

import pytest

import cron.scheduler_delivery as sd
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session_context import clear_session_vars, set_session_vars

pytest.importorskip("alibabacloud_dingtalk")

DM_CHAT = "cidDMxxxxxxxxxxxxxxxxxx=="
STAFF_ID = "manager1234"


class _RobotSdk:
    def __init__(self):
        self.routes: list[tuple[str, object]] = []

    def _record(self, route):
        async def call(request, headers, runtime):
            self.routes.append((route, request))
            return SimpleNamespace(body=SimpleNamespace(process_query_key="q1", invalid_staff_id_list=[]))
        return call

    def __getattr__(self, name):
        routes = {"batch_send_otowith_options_async": "batch_send_oto",
                  "private_chat_send_with_options_async": "private_chat_send",
                  "org_group_send_with_options_async": "org_group_send"}
        if name in routes:
            return self._record(routes[name])
        raise AttributeError(name)


def _origin_for(chat_type: str) -> dict:
    from tools.cronjob_job_args import _origin_from_env

    tokens = set_session_vars(platform="dingtalk", chat_id=DM_CHAT, chat_type=chat_type, user_id="sender-x",
                              user_id_alt=STAFF_ID, session_key="dt", async_delivery=True)
    try:
        return _origin_from_env("every 1h")
    finally:
        clear_session_vars(tokens)


def _cold_adapter():
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "cid", "client_secret": "sec"}))
    adapter._robot_sdk = _RobotSdk()
    adapter._robot_code = "robot-1"
    adapter._card_template_id = None  # robot-native path; no inbound message cached (fresh process)

    async def _token():
        return "tok"
    adapter._get_access_token = _token
    return adapter


@pytest.mark.parametrize("chat_type, expected_route", [("dm", "batch_send_oto"), ("group", "org_group_send")])
def test_cron_delivery_after_restart_keeps_the_origin_conversation_kind(chat_type, expected_route):
    origin = _origin_for(chat_type)
    assert origin["chat_type"] == chat_type and origin["user_id_alt"] == STAFF_ID

    adapter = _cold_adapter()
    t = sd._prepare_target_delivery(
        {"id": "job-1", "origin": origin}, {"platform": "dingtalk", "chat_id": DM_CHAT},
        adapters={Platform.DINGTALK: adapter}, loop=None, config=GatewayConfig(), notify_delivery=True,
        mirror_enabled=False, mirror_text="", delivery_errors=[])
    _thread, route_metadata, _media = sd._live_route_metadata(t)

    result = asyncio.run(adapter.send(DM_CHAT, "daily report", metadata=route_metadata))

    assert result.success, result.error
    route, request = adapter._robot_sdk.routes[-1]
    assert route == expected_route
    if route == "batch_send_oto":
        assert request.user_ids == [STAFF_ID]
