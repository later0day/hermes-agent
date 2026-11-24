"""Real CLI -> session owner -> gateway watcher -> Telegram SDK handoff coverage."""

from __future__ import annotations

import importlib.metadata
import sqlite3
import sys

import pytest

from tests.e2e.core.platforms._drv_telegram import TelegramDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.e2e.core.terminal._pty import PtyHermes
from tests.fakes.fake_llm_provider import FakeLLMServer, Text

pytestmark = [pytest.mark.spawns_gateway_lookalike, pytest.mark.platforms("posix")]


def test_interactive_handoff_is_delivered_by_the_running_gateway(tmp_path_factory: pytest.TempPathFactory) -> None:
    try:
        importlib.metadata.version("python-telegram-bot")
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(f"python-telegram-bot is not installed for {sys.executable}: the test environment lacks the "
                    "`messaging` extra (`source ./activate --test-extras all,messaging`)", pytrace=False)

    marker = "HANDOFF E2E DELIVERED"
    drv = TelegramDriver()
    drv.start()
    director = Director()
    director.script("none", Text(marker))
    llm = FakeLLMServer(director)
    llm.start()
    platform_env = drv.gateway_env()
    gw = GatewayUnderTest(tmp_path_factory.mktemp("handoff"), llm_base_url=llm.base_url,
                          config=drv.gateway_config(), env=platform_env, ready=drv.connected)
    cli = None
    try:
        gw.start()
        gateway_pid = gw.proc.pid
        cli = PtyHermes(gw.root, [], None, rows=40, cols=120, extra_env=platform_env)
        cli.wait_ready()
        assert cli.exit("/handoff telegram", timeout=120) == 0, cli.dump()
        cli_output = bytes(cli.raw).decode(errors="replace")
        assert "Handoff complete" in cli_output, cli_output[-6000:]
        assert gw.alive() and gw.proc.pid == gateway_pid, gw.tail()

        conn = sqlite3.connect(f"file:{gw.db_path}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute(
                "SELECT id, handoff_state, handoff_platform, handoff_error FROM sessions "
                "WHERE handoff_state IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, rows
        session_id, state, platform, error = rows[0]
        assert (state, platform, error) == ("completed", "telegram", None)
        watcher_log = (gw.hermes_home / "logs" / "gateway.log").read_text(errors="replace")
        dispatch = f"Handoff: dispatching synthetic turn for CLI session {session_id} → telegram"
        assert dispatch in watcher_log, gw.tail()

        requests = director.requests.get("none", [])
        assert len(requests) == 1, f"handoff started {len(requests)} synthetic model turns"
        user_content = [m.get("content") for m in requests[0]["messages"] if m.get("role") == "user"]
        assert any(isinstance(content, str) and "Session was just handed off from CLI" in content
                   for content in user_content), user_content

        visible = drv.visible(drv.home_channel)
        assert [message.text for message in visible] == [marker], drv.describe()
        assert len(drv.sends(drv.home_channel)) == 1, drv.describe()
        assert not drv.format_rejections(drv.home_channel), drv.describe()
    finally:
        if cli is not None:
            cli.close()
        gw.stop()
        llm.stop()
        drv.stop()
