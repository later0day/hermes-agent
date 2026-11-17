"""A queued cron delivery that failed before anything was sent is retried; nothing else is.

2026-10-14 14:56-15:00: a gateway whose DingTalk SDK import had failed answered every queued send with
"robot SDK is unavailable" and the at-most-once queue marked six reports failed, lost for good. Such a
refusal provably sent nothing, so a deferred retry cannot duplicate. A multi-target job (one target may
already have received it) and any error from after the transport call keep the at-most-once outcome.
"""

from __future__ import annotations

import time

import pytest

SDK_DOWN = "live adapter send failed: DingTalk robot SDK is unavailable"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    import cron.delivery_queue as q

    monkeypatch.setattr(q, "DELIVERY_DB", tmp_path / "deliveries.db")
    return q


def _advance(monkeypatch, queue, seconds):
    now = time.time() + seconds
    monkeypatch.setattr(queue.time, "time", lambda: now)


def test_pre_send_refusal_is_deferred_then_delivered(queue, monkeypatch):
    queue.enqueue("exec-1", {"id": "job-1", "deliver": "origin"}, "report")
    outcomes = iter([SDK_DOWN, None])
    calls = []

    def send(job, content, for_failure):
        calls.append(content)
        return next(outcomes)

    assert queue.drain(send) == 1
    assert queue.get_status("exec-1")["status"] == "pending"   # deferred, not failed
    assert queue.drain(send) == 0                              # not before its retry time
    _advance(monkeypatch, queue, queue.RETRY_DELAY_SECONDS + 1)
    assert queue.drain(send) == 1
    assert queue.get_status("exec-1")["status"] == "delivered"
    assert calls == ["report", "report"]


def test_retries_are_bounded(queue, monkeypatch):
    queue.enqueue("exec-1", {"id": "job-1", "deliver": "origin"}, "report")
    for attempt in range(queue.MAX_DELIVERY_RETRIES + 1):
        _advance(monkeypatch, queue, queue.RETRY_DELAY_SECONDS * (attempt + 1) * 10)
        queue.drain(lambda *_: SDK_DOWN)
    assert queue.get_status("exec-1")["status"] == "failed"


@pytest.mark.parametrize("deliver, error", [
    ("dingtalk:cidA,dingtalk:cidB", SDK_DOWN),       # a partial multi-target success would be re-sent
    ("origin", "DingTalk group send failed: 500"),    # the transport was called; outcome unknown
])
def test_anything_that_may_have_sent_stays_at_most_once(queue, deliver, error):
    queue.enqueue("exec-1", {"id": "job-1", "deliver": deliver}, "report")
    assert queue.drain(lambda *_: error) == 1
    assert queue.get_status("exec-1")["status"] == "failed"
