"""Folder/site approval fields and denial reasons across the managed-worker control pipe."""

import queue
import threading
from types import SimpleNamespace

import pytest

from agent import managed_worker
from agent.managed_worker import WorkerControls
from gateway.session_controls import AuthorityConnection
from gateway.session_managed_worker import ManagedWorker, _prompt_frame
from gateway.session_pending_controls import PendingControls
from persistence.core.errors import AuthorityRefusal
from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
from tui_gateway.contracts.canonical_sessions import CanonicalApprovalParams, CanonicalPendingPrompt


def _controls():
    published = []
    events = SimpleNamespace(
        lock=threading.RLock(),
        publish=lambda sid, prompt, event_type: published.append((event_type, prompt)),
    )
    return PendingControls(events), published


@pytest.fixture(autouse=True)
def clean_approval_queue():
    approval._gateway_queues.pop("route", None)
    yield
    approval._gateway_queues.pop("route", None)


@pytest.mark.parametrize("data, target", [
    ({"kind": "access", "path": "/srv/Project  Files", "access": "read", "risk": "Read only risk"},
     {"path": "/srv/Project  Files", "access": "read", "risk": "Read only risk"}),
    ({"kind": "network", "host": "downloads.example", "label": "Download  mirror", "risk": "Network risk"},
     {"host": "downloads.example", "label": "Download  mirror", "risk": "Network risk"}),
])
def test_local_folder_and_site_prompts_retain_their_card_and_trim_denial_reason(data, target):
    controls, published = _controls()
    data = {"request_id": "card-1", "command": "", "description": "E2E approval",
            "allow_permanent": False, **data}
    entry = _ApprovalEntry(data)
    approval._gateway_queues["route"] = [entry]

    controls.register("s", "route", 4, data)
    prompt = published[0][1]
    assert prompt["kind"] == "approval"
    assert prompt["approval_kind"] == data["kind"]
    assert {field: prompt[field] for field in target} == target
    CanonicalPendingPrompt.model_validate(prompt)
    assert controls.snapshot("s", 4) == (prompt,)

    answer = controls.respond("s", 4, "card-1", {"choice": "deny", "reason": "  Not this target  "})
    assert answer == {"status": "resolved", "prompt_id": "card-1"}
    assert entry.event.is_set() and entry.result == "deny"
    assert entry.reason == "Not this target"


@pytest.mark.asyncio
async def test_canonical_rpc_accepts_and_forwards_the_folder_denial_reason():
    answers = []

    async def respond(actor, ref, generation, prompt_id, response):
        answers.append((actor, ref, generation, prompt_id, response))
        return {"status": "resolved", "prompt_id": prompt_id}

    viewer = object.__new__(AuthorityConnection)
    viewer.authority = SimpleNamespace(respond=respond)
    viewer.actor = "actor"
    params = {"session_id": "s", "execution_generation": 4, "prompt_id": "card-1",
              "choice": "deny", "reason": "  Not this target  "}
    CanonicalApprovalParams.model_validate(params)

    assert await viewer.respond("ref", params) == {"status": "resolved", "prompt_id": "card-1"}
    assert answers == [("actor", "ref", 4, "card-1", {"choice": "deny", "reason": "  Not this target  "})]


def test_command_prompt_keeps_choice_only_answers():
    controls, published = _controls()
    data = {"request_id": "command-1", "kind": "command", "command": "rm -rf build",
            "description": "dangerous"}
    entry = _ApprovalEntry(data)
    approval._gateway_queues["route"] = [entry]
    controls.register("s", "route", 2, data)

    assert published[0][1]["approval_kind"] == "command"
    with pytest.raises(AuthorityRefusal, match="invalid_params"):
        controls.respond("s", 2, "command-1", {"choice": "deny", "reason": "not for commands"})
    assert entry.result is None and not entry.event.is_set()

    assert controls.respond("s", 2, "command-1", {"choice": "once"})["status"] == "resolved"
    assert entry.result == "once" and entry.reason is None


@pytest.mark.parametrize("kind, fields", [
    ("access", {"path": "/srv/Shared  Output", "access": "read_write", "risk": "Write risk"}),
    ("network", {"host": "downloads.example", "label": "Downloads", "risk": "Network risk"}),
    ("command", {"command": "rm -rf build", "description": "dangerous"}),
])
def test_worker_notification_forwards_card_fields_without_queue_provenance(monkeypatch, kind, fields):
    sent = []
    channel = SimpleNamespace(send=lambda message, **payload: sent.append((message, payload)))
    controls = object.__new__(WorkerControls)
    controls.channel, controls.route = channel, "route"
    acknowledged = []
    monkeypatch.setattr(approval, "ack_gateway_approval", lambda route, request_id: acknowledged.append((route, request_id)))
    data = {"request_id": "card-worker", "command": "", "description": "approval", "kind": kind,
            "pattern_key": "private-queue-key", "pattern_keys": ["private-queue-key"], **fields}

    controls.approval(data)

    expected = {key: value for key, value in data.items() if key not in {"pattern_key", "pattern_keys"}}
    assert sent == [("approval", {"data": expected})]
    assert acknowledged == [("route", "card-worker")]


def test_owner_accepts_folder_fields_and_returns_the_reason_to_the_worker():
    controls, published = _controls()
    live = SimpleNamespace(controls=controls, route="route")
    authority = SimpleNamespace(
        sessions={"s": live},
        register_approval=lambda sid, generation, route, data: controls.register(sid, route, generation, data),
    )
    answers = []
    worker = SimpleNamespace(respond=lambda *answer: answers.append(answer))
    data = {"request_id": "worker-folder", "kind": "access", "path": "/srv/Project  Files",
            "access": "read_write", "risk": "Write risk", "command": "", "description": "folder"}

    assert _prompt_frame(authority, SimpleNamespace(session_id="s"), {"generation": 7}, worker,
                         {"type": "approval", "data": data}) is True
    assert published[0][1]["approval_kind"] == "access"
    assert published[0][1]["path"] == "/srv/Project  Files"

    result = controls.respond("s", 7, "worker-folder", {"choice": "deny", "reason": "  Private files  "})
    assert result["status"] == "resolved"
    assert answers == [("approval", "worker-folder", "deny", "Private files")]


def test_managed_worker_reason_frame_reaches_the_real_queue_resolver(monkeypatch):
    owner = object.__new__(ManagedWorker)
    owner.closed = threading.Event()
    owner.commands = queue.Queue()
    owner.respond("approval", "worker-folder", "deny", "Private files")
    frame = owner.commands.get_nowait()
    assert frame == {"type": "approval", "prompt_id": "worker-folder", "value": "deny", "reason": "Private files"}

    controls = object.__new__(WorkerControls)
    controls.route = "route"
    controls.agent = None
    controls.stopped = threading.Event()
    controls.finish = threading.Event()
    controls.clarifications = {}
    controls.lock = threading.Lock()
    frames = iter([{**frame, "reason": "  Private files  "}, {"type": "finish"}])
    resolved = []
    monkeypatch.setattr(managed_worker, "read_frame", lambda _stream: next(frames))
    monkeypatch.setattr(managed_worker, "_drain_background_delegations", lambda _budget: 0)
    monkeypatch.setattr(managed_worker, "_die_with_owner", lambda: None)
    monkeypatch.setattr(approval, "resolve_gateway_approval",
                        lambda route, choice, **kwargs: resolved.append((route, choice, kwargs)) or 1)

    controls.read()

    assert resolved == [("route", "deny", {"request_id": "worker-folder", "reason": "Private files"})]
    assert controls.finish.is_set()
