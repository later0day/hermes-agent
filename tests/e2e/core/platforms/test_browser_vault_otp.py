"""Real-browser one-time-code entry through each platform's distinct interaction contract.

The child is the real gateway and adapter SDK talking only to the loopback platform stand-in.  The
model opens a disposable Chromium tab on a loopback login origin, then ``browser_vault`` fills the
code supplied by a real Discord modal, Slack view submission, or Telegram ForceReply update.  The
login origin records the resulting HTTP form POST; neither browser evaluation nor platform dispatch
is patched.  These stand-ins do not provide coverage of Discord, Slack, or Telegram cloud services.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs

import pytest

from tests.e2e.core.platforms._contract import Rig, answer, wait_reply
from tests.e2e.core.platforms._drv_discord import DiscordDriver
from tests.e2e.core.platforms._drv_slack import SlackDriver
from tests.e2e.core.platforms._drv_telegram import TelegramDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [
    pytest.mark.integration,
    pytest.mark.spawns_gateway_lookalike,
    pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER", "").strip() != "1",
                       reason="real-browser E2E: set HERMES_E2E_BROWSER=1 to opt in"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group gateway harness"),
]

_CODE = "642913"
_SDK = {"discord": "discord.py", "slack": "slack-bolt", "telegram": "python-telegram-bot"}


class _LoginOrigin:
    def __init__(self) -> None:
        self.submissions: list[str] = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/login":
                    self.send_error(404)
                    return
                body = b"""<!doctype html><html><body><form id='login' method='post' action='/login'>
<label for='code'>One-time code</label><input id='code' name='code' type='text'
 inputmode='numeric' autocomplete='one-time-code'><button type='submit'>Verify</button></form>
<script>let submitted=false;document.getElementById('code').addEventListener('input',()=>{
 if(!submitted){submitted=true;document.getElementById('login').requestSubmit();}});</script>
</body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if self.path != "/login":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                origin.submissions.append(str((fields.get("code") or [""])[0]))
                body = b"accepted"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="otp-login-origin", daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/login"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def _chromium(tmp_path: Path) -> Iterator[str]:
    from tools.browser_launcher import launch_chromium, stop_browser

    # The outer PM-activated shell supplies the binary path so the per-test real-home I/O guard
    # remains intact. Chromium itself gets only the fresh temporary profile below.
    executable = os.environ.get("HERMES_TEST_CHROMIUM_EXECUTABLE", "").strip()
    if not executable:
        pytest.fail("set HERMES_TEST_CHROMIUM_EXECUTABLE to chromium_executable(allow_override=False)",
                    pytrace=False)
    key = f"e2e-browser-vault-otp-{os.getpid()}-{threading.get_ident()}"
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "browser-home"), "HERMES_HOME": str(tmp_path / "browser-hermes"),
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    browser, error = launch_chromium(key, executable=executable, user_data_dir=str(tmp_path / "chromium-profile"),
                                     ephemeral=True, headed=False, env=env, timeout=45)
    if browser is None:
        pytest.fail(f"disposable Chromium did not start: {error}", pytrace=False)
    try:
        yield browser.http_url
    finally:
        stop_browser(key)


def _discord_submit(rig: Rig, chat_id: str, code: str) -> None:
    drv = rig.drv
    button = wait_until(lambda: next(iter(drv.buttons(chat_id)), None),
                        "Discord Enter code button", on_timeout=rig.ctx)
    interaction = drv.standin.click(drv.user_id, chat_id, button["message_id"], button["custom_id"])
    modal = wait_until(lambda: drv.standin.modal(interaction), "Discord modal callback", on_timeout=rig.ctx)
    assert modal["custom_id"]
    submitted = drv.standin.submit_modal(drv.user_id, interaction, modal, {"hermes_vault_code_value": code})
    wait_until(lambda: any(str(c.params.get("interaction_id")) == submitted["id"] and not c.faulted
                           for c in drv.standin.calls_of("interaction_callback")),
               "Discord modal submission acknowledgement", on_timeout=rig.ctx)


def _slack_submit(rig: Rig, chat_id: str, code: str) -> None:
    drv = rig.drv
    button = wait_until(
        lambda: next((b for b in drv.buttons(chat_id) if b.get("action_id") == "hermes_vault_code_open"), None),
        "Slack Enter code button", on_timeout=rig.ctx)
    opened = drv.standin.block_action(drv.user_id, chat_id, button["message_id"], button)
    wait_until(lambda: drv.standin.acked(opened), "Slack block-action acknowledgement", on_timeout=rig.ctx)
    call = wait_until(lambda: next((c for c in reversed(drv.standin.calls_of("views.open")) if not c.faulted), None),
                      "Slack views.open", on_timeout=rig.ctx)
    view = call.response["view"]
    assert view["callback_id"] == "hermes_vault_code_submit"
    assert view["private_metadata"]
    values = {"hermes_vault_code_block": {"hermes_vault_code_value": {"type": "plain_text_input", "value": code}}}
    envelope = drv.standin.view_submission(drv.user_id, view, values)
    wait_until(lambda: drv.standin.acked(envelope), "Slack view-submission acknowledgement", on_timeout=rig.ctx)


def _telegram_submit(rig: Rig, chat_id: str, code: str) -> None:
    drv = rig.drv
    call = wait_until(
        lambda: next((c for c in drv.sends(chat_id)
                      if isinstance(c.params.get("reply_markup"), dict)
                      and c.params["reply_markup"].get("force_reply")), None),
        "Telegram ForceReply card", on_timeout=rig.ctx)
    markup = call.params["reply_markup"]
    assert markup.get("selective") is True
    prompt = call.response["result"] if "result" in call.response else call.response
    reply = drv.standin.dm_reply(int(drv.user_id), code, prompt)
    wait_until(lambda: any(str(c.params.get("message_id")) == str(reply["message"]["message_id"])
                           for c in drv.standin.calls_of("deleteMessage") if not c.faulted),
               "Telegram OTP reply deletion", on_timeout=rig.ctx)


_FLOWS: dict[str, tuple[Any, Callable[[Rig, str, str], None]]] = {
    "discord": (DiscordDriver, _discord_submit),
    "slack": (SlackDriver, _slack_submit),
    "telegram": (TelegramDriver, _telegram_submit),
}


def _assert_private(rig: Rig, token: str, code: str) -> None:
    requests = rig.director.requests.get(token) or []
    serialized = json.dumps(requests)
    assert code not in serialized, "OTP reached the model"
    assert code not in rig.gw.tail(20000), "OTP reached gateway logs"
    tool_messages = [m for request in requests for m in request.get("messages", []) if m.get("role") == "tool"]
    assert any(all(fragment in str(m.get("content")) for fragment in
                   ('"success": true', '"filled_fields": 1', '"source": "user"'))
               for m in tool_messages), f"browser_vault result was not returned to the model: {serialized}"


@pytest.mark.parametrize("platform", list(_FLOWS))
def test_browser_vault_otp(platform: str, tmp_path: Path) -> None:
    driver_cls, submit = _FLOWS[platform]
    try:
        importlib.metadata.version(_SDK[platform])
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(f"{_SDK[platform]} is missing: activate with --test-extras all,messaging", pytrace=False)

    origin = _LoginOrigin()
    login_url = origin.start()
    drv = driver_cls()
    drv.start()
    director = Director()
    llm = FakeLLMServer(director)
    llm.start()
    gw = None
    try:
        with _chromium(tmp_path) as cdp_url:
            gw = GatewayUnderTest(tmp_path / "gateway", llm_base_url=llm.base_url, config=drv.gateway_config(),
                                  env={**drv.gateway_env(), "BU_CDP_URL": cdp_url,
                                       "HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1"}, ready=drv.connected)
            rig = Rig(gw=gw, drv=drv, director=director, llm=llm)
            gw.start()
            token, aid = f"otp-{platform}", f"A-otp-{platform}"
            director.script(
                token,
                ToolCall("browser_exec", {"code": f"# Open the test login\nnew_tab({login_url!r})\nwait_for_load()\nprint(page_info())"}),
                ToolCall("browser_vault", {"action": "enter_code"}),
                answer(aid, "code accepted"),
            )
            inbound = drv.dm(f"Log in with a one-time code [in:{token}]")
            submit(rig, inbound.chat_id, _CODE)
            wait_until(lambda: origin.submissions == [_CODE], "actual browser OTP form POST", timeout=30,
                       on_timeout=rig.ctx)
            wait_reply(rig, inbound.chat_id, aid)
            assert director.turns.get(token) == 1
            assert len(gw.user_rows(f"[in:{token}]")) == 1
            assert not gw.user_rows(_CODE), "OTP was persisted as a normal user message"
            assert _CODE not in "\n".join(v.text for v in drv.visible(inbound.chat_id))
            _assert_private(rig, token, _CODE)
    finally:
        if gw is not None:
            gw.stop()
        llm.stop()
        drv.stop()
        origin.stop()
