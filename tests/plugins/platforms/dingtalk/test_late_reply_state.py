"""The turn outcome reaches the message it belongs to, never the next one.

A streamed reply's final card edit fires the Done reaction before the runner records the turn's
outcome; the late ``error``/``interrupted`` state used to sit in ``_pending_reply_state`` and was
popped by the NEXT turn's reaction instead.
"""
import asyncio
from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.dingtalk.adapter import DingTalkAdapter


def _adapter():
    a = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
    a.reactions = []
    pending = []

    async def send_emotion(msg_id, conv_id, label, *, recall=False):
        a.reactions.append((msg_id, "-" if recall else "+", label))

    a._send_emotion = send_emotion
    a._spawn_bg = pending.append
    a.run_bg = lambda: [asyncio.run(pending.pop(0)) for _ in list(pending)]
    return a


def _inbound(a, msg_id):
    a._begin_reply_cycle("c", SimpleNamespace(message_id=msg_id, conversation_id="conv"))


def test_a_late_error_outcome_corrects_its_own_message_and_not_the_next():
    a = _adapter()
    _inbound(a, "m1")
    a._fire_done_reaction("c")             # streamed final edit, outcome not known yet
    a.set_pending_reply_state("c", "error")
    a.run_bg()
    assert a.reactions[-2:] == [("m1", "-", a.REACTION_DONE), ("m1", "+", a.REACTION_ERROR)]

    _inbound(a, "m2")
    a._fire_done_reaction("c")
    a.run_bg()
    assert a.reactions[-1] == ("m2", "+", a.REACTION_DONE)
    assert ("m2", "-", a.REACTION_DONE) not in a.reactions
