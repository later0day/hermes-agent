"""A transient OpenAPI SDK import failure at module load must not disable cards for the process lifetime.

Production 2026-10-14: a gateway start hit a failed ``alibabacloud_dingtalk`` import while many profiles
initialised at once. ``CARD_SDK_AVAILABLE`` stayed False, ``connect()`` never built the robot client, and
every robot-native send (cron delivery without a live session webhook) failed with "DingTalk robot SDK is
unavailable" until the next restart, while Stream inbound kept working.
"""

import asyncio
import importlib
import sys

import pytest

from gateway.config import PlatformConfig

pytest.importorskip("alibabacloud_dingtalk")

_SDK_GLOBALS = ("dingtalk_card_client", "dingtalk_card_models", "dingtalk_robot_client",
                "dingtalk_robot_models", "open_api_models", "tea_util_models")


def _simulate_failed_import(monkeypatch, module):
    monkeypatch.setattr(module, "CARD_SDK_AVAILABLE", False)
    for name in _SDK_GLOBALS:
        monkeypatch.setattr(module, name, None)


class _FakeStreamClient:
    def __init__(self, *_a, **_kw):
        self.websocket = None

    def register_callback_handler(self, *_a, **_kw):
        pass

    async def start(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_connect_recovers_sdk_after_failed_import(monkeypatch):
    mod = importlib.import_module("plugins.platforms.dingtalk.adapter")
    _simulate_failed_import(monkeypatch, mod)
    monkeypatch.setattr(mod.dingtalk_stream, "DingTalkStreamClient", _FakeStreamClient)

    adapter = mod.DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "cid", "client_secret": "sec"}))
    monkeypatch.setattr(adapter, "_run_stream", lambda: asyncio.sleep(0))
    monkeypatch.setattr(adapter, "_run_watchdog", lambda: asyncio.sleep(0))
    try:
        assert await adapter.connect() is True
        assert adapter._robot_sdk is not None, "robot client must exist so robot-native sends work"
        assert mod.CARD_SDK_AVAILABLE and mod.dingtalk_robot_models is not None
        result = await adapter._send_robot_native_message("cidX", "sampleMarkdown", {"title": "t", "text": "x"})
        assert result.error != "DingTalk robot SDK is unavailable"
    finally:
        adapter._running = False
        if adapter._http_client is not None:
            await adapter._http_client.aclose()


@pytest.mark.asyncio
async def test_connect_rebinds_the_module_copy_siblings_read(monkeypatch):
    """Siblings read SDK models via ``plugins.platforms.dingtalk.adapter`` even when the plugin loader
    imported the adapter under another module name; both copies must recover."""
    canonical = importlib.import_module("plugins.platforms.dingtalk.adapter")
    spec = importlib.util.spec_from_file_location("hermes_plugins_test_copy.adapter", canonical.__file__)
    loaded_copy = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded_copy)
    spec.loader.exec_module(loaded_copy)
    _simulate_failed_import(monkeypatch, canonical)
    _simulate_failed_import(monkeypatch, loaded_copy)
    monkeypatch.setattr(loaded_copy.dingtalk_stream, "DingTalkStreamClient", _FakeStreamClient)

    adapter = loaded_copy.DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "cid", "client_secret": "sec"}))
    monkeypatch.setattr(adapter, "_run_stream", lambda: asyncio.sleep(0))
    monkeypatch.setattr(adapter, "_run_watchdog", lambda: asyncio.sleep(0))
    try:
        assert await adapter.connect() is True
        assert canonical.CARD_SDK_AVAILABLE and canonical.dingtalk_card_models is not None
    finally:
        adapter._running = False
        if adapter._http_client is not None:
            await adapter._http_client.aclose()
