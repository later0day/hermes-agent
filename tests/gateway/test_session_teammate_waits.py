"""Behaviour contract of the teammate-wait strip's reader (``bot_delivery.list``).

Receipts are the real on-disk shape the canonical owner writes (``gateway/session_bot.py``) under
``<target>/runtime/bot_live_delivery``; the owner join runs against a real ``PendingControls``.
"""
import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

from gateway.session_pending_controls import PendingControls
from gateway.session_teammate_waits import bot_delivery_list
from tools.bot_delivery_status import FAILED_VISIBLE_SECONDS, list_sender_deliveries


def _profile(root: Path, name: str) -> Path:
    home = root if name == "default" else root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    return home


def _receipt(home: Path, key: str, *, sender: str, status: str, age: float = 0.0, **extra) -> Path:
    root = home / "runtime" / "bot_live_delivery"
    root.mkdir(parents=True, exist_ok=True)
    record = {"delivery_id": key, "profile_home": str(home), "session_id": "chat-1", "principal_id": "p",
              "message": f"Message from 🤖 {sender.title()} (@{sender}): please review the draft",
              "status": status, "admission_id": "adm-" + key,
              "author": {"id": f"bot:{sender}", "name": sender, "is_bot": True}, **extra}
    path = root / f"{key}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def test_lists_only_this_senders_unsettled_deliveries_and_never_writes(tmp_path):
    root = tmp_path / "hermes"
    _profile(root, "default")
    scribe = _profile(root, "scribe")
    _receipt(scribe, "a" * 32, sender="alice", status="queued")
    _receipt(scribe, "b" * 32, sender="alice", status="settled", reply="done")
    _receipt(scribe, "c" * 32, sender="bob", status="queued")
    _receipt(scribe, "d" * 32, sender="alice", status="failed", reason="rate_limit", error="429")
    _receipt(scribe, "e" * 32, sender="alice", status="ambiguous")
    _receipt(scribe, "f" * 32, sender="alice", status="failed", reason="rate_limit", age=FAILED_VISIBLE_SECONDS + 60)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (scribe / "runtime/bot_live_delivery").iterdir()}

    rows = list_sender_deliveries(root, "alice", profiles=[("default", root), ("scribe", scribe)],
                                  live_state=lambda *_: (None, None))

    by_id = {row["delivery_id"][0]: row for row in rows}
    assert set(by_id) == {"a", "d", "e"}  # settled, another sender's and stale failures drop off
    assert by_id["a"]["state"] == "queued" and by_id["a"]["target"] == "scribe"
    assert by_id["a"]["preview"] == "please review the draft"
    assert (by_id["d"]["state"], by_id["d"]["reason"], by_id["d"]["error"]) == ("failed", "rate_limit", "429")
    # An unknown outcome is a failure the user must not resend, and says so.
    assert (by_id["e"]["state"], by_id["e"]["reason"]) == ("failed", "unknown_outcome")
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (scribe / "runtime/bot_live_delivery").iterdir()}
    assert after == before  # read-only: no claim, no lock file, no rewrite


def test_a_poll_reparses_only_receipts_written_since_the_last_one(tmp_path, monkeypatch):
    import tools.bot_delivery_status as status

    root = tmp_path / "hermes"
    _profile(root, "default")
    scribe = _profile(root, "scribe")
    for i in range(5):
        _receipt(scribe, f"{i}" * 32, sender="alice", status="settled", reply="done")
    live = _receipt(scribe, "a" * 32, sender="alice", status="queued")
    reads = []
    real_read = status._read_receipt
    monkeypatch.setattr(status, "_read_receipt", lambda path: reads.append(path.name) or real_read(path))

    def poll():
        return list_sender_deliveries(root, "alice", profiles=[("scribe", scribe)], live_state=lambda *_: (None, None))

    assert [row["state"] for row in poll()] == ["queued"]
    reads.clear()
    assert [row["state"] for row in poll()] == ["queued"]
    assert reads == []  # settled history and the unchanged live receipt are not re-read

    record = json.loads(live.read_text(encoding="utf-8-sig"))
    tmp = live.with_suffix(".tmp")
    tmp.write_text(json.dumps({**record, "status": "failed", "reason": "rate_limit"}), encoding="utf-8")
    os.replace(tmp, live)  # the owner's atomic rewrite
    assert [(row["state"], row["reason"]) for row in poll()] == [("failed", "rate_limit")]
    assert reads == [live.name]


class _Events:
    def __init__(self):
        self.lock = threading.RLock()
        self.published = []

    def publish(self, session_id, payload, event_type=None):
        self.published.append(event_type)


def test_owner_join_reports_working_and_waiting_on_you(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    alice, scribe, critic = _profile(root, "default"), _profile(root, "scribe"), _profile(root, "critic")
    _receipt(scribe, "1" * 32, sender="default", status="queued")
    _receipt(critic, "2" * 32, sender="default", status="queued")
    # Queued behind the parked turn in the same chat: it waits on that turn, not on the user.
    _receipt(scribe, "3" * 32, sender="default", status="queued")
    admissions = {"adm-" + "1" * 32: "started", "adm-" + "2" * 32: "started", "adm-" + "3" * 32: "queued"}
    store = SimpleNamespace(admissions=SimpleNamespace(
        get=lambda *, admission_id: SimpleNamespace(status=admissions[admission_id])))

    events = _Events()
    waiting = PendingControls(events)
    clarify = SimpleNamespace(clarify_id="q1", question="Which draft?", choices=["v1", "v2"], multi_select=False,
                              event=threading.Event())
    waiting.register_clarify("chat-1", 3, clarify)
    published_before = list(events.published)

    def authority(home, controls):
        return SimpleNamespace(profile_id=str(home), persistence=store, retired=False,
                               sessions={"chat-1": SimpleNamespace(controls=controls)})

    authorities = {scribe: authority(scribe, waiting), critic: authority(critic, PendingControls(_Events()))}
    registry = SimpleNamespace(for_home=lambda home: authorities.get(Path(home)))
    runner = SimpleNamespace(session_authorities=registry)
    sender = SimpleNamespace(profile_id=str(alice), retired=False, runner=runner)
    connection = SimpleNamespace(authority=sender, actor=SimpleNamespace(capabilities={"session:read"}))

    result = asyncio.run(bot_delivery_list(connection, None, {}))

    states = {row["delivery_id"][0]: (row["target"], row["state"], row.get("waiting_on")) for row in result["deliveries"]}
    assert result["sender"] == "default"
    assert states == {"1": ("scribe", "waiting_on_you", "clarify"), "2": ("critic", "working", None),
                      "3": ("scribe", "queued", None)}
    assert events.published == published_before  # the poll never settles or re-publishes a prompt
    clarify.event.set()  # the user answered: the same delivery is plain work again
    again = asyncio.run(bot_delivery_list(connection, None, {}))
    assert {row["delivery_id"][0]: row["state"] for row in again["deliveries"]}["1"] == "working"


@pytest.mark.platforms("posix")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a 0o000 file")
def test_a_receipt_that_cannot_be_read_fails_the_poll_instead_of_vanishing(tmp_path):
    root = tmp_path / "hermes"
    _profile(root, "default")
    scribe = _profile(root, "scribe")
    live = _receipt(scribe, "a" * 32, sender="alice", status="queued")
    (scribe / "runtime/bot_live_delivery/notes.json").write_text("not json", encoding="utf-8")

    def poll():
        return list_sender_deliveries(root, "alice", profiles=[("scribe", scribe)], live_state=lambda *_: (None, None))

    assert [row["state"] for row in poll()] == ["queued"]  # a foreign file is still skipped
    record = json.loads(live.read_text(encoding="utf-8-sig"))
    tmp = live.with_suffix(".tmp")
    tmp.write_text(json.dumps({**record, "status": "claimed"}), encoding="utf-8")
    os.replace(tmp, live)
    live.chmod(0)
    try:
        with pytest.raises(PermissionError):
            poll()  # an error the client keeps its last rows through, never an empty answer
    finally:
        live.chmod(0o600)
    assert [row["delivery_id"][0] for row in poll()] == ["a"]


async def _authority_connection(home: Path, instance_id: str, *, host: bool = False):
    """A real authority and its contract-checked native RPC connection over ``home/state.db``."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from persistence_assembly import open_persistence, profile_scope

    persistence = open_persistence(profile_scope(home=home))
    store = SessionStore(home / "sessions", GatewayConfig())
    store._db, store._persistence_beside = persistence, lambda pinned: pinned
    runner = SimpleNamespace(session_store=store, _session_db=persistence, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(
        runner, profile_id=str(home.resolve()), instance_id=instance_id, persistence=persistence)
    connection = AuthorityConnection(authority, object(), {
        "user_id": "room-owner", "provider": "local", "profile_id": str(home.resolve()),
        "instance_id": instance_id, **({"profile_scope": "host"} if host else {}),
        "capabilities": ["session:create", "session:read", "session:submit", "session:control"],
    })
    return SimpleNamespace(store=store, runner=runner, authority=authority, connection=connection)


async def _rpc(connection, method: str, params: dict) -> dict:
    response = await connection.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert "result" in response, response
    return response["result"]


@pytest.mark.asyncio
async def test_room_delivery_survives_target_authority_rebuild_and_recovers_terminal_receipt(
        tmp_path, monkeypatch):
    """A hosted-Room owner's delivery remains truthful with no in-process target authority.

    The receipt and admission are produced by the real ``bot_relay.deliver`` RPC. Removing the
    target exercises the SQLite-only join; rebuilding it restores the owner join, including an
    actual pending clarify. Startup recovery then republishes the committed terminal reply to disk.
    """
    from dataclasses import replace

    from gateway import run, session_policy
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority, admission_row
    from gateway.session_bot import recover_bot_deliveries
    from gateway.session_hosted_rpc import HostedRoomAuthorityRPC
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from gateway.session_results import retain_result
    from persistence_assembly import close_stores_under
    from tools.bot_delivery_status import stored_live_state

    root = _profile(tmp_path / "hermes", "default")
    scribe = _profile(root, "scribe")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {
        "model": {"default": "fixture"}, "platform_toolsets": {"cli": []}})
    monkeypatch.setattr(run, "_resolve_gateway_model", lambda config: "fixture")
    # Keep the FIFO durable but drive its claim/settle boundaries explicitly in this test.
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: None)
    original_restore = session_policy.restore_policy

    def restore_room(data):
        if data["source"] == "bot_room":
            return replace(original_restore({**data, "source": "gui", "platform": "desktop"}),
                           source="bot_room", platform="bot_room")
        return original_restore(data)

    monkeypatch.setattr(session_policy, "restore_policy", restore_room)
    sender = await _authority_connection(root, "sender", host=True)
    target = await _authority_connection(scribe, "target-first")
    databases = [sender.authority.persistence, target.authority.persistence]
    try:
        registry = SessionAuthorities(root)
        registry.add(root, sender.authority)
        registry.add(scribe, target.authority, name="scribe")
        sender.runner.session_authorities = registry

        room_rpc = HostedRoomAuthorityRPC(
            sender.authority, asyncio.get_running_loop(), room_id="room-recovery", member_id="default",
            profile="default", principal=sender.connection.actor, authorize=lambda *args: True)
        room = await asyncio.to_thread(
            room_rpc.create, profile="default", source="bot_room", title="Group: room-recovery")
        assert sender.authority.persistence.sessions.get(room["session_id"]).source == "bot_room"

        chat = create_local_session(target.authority, target.connection.actor, {
            "request_id": "bot", "source": "gui", "cwd": str(scribe), "model": "fixture", "toolsets": []})
        title_new_session(target.authority, chat, "Bot Chat")
        key = "9" * 32
        message = "Message from 🤖 Default (@default): review the Room handoff"
        admitted = await _rpc(sender.connection, "bot_relay.deliver", {
            "id": key, "profile": "scribe", "message": message,
            "author": {"id": "bot:default", "name": "default", "is_bot": True},
        })
        assert admitted["status"] == "queued"
        receipt_path = scribe / "runtime" / "bot_live_delivery" / f"{key}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["admission_id"] == admitted["admission_id"]
        assert receipt["principal_id"] == sender.connection.actor.subject

        async def deliveries():
            return (await _rpc(sender.connection, "bot_delivery.list", {}))["deliveries"]

        assert [row["state"] for row in await deliveries()] == ["queued"]
        assert registry.remove(scribe) is target.authority
        for task in list(target.authority._bot_receipt_tasks):
            task.cancel()
        await asyncio.gather(*list(target.authority._bot_receipt_tasks), return_exceptions=True)
        databases.remove(target.authority.persistence)
        target.authority.persistence.close()
        # No target authority: the same RPC now necessarily joins receipt -> real SQLite directly.
        assert stored_live_state("scribe", scribe, receipt) == ("queued", None)
        assert [row["state"] for row in await deliveries()] == ["queued"]

        target = await _authority_connection(scribe, "target-restarted")
        databases.append(target.authority.persistence)
        registry.add(scribe, target.authority, name="scribe")
        row = target.authority.persistence.admissions.claim(
            epoch=target.authority.epoch, session_id=admitted["session_id"])
        assert row is not None and row.status == "started"
        assert [row["state"] for row in await deliveries()] == ["working"]

        registry.remove(scribe)
        assert stored_live_state("scribe", scribe, receipt) == ("started", None)
        assert [row["state"] for row in await deliveries()] == ["working"]
        registry.add(scribe, target.authority, name="scribe")
        live = target.authority.sessions[admitted["session_id"]]
        clarify = SimpleNamespace(clarify_id="room-review", question="Which Room handoff?",
                                  choices=["first", "second"], multi_select=False, event=threading.Event())
        live.controls.register_clarify(admitted["session_id"], row.generation, clarify)
        assert [(row["state"], row.get("waiting_on")) for row in await deliveries()] == [
            ("waiting_on_you", "clarify")]
        registry.remove(scribe)
        assert stored_live_state("scribe", scribe, receipt) == ("started", None)
        assert [(row["state"], row.get("waiting_on")) for row in await deliveries()] == [("working", None)]
        registry.add(scribe, target.authority, name="scribe")

        retain_result(target.authority.persistence, epoch=target.authority.epoch, row=admission_row(row),
                      result={"result": {"final_response": "Room review complete"}, "usage": {}})
        await recover_bot_deliveries(target.authority)
        settled = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert (settled["status"], settled["reply"]) == ("settled", "Room review complete")
        registry.remove(scribe)
        assert await deliveries() == []  # terminal disk state, not a stale working/waiting row
    finally:
        for persistence in databases:
            persistence.close()
        close_stores_under(root)
