"""A worker-queued cron result of an ``/agent use``-bound satellite is drained through the primary bot.

Under a managed (systemd) gateway every cron job runs in an external worker that queues its send; the
gateway drain must lend the primary adapter to a credentialless satellite for the chats routed to it.
The in-process ticker counted ``/agent use`` bindings as routes, the drain only static
``profile_routes`` — so a satellite reached purely by bindings (the common case) failed every delivery
with "platform 'dingtalk' not configured/enabled" (seen live for every xcx report).
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import gateway.run as gateway_run


def test_binding_routed_satellite_queue_row_is_delivered_by_the_primary_adapter(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    from cron import delivery_queue
    from gateway.config import Platform
    from gateway.source_agent_binding import SourceAgentBindingStore
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = tmp_path / "root"
    satellite_home = root / "profiles" / "satellite"
    (satellite_home / "cron").mkdir(parents=True)
    # No static profile_routes at all: the chat reaches the satellite only through `/agent use`.
    (root / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": True}}), encoding="utf-8")
    (satellite_home / "config.yaml").write_text(yaml.safe_dump({"model": "x"}), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    bindings_db = root / "gateway_source_agent_bindings.sqlite"
    monkeypatch.setattr("gateway.source_agent_binding.source_agent_bindings_db_path", lambda: bindings_db)
    store = SourceAgentBindingStore(db_path=bindings_db)
    store.set_binding("source:dingtalk:group:cidGROUP==", "satellite")
    store.close()

    sent, standalone = [], []

    class PrimaryDingTalk:
        async def send(self, chat_id, content, metadata=None):
            sent.append(chat_id)
            return {"success": True, "message_id": "m1"}

    async def _standalone(platform, pconfig, chat_id, text, **kwargs):
        standalone.append(chat_id)
        return {"success": False, "error": "no DingTalk credential"}

    token = set_hermes_home_override(str(satellite_home))  # the external worker queues under its home
    try:
        delivery_queue.enqueue("exec-1", {"id": "job1", "name": "probe", "deliver": "dingtalk:cidGROUP=="}, "hello")
    finally:
        reset_hermes_home_override(token)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True), _profile_adapters={"satellite": {}})
    monkeypatch.setattr(gateway_run, "_handoff_watch_scopes", lambda _r: [("satellite", satellite_home)])
    try:
        with patch("tools.send_message_tool._send_to_platform", _standalone), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            gateway_run._drain_restart_safe_cron_deliveries({Platform.DINGTALK: PrimaryDingTalk()}, loop, runner)
    finally:
        loop.call_soon_threadsafe(loop.stop)

    token = set_hermes_home_override(str(satellite_home))
    try:
        row = delivery_queue.get_status("exec-1")
    finally:
        reset_hermes_home_override(token)
    assert row["status"] == "delivered", row
    assert sent == ["cidGROUP=="] and standalone == []
