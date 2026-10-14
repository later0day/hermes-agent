"""``/kanban swarm`` from a chat subscribes that chat to the swarm's result.

``/kanban create`` auto-subscribes the creating chat; a swarm created from chat did not, so the chat
never heard when it finished (the fork subscribed it). The chat is now subscribed to the swarm's
synthesizer — the card that completes with the swarm's answer. Real handler, real kanban DB.
"""

import asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_swarm_from_chat_subscribes_the_chat_to_its_synthesizer(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc, kanban_db_notify as kbn

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    source = SessionSource(platform=Platform.DINGTALK, chat_id="cidGROUP==", chat_type="group", user_id="u1")
    event = MessageEvent(
        text='/kanban swarm "compare three vendors" --worker coder:research --worker coder:pricing '
             "--verifier reviewer --synthesizer pm",
        source=source)

    reply = asyncio.run(runner._handle_kanban_command(event))

    synthesizer = reply.split("Synthesizer:", 1)[1].split()[0]
    assert synthesizer.startswith("t_")
    with kbc.connect() as conn:
        subs = kbn.list_notify_subs(conn, synthesizer)
    assert [s["chat_id"] for s in subs] == ["cidGROUP=="]
    assert synthesizer in reply.split("Synthesizer:", 1)[1]  # the subscribed-suffix names it
