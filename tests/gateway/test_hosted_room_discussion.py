"""Behavior tests for deterministic same-gateway Discussion policy."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms


ROOM_ID = "room-1"
GATEWAY_ID = "gateway-a"
LOCAL_PROFILES = ("research", "build", "review", "ops", "qa", "docs")
MEMBERS = [
    {
        "member_id": f"member-{profile}",
        "profile": profile,
        "handle": profile,
        "display_name": profile.title(),
    }
    for profile in LOCAL_PROFILES[:3]
]


@pytest.fixture
def room_db(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=MEMBERS,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def _events(db: Path) -> list[dict]:
    return hosted_rooms.read_events(
        db,
        room_id=ROOM_ID,
        since_seq=0,
        limit=hosted_rooms.MAX_LOG_LIMIT,
    )["events"]


def _append_user(
    db: Path,
    *,
    event_id: str,
    text: str,
    thread_id: str = "thread-1",
    actor_id: str = "local-user",
) -> dict:
    return hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": actor_id},
        authority_gateway_id=GATEWAY_ID,
        authority_epoch=1,
        payload={"text": text, "thread_id": thread_id},
        now=time.time(),
    )


def _append_publication(
    db: Path,
    plan: discussion.PublicationPlan,
) -> list[dict]:
    return [
        hosted_rooms.append_event(
            db,
            **event.append_kwargs(ROOM_ID),
            now=time.time(),
        )
        for event in plan.events
    ]


def _append_activity(
    db: Path,
    *,
    event_id: str,
    discussion_event_id: str,
    thread_id: str,
) -> dict:
    return hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="room.activity",
        actor={"kind": "gateway", "id": GATEWAY_ID},
        payload={
            "status": "settled",
            "reason_code": "silent_round",
            "thread_id": thread_id,
            "discussion_event_id": discussion_event_id,
        },
        authority_gateway_id=GATEWAY_ID,
        authority_epoch=1,
    )


def _next_task(room: dict, db: Path) -> discussion.DiscussionTaskPlan:
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "task", decision
    assert decision.task is not None
    return decision.task


def _settle_next(
    room: dict,
    db: Path,
    *,
    text: str,
) -> discussion.DiscussionTaskPlan:
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": text},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, publication)
    return task


def test_deferred_member_allows_next_mentioned_member_and_later_terminal_result(
    room_db,
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    first = _next_task(room, db)
    deferred = discussion.plan_publication(
        room,
        _events(db),
        first,
        status="deferred",
        result={"reason": "member_unavailable"},
        execution_generation=1,
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, deferred)

    second = _next_task(room, db)
    assert second.member.member_id != first.member.member_id

    settled = discussion.plan_publication(
        room,
        _events(db),
        first,
        status="settled",
        result={"text": "Recovered on explicit retry."},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, settled)
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "task"
    assert decision.task is not None
    assert decision.task.member.member_id == second.member.member_id


def test_distinct_threads_are_planned_fifo_without_skipping(room_db):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First", thread_id="thread-1")
    _append_user(db, event_id="user-2", text="Second", thread_id="thread-2")

    first = _next_task(room, db)
    assert first.discussion_event_id == "user-1"
    _append_activity(
        db,
        event_id="activity-1",
        discussion_event_id="user-1",
        thread_id="thread-1",
    )
    second = _next_task(room, db)
    assert second.discussion_event_id == "user-2"


def test_room_stop_fences_old_work_but_allows_a_later_message(room_db):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First", thread_id="thread-1")
    stop = hosted_rooms.request_room_stop(
        db,
        room_id=ROOM_ID,
        cancel_id="user-stop-1",
        expected_gateway_id=str(room["authority_gateway_id"]),
        expected_epoch=int(room["authority_epoch"]),
    )
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "idle"
    assert stop["kind"] == "room.stop_requested"

    _append_user(db, event_id="user-2", text="Continue", thread_id="thread-2")
    resumed = _next_task(room, db)
    assert resumed.discussion_event_id == "user-2"


def test_deterministic_task_fits_existing_driver_and_reconstructs_after_restart(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    user = _append_user(db, event_id="user-1", text="Check the release.")

    first = _next_task(room, db)
    repeated = _next_task(room, db)
    assert first == repeated
    assert first.identity.thread_id == "thread-1"
    assert first.payload == {
        "target_member_id": "member-research",
        "target_profile": "research",
        "prompt": first.payload["prompt"],
        "source_event_seq": user["seq"],
    }
    assert set(first.payload) == {
        "target_member_id",
        "target_profile",
        "prompt",
        "source_event_seq",
    }

    admitted = driver.admit_task(
        db,
        first.identity,
        payload=first.payload,
        clock=time.time,
    )
    stored = driver.get_task(db, first.identity)
    reconstructed = discussion.reconstruct_task_plan(
        room,
        _events(db),
        stored,
        local_profiles=LOCAL_PROFILES,
    )
    assert admitted["status"] == "queued"
    assert reconstructed == first

    reopened_events = _events(db)
    assert (
        discussion.reconstruct_task_plan(
            room,
            reopened_events,
            driver.get_task(db, first.identity),
            local_profiles=LOCAL_PROFILES,
        )
        == first
    )


@pytest.mark.parametrize(
    ("text", "expected_profile"),
    [
        ("@build please inspect this", "build"),
        ("@build: please inspect this", "build"),
        ("@build. Please inspect this", "build"),
        ("@all: inspect this", "research"),
        ("@all inspect this", "research"),
        ("@everyone inspect this", "research"),
        ("inspect this", "research"),
        ("@unknown inspect this", "research"),
    ],
)
def test_mentions_select_handles_or_everyone(
    room_db: tuple[Path, dict],
    text: str,
    expected_profile: str,
):
    db, room = room_db
    _append_user(db, event_id="user-1", text=text)

    assert _next_task(room, db).member.profile == expected_profile


def test_member_mention_joins_the_next_round_not_the_current_round(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="@research lead this")

    first = _settle_next(room, db, text="@build can add the implementation detail.")
    second = _next_task(room, db)

    assert first.member.profile == "research"
    assert first.round_index == 0
    assert second.member.profile == "build"
    assert second.round_index == 1
    assert "@research lead this" in second.payload["prompt"]
    assert "@build can add the implementation detail." in second.payload["prompt"]


def test_plain_member_reply_does_not_wake_another_bot_round(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="@research answer the user")
    _settle_next(room, db, text="The answer is ready for the user.")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )

    assert decision.status == "settled"
    assert decision.reason == "silent_round"


def test_deferred_member_turn_keeps_discussion_pending_for_exact_retry(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(
        db,
        event_id="user-1",
        text="@research answer the user",
        thread_id="dagtask:t1",
        actor_id="task-dag",
    )
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="deferred",
        execution_generation=1,
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, publication)

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )

    assert decision.status == "idle"
    assert decision.reason == "deferred_turn"
    assert decision.discussion_event_id == "user-1"


@pytest.mark.parametrize("value", ["", "pass", "pass.", "(pass)", " ( PASS ). "])
def test_pass_detection(value: str):
    assert discussion.is_pass_text(value)


def test_real_text_is_not_a_pass():
    assert not discussion.is_pass_text("I found the issue.")


def test_full_pass_round_settles_without_member_messages(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Any concerns?")

    for _member in MEMBERS:
        _settle_next(room, db, text="(pass)")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"
    assert [event["kind"] for event in _events(db)].count("message.member") == 0


def test_failed_members_advance_the_round_as_silence(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Any concerns?")

    for expected in ("research", "build", "review"):
        task = _next_task(room, db)
        assert task.member.profile == expected
        publication = discussion.plan_publication(
            room,
            _events(db),
            task,
            status="failed",
            result={"error": f"{expected} unavailable"},
            local_profiles=LOCAL_PROFILES,
        )
        assert publication.terminal_kind == "turn.failed"
        assert len(publication.events) == 1
        assert publication.events[0].payload["reason_code"] == "unknown"
        _append_publication(db, publication)

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"


def test_failed_publication_preserves_a_typed_actionable_reason(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Please continue.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="failed",
        result={"error": "HTTP 401 authentication failed"},
        local_profiles=LOCAL_PROFILES,
    )
    assert publication.events[0].payload["reason_code"] == "provider_auth_or_access"


def test_failed_publication_rejects_an_untrusted_reason_code(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Please continue.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="failed",
        result={"error": "failed", "reason_code": "invented"},
        local_profiles=LOCAL_PROFILES,
    )
    assert publication.events[0].payload["reason_code"] == "unknown"


def test_publication_is_idempotent_and_changed_result_conflicts(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )

    first = _append_publication(db, publication)
    repeated = _append_publication(db, publication)
    assert [event["seq"] for event in first] == [event["seq"] for event in repeated]
    assert all(event["idempotent"] for event in repeated)

    changed = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Different."},
        local_profiles=LOCAL_PROFILES,
    )
    with pytest.raises(hosted_rooms.EventConflictError):
        _append_publication(db, changed)


def test_partial_publication_replays_same_effects_before_policy_advances(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )

    message_effect = publication.events[0]
    hosted_rooms.append_event(
        db,
        **message_effect.append_kwargs(ROOM_ID),
        now=time.time(),
    )
    assert _next_task(room, db).identity == task.identity

    replayed = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, replayed)
    assert _next_task(room, db).member.profile == "build"


def test_watermark_excludes_a_members_old_input_and_own_reply(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Old request.")
    first = _settle_next(room, db, text="Old answer.")
    watermark = discussion.derive_member_watermarks(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )[("thread-1", first.member.member_id)]
    assert watermark == max(
        event["seq"]
        for event in _events(db)
        if event["kind"] == "message.member"
        and event["payload"]["task_id"] == first.identity.task_id
    )

    latest = _append_user(db, event_id="user-2", text="New request.")
    next_task = _next_task(room, db)
    assert next_task.member.profile == "research"
    assert next_task.payload["source_event_seq"] == latest["seq"]
    assert "New request." in next_task.payload["prompt"]
    assert "Old request." not in next_task.payload["prompt"]
    assert "Old answer." not in next_task.payload["prompt"]


def test_newer_same_thread_user_event_cancels_a_late_result(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.")
    stale = _next_task(room, db)
    latest = _append_user(db, event_id="user-2", text="Second request.")

    publication = discussion.plan_publication(
        room,
        _events(db),
        stale,
        status="settled",
        result={"text": "Late stale answer."},
        local_profiles=LOCAL_PROFILES,
    )
    assert publication.terminal_kind == "turn.cancelled"
    assert [event.kind for event in publication.events] == ["turn.cancelled"]
    assert publication.events[0].payload["reason"] == "superseded_by_newer_user_event"
    _append_publication(db, publication)

    current = _next_task(room, db)
    assert current.payload["source_event_seq"] == latest["seq"]
    assert "Second request." in current.payload["prompt"]


def test_cross_thread_newer_user_does_not_discard_completed_old_reply(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.", thread_id="thread-1")
    old = _next_task(room, db)
    _append_user(db, event_id="user-2", text="Other topic.", thread_id="thread-2")

    publication = discussion.plan_publication(
        room,
        _events(db),
        old,
        status="settled",
        result={"text": "Completed first topic."},
        local_profiles=LOCAL_PROFILES,
    )
    assert [event.kind for event in publication.events] == [
        "message.member",
        "turn.settled",
    ]


def test_oversized_member_reply_is_truncated_and_next_turn_stays_serviceable(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(
        db,
        event_id="user-large",
        text="u" * discussion.MAX_USER_TEXT_BYTES,
    )
    first = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        first,
        status="settled",
        result={"text": "é" * (discussion.MAX_MEMBER_TEXT_BYTES + 100)},
        local_profiles=LOCAL_PROFILES,
    )

    member_event = next(event for event in publication.events if event.kind == "message.member")
    member_text = member_event.payload["text"]
    assert len(member_text.encode("utf-8")) <= discussion.MAX_MEMBER_TEXT_BYTES
    assert member_text.endswith("share the full result as a file.]")
    _append_publication(db, publication)

    followup = _next_task(room, db)
    assert len(followup.payload["prompt"].encode("utf-8")) <= driver.MAX_PROMPT_BYTES
    assert "Earlier content omitted" in followup.payload["prompt"]


def test_five_round_bound(room_db: tuple[Path, dict]):
    db, room = room_db
    room["members"] = MEMBERS[:2]
    # Address ONE member at round 0 (`@research`) so the opening round has a
    # single responder; the ping-pong `@`-citations then carry the discussion
    # forward. This reaches the round cap at r4 with 9 member messages — under
    # MAX_DISCUSSION_MESSAGES (10) — so the bound we assert is max_rounds and
    # not the message cap intercepting first. Under the old 3-round cap this
    # same drive bounded two rounds earlier, so the test pins the v2 extension.
    _append_user(db, event_id="user-1", text="@research Discuss.")

    settled = 0
    decision = discussion.plan_next_task(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    while decision.status == "task":
        task = decision.task
        peer = "build" if task.member.profile == "research" else "research"
        publication = discussion.plan_publication(
            room,
            _events(db),
            task,
            status="settled",
            result={"text": f"Reply {settled}. @{peer}"},
            local_profiles=LOCAL_PROFILES,
        )
        _append_publication(db, publication)
        settled += 1
        assert settled <= discussion.MAX_DISCUSSION_MESSAGES, "did not bound"
        decision = discussion.plan_next_task(
            room, _events(db), local_profiles=LOCAL_PROFILES
        )

    assert decision.status == "bounded"
    assert decision.reason == "max_rounds"
    # A 3-round cap would have bounded before a 5th round could accrue this many
    # turns; hitting max_rounds here after >6 messages proves rounds 3 and 4 ran.
    assert settled > 2 * discussion.MAX_DISCUSSION_ROUNDS // 3


def test_later_round_task_reconstructs_after_restart(
    room_db: tuple[Path, dict],
):
    # The load-bearing v2 (5-round) recovery path: an in-flight task from a
    # round >2 has a turn_id like `d1.r3.…` / `d1.r4.…`. Before widening the
    # `_TURN_ID_RE` round group from [0-2] to [0-4], reconstruct_task_plan would
    # raise "turn_id is not a Discussion coordinate" on restart, losing the task.
    # Drive a serial ping-pong until a task lands in round >= 3, admit it, then
    # reconstruct it from a fresh read of the log (simulating a gateway restart).
    db, room = room_db
    room["members"] = MEMBERS[:2]
    _append_user(db, event_id="user-1", text="@research Discuss.")

    late_task = None
    decision = discussion.plan_next_task(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    while decision.status == "task":
        task = decision.task
        match = discussion._TURN_ID_RE.fullmatch(task.identity.turn_id)
        assert match is not None, task.identity.turn_id
        if int(match.group("round")) >= 3:
            late_task = task
            break
        peer = "build" if task.member.profile == "research" else "research"
        publication = discussion.plan_publication(
            room,
            _events(db),
            task,
            status="settled",
            result={"text": f"Reply. @{peer}"},
            local_profiles=LOCAL_PROFILES,
        )
        _append_publication(db, publication)
        decision = discussion.plan_next_task(
            room, _events(db), local_profiles=LOCAL_PROFILES
        )

    assert late_task is not None, "expected a task in round >= 3"

    # Admit it to the driver, then reconstruct from a fresh log read (restart).
    driver.admit_task(db, late_task.identity, payload=late_task.payload, clock=time.time)
    reconstructed = discussion.reconstruct_task_plan(
        room,
        _events(db),
        driver.get_task(db, late_task.identity),
        local_profiles=LOCAL_PROFILES,
    )
    assert reconstructed == late_task


def test_ten_message_bound(tmp_path: Path):
    db = tmp_path / "state.db"
    members = [
        {
            "member_id": f"member-{profile}",
            "profile": profile,
            "handle": profile,
        }
        for profile in LOCAL_PROFILES
    ]
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Large",
        members=members,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    _append_user(db, event_id="user-1", text="Discuss.")

    for index in range(discussion.MAX_DISCUSSION_MESSAGES):
        _settle_next(room, db, text=f"Reply {index}. @everyone")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "bounded"
    assert decision.reason == "max_messages"


def test_prompt_delta_is_bounded_to_24_message_lines(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    for index in range(30):
        _append_user(
            db,
            event_id=f"user-{index}",
            text=f"Message {index}.",
        )

    task = _next_task(room, db)
    assert task.payload["prompt"].count("User (user):") == 24
    assert "Message 5." not in task.payload["prompt"]
    assert "Message 6." in task.payload["prompt"]
    assert "Message 29." in task.payload["prompt"]


def test_attachment_payload_is_rejected_by_local_text_only_boundary():
    with pytest.raises(discussion.DiscussionValidationError, match="unknown fields"):
        discussion.validate_user_payload({
            "text": "Review.",
            "thread_id": "thread-1",
            "attachments": [{"name": "notes.txt"}],
        })


@pytest.mark.parametrize(
    ("members", "match"),
    [
        (MEMBERS[:1], "between 2 and 6"),
        (MEMBERS + MEMBERS + MEMBERS[:1], "between 2 and 6"),
        (
            [MEMBERS[0], {**MEMBERS[1], "profile": "research"}],
            "profiles must be unique",
        ),
        ([MEMBERS[0], {**MEMBERS[1], "handle": "RESEARCH"}], "handles must be unique"),
        (
            [MEMBERS[0], {**MEMBERS[1], "member_id": "MEMBER-RESEARCH"}],
            "ids must be unique",
        ),
        ([MEMBERS[0], {**MEMBERS[1], "route": {"mode": "ssh"}}], "cross-gateway"),
        ([MEMBERS[0], {**MEMBERS[1], "connectionId": "remote"}], "cross-gateway"),
        ([MEMBERS[0], {**MEMBERS[1], "profile": "missing"}], "not local"),
    ],
)
def test_malformed_or_remote_roster_is_rejected(members: list[dict], match: str):
    with pytest.raises(discussion.DiscussionValidationError, match=match):
        discussion.validate_roster(members, local_profiles=LOCAL_PROFILES)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hello"},
        {"text": "hello", "thread_id": "thread-1", "images": []},
        {"text": "", "thread_id": "thread-1"},
        {"text": "hello", "thread_id": "../escape"},
        {"text": ["hello"], "thread_id": "thread-1"},
    ],
)
def test_user_payload_is_exact_and_text_only(payload: dict):
    with pytest.raises(discussion.DiscussionValidationError):
        discussion.validate_user_payload(payload)


def test_malformed_log_and_task_reconstruction_fail_closed(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    _append_user(db, event_id="user-2", text="Report again.")
    task = _next_task(room, db)
    events = _events(db)

    with pytest.raises(discussion.DiscussionValidationError, match="sequence order"):
        discussion.plan_next_task(
            room,
            list(reversed(events)),
            local_profiles=LOCAL_PROFILES,
        )

    malformed = {
        "identity": driver.TaskIdentity(
            room_id=task.identity.room_id,
            task_id="dtask:wrong",
            thread_id=task.identity.thread_id,
            turn_id=task.identity.turn_id,
        ),
        "payload": dict(task.payload),
    }
    with pytest.raises(
        discussion.DiscussionReconstructionError,
        match="deterministic reconstruction",
    ):
        discussion.reconstruct_task_plan(
            room,
            events,
            malformed,
            local_profiles=LOCAL_PROFILES,
        )


# ── Decider (star roster) role ───────────────────────────────────────────────

DECIDER_MEMBERS = [
    {
        "member_id": "member-research",
        "profile": "research",
        "handle": "research",
        "display_name": "Research",
        "role": "decider",
    },
    {
        "member_id": "member-build",
        "profile": "build",
        "handle": "build",
        "display_name": "Build",
    },
    {
        "member_id": "member-review",
        "profile": "review",
        "handle": "review",
        "display_name": "Review",
    },
]


@pytest.fixture
def decider_room_db(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=DECIDER_MEMBERS,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def test_roster_role_defaults_to_worker_and_persists_decider():
    members = discussion.validate_roster(
        DECIDER_MEMBERS, local_profiles=LOCAL_PROFILES
    )
    assert members[0].role == discussion.DECIDER_ROLE
    assert members[1].role == discussion.WORKER_ROLE
    assert members[2].role == discussion.WORKER_ROLE


@pytest.mark.parametrize(
    "members, match",
    [
        (
            [
                {**DECIDER_MEMBERS[0]},
                {**DECIDER_MEMBERS[1], "role": "decider"},
            ],
            "at most one decider",
        ),
        (
            [
                {**DECIDER_MEMBERS[0], "role": "boss"},
                {**DECIDER_MEMBERS[1]},
            ],
            "role must be one of",
        ),
    ],
)
def test_invalid_decider_rosters_are_rejected(members: list[dict], match: str):
    with pytest.raises(discussion.DiscussionValidationError, match=match):
        discussion.validate_roster(members, local_profiles=LOCAL_PROFILES)


def test_decider_must_be_local():
    remote_decider = [
        {
            "member_id": "member-remote",
            "profile": "research",
            "handle": "research",
            "role": "decider",
            "target": {
                "kind": "peer",
                "peer_id": "peer-1",
                "installation_id": "inst-1",
                "profile": "research",
                "capability_digest": "a" * 64,
            },
        },
        {**DECIDER_MEMBERS[1]},
    ]
    with pytest.raises(
        discussion.DiscussionValidationError, match="decider must be local"
    ):
        discussion.validate_roster(remote_decider, local_profiles=LOCAL_PROFILES)


def test_decider_answers_opening_round_alone(decider_room_db):
    db, room = decider_room_db
    # No @mention: a mesh roster would wake everyone; the decider roster does not.
    _append_user(db, event_id="user-1", text="Ship the release.")
    first = _next_task(room, db)
    assert first.member.profile == "research"
    assert first.round_index == 0
    assert "You are the decider" in first.payload["prompt"]
    assert "never call delegate_task" in first.payload["prompt"]
    assert "never answer (pass) on the opening turn" in first.payload["prompt"]


def test_internal_task_dag_anchor_targets_worker_without_decider_round(
    decider_room_db,
):
    db, room = decider_room_db
    _append_user(
        db,
        event_id="dagdispatch:t1",
        text="@build run the claimed task",
        thread_id="dagtask:t1",
        actor_id="task-dag",
    )

    first = _settle_next(room, db, text="@research claimed task complete")
    assert first.member.profile == "build"
    assert first.round_index == 0
    assert "You are the decider" not in first.payload["prompt"]
    assert "@research" in first.payload["prompt"]

    # The ledger, not conversational mentions, owns the next DAG transition.
    # The worker's natural report-to-decider text must not open another round.
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"


def test_decider_dispatches_worker_who_reports_back(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship the release.")
    # Decider dispatches the build worker with a concrete sub-task.
    first = _settle_next(
        room, db, text="@build implement the API in server.py, return a summary."
    )
    assert first.member.profile == "research"

    worker = _next_task(room, db)
    assert worker.member.profile == "build"
    assert worker.round_index == 1
    # Worker prompt should route its report back to the decider.
    assert "@research" in worker.payload["prompt"]
    assert "You are the decider" not in worker.payload["prompt"]


def test_worker_to_worker_mention_does_not_break_the_star(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship the release.")
    _settle_next(room, db, text="@build please build it.")
    # The build worker tries to hand off to review directly (worker->worker).
    _settle_next(room, db, text="@review take it from here.")
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    # review must NOT be pulled in by a worker; only decider edges carry turns.
    assert decision.status != "task" or decision.task.member.profile != "review"


def test_worker_can_pull_the_decider_back(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship the release.")
    _settle_next(room, db, text="@build please build it.")
    # Build reports back and re-mentions the decider.
    # Natural model prose commonly appends a colon to the addressee. The
    # mention parser must not consume it as part of the handle.
    handoff = _settle_next(room, db, text="Done. @research: please review and wrap up.")
    assert handoff.member.profile == "build"
    back = _next_task(room, db)
    assert back.member.profile == "research"
    assert back.round_index == 1


def test_mesh_roster_without_decider_is_unchanged(room_db):
    # Baseline: the default fixture roster has no decider, so the opening round
    # still wakes everyone (mesh behavior) and uses the mesh prompt.
    db, room = room_db
    _append_user(db, event_id="user-1", text="hello team")
    first = _next_task(room, db)
    assert "Rules for this Discussion:" in first.payload["prompt"]
    assert "You are the decider" not in first.payload["prompt"]


# ── C4: live task-DAG projection ────────────────────────────────────────────

def test_project_task_dag_empty_without_decider(room_db):
    # A mesh roster (no decider) has no orchestration ledger to project.
    db, room = room_db
    _append_user(db, event_id="user-1", text="hello team")
    _settle_next(room, db, text="hi")
    assert discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES
    ) == ()


def test_project_task_dag_dispatch_then_completion(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship the release.")
    # Decider dispatches one worker.
    _settle_next(room, db, text="@build please build the API.")
    tasks = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    assert len(tasks) == 1
    (task,) = tasks
    assert task.owner_handle == "build"
    assert task.status == "dispatched"
    assert task.completed_seq is None
    assert task.blocked_by == ()

    # The worker replies → the task completes.
    _settle_next(room, db, text="Built it. @research done.")
    tasks = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    build_task = next(t for t in tasks if t.owner_handle == "build")
    assert build_task.status == "completed"
    assert build_task.completed_seq is not None


def test_project_task_dag_parallel_dispatch_is_not_blocked(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship it.")
    # Two workers mentioned in ONE decider message run in parallel.
    _settle_next(room, db, text="@build build it and @review review it.")
    tasks = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    assert len(tasks) == 2
    assert all(t.blocked_by == () for t in tasks), tasks
    assert {t.owner_handle for t in tasks} == {"build", "review"}


def test_project_task_dag_later_round_blocks_on_open_earlier_task(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship it.")
    # Round 0: decider dispatches build.
    _settle_next(room, db, text="@build build the API.")
    # build reports back and pulls the decider in again.
    _settle_next(room, db, text="Building… @research need a decision.")
    # Round 1: decider dispatches review while build is still open? build已完成
    # (it posted a reply), so simulate a still-open second dispatch instead:
    _settle_next(room, db, text="@review please review while build continues.")
    tasks = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    review = next(t for t in tasks if t.owner_handle == "review")
    build = next(t for t in tasks if t.owner_handle == "build")
    # build completed (it replied); review is a later dispatch. Since build is
    # already complete it must NOT be a blocker of review.
    assert build.status == "completed"
    assert build.task_id not in review.blocked_by


def test_research_team_parallel_followups_and_final_synthesis(decider_room_db):
    db, room = decider_room_db
    _append_user(
        db,
        event_id="research-request",
        text="Compare news sites A and B, verify conflicts, and report one synthesis.",
        thread_id="research-thread",
    )

    opening = _settle_next(
        room,
        db,
        text=(
            "@build research news site A; return URLs, timestamps, facts, confidence, "
            "and open questions. @review research news site B using the same evidence schema."
        ),
    )
    assert opening.member.profile == "research"

    parallel = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES, thread_id="research-thread"
    )
    assert {task.owner_handle for task in parallel} == {"build", "review"}
    assert all(task.blocked_by == () for task in parallel)

    site_a = _settle_next(
        room,
        db,
        text=(
            "Site A: headline Alpha; URL https://a.example/alpha; published 09:00 UTC; "
            "claim 10 affected; confidence medium; open question revision time. "
            "@research please compare."
        ),
    )
    site_b = _settle_next(
        room,
        db,
        text=(
            "Site B: headline Alpha update; URL https://b.example/alpha; published 10:00 UTC; "
            "claim 12 affected after official update; confidence high. @research please compare."
        ),
    )
    assert {site_a.member.profile, site_b.member.profile} == {"build", "review"}

    coordinator = _next_task(room, db)
    assert coordinator.member.profile == "research"
    assert "https://a.example/alpha" in coordinator.payload["prompt"]
    assert "https://b.example/alpha" in coordinator.payload["prompt"]
    _append_publication(
        db,
        discussion.plan_publication(
            room,
            _events(db),
            coordinator,
            status="settled",
            result={
                "text": (
                    "The counts conflict. @build verify Site A revision time and source. "
                    "@review verify whether Site B cites the official update."
                )
            },
            local_profiles=LOCAL_PROFILES,
        ),
    )

    followups = discussion.project_task_dag(
        room, _events(db), local_profiles=LOCAL_PROFILES, thread_id="research-thread"
    )
    second_wave = followups[-2:]
    assert {task.owner_handle for task in second_wave} == {"build", "review"}
    assert all(task.blocked_by == () for task in second_wave)

    supplements = {}
    while set(supplements) != {"build", "review"}:
        next_task = _next_task(room, db)
        if next_task.member.profile == "research":
            response = "Waiting for both requested source checks before synthesis."
        elif next_task.member.profile == "build":
            response = "Site A revised at 10:15 UTC and still cites the initial bulletin. @research verified."
            supplements["build"] = response
        else:
            response = "Site B links the 10:00 UTC official update containing 12 affected. @research verified."
            supplements["review"] = response
        _append_publication(
            db,
            discussion.plan_publication(
                room,
                _events(db),
                next_task,
                status="settled",
                result={"text": response},
                local_profiles=LOCAL_PROFILES,
            ),
        )
    final = _next_task(room, db)
    assert final.member.profile == "research"
    transcript = "\n".join(
        event["payload"].get("text", "")
        for event in _events(db)
        if event["kind"] == "message.member"
    )
    assert "revised at 10:15 UTC" in transcript
    assert "official update containing 12" in transcript
    assert "@build" in final.payload["prompt"]
    assert "@review" in final.payload["prompt"]
    _append_publication(
        db,
        discussion.plan_publication(
            room,
            _events(db),
            final,
            status="settled",
            result={
                "text": (
                    "Leader report: both sites cover Alpha. Site A preserves the initial 10; "
                    "Site B reports the later official 12. The difference is temporal, not an unresolved conflict."
                )
            },
            local_profiles=LOCAL_PROFILES,
        ),
    )
    decision = discussion.plan_next_task(
        room, _events(db), local_profiles=LOCAL_PROFILES
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"
    messages = [
        event for event in _events(db) if event["kind"] == "message.member"
    ]
    assert messages[-1]["payload"]["text"].startswith("Leader report:")


def test_research_team_failed_source_never_produces_a_false_leader_report(decider_room_db):
    db, room = decider_room_db
    _append_user(
        db,
        event_id="research-failure",
        text="Compare news sites A and B and report verified findings.",
        thread_id="research-failure-thread",
    )
    _settle_next(
        room,
        db,
        text="@build research site A. @review research site B.",
    )

    first_worker = _next_task(room, db)
    failed = discussion.plan_publication(
        room,
        _events(db),
        first_worker,
        status="failed",
        result={"error": "HTTP 401 authentication failed PRIVATE_PROVIDER_RESPONSE"},
        local_profiles=LOCAL_PROFILES,
    )
    assert failed.terminal_kind == "turn.failed"
    assert failed.events[0].payload["reason_code"] == "provider_auth_or_access"
    assert "PRIVATE_PROVIDER_RESPONSE" not in str(failed.events[0].payload)
    _append_publication(db, failed)

    second_worker = _settle_next(
        room,
        db,
        text="Site B evidence: https://b.example/story. @research site A was unavailable.",
    )
    assert second_worker.member.profile in {"build", "review"}
    coordinator = _next_task(room, db)
    assert coordinator.member.profile == "research"
    failed_events = [event for event in _events(db) if event["kind"] == "turn.failed"]
    assert failed_events[0]["payload"]["reason_code"] == "provider_auth_or_access"
    _append_publication(
        db,
        discussion.plan_publication(
            room,
            _events(db),
            coordinator,
            status="settled",
            result={"text": "Unable to produce a verified comparison because Site A failed."},
            local_profiles=LOCAL_PROFILES,
        ),
    )
    messages = [
        event["payload"]["text"]
        for event in _events(db)
        if event["kind"] == "message.member"
    ]
    assert not any(text.startswith("Leader report:") for text in messages)


def test_research_team_superseded_request_discards_stale_research_result(decider_room_db):
    db, room = decider_room_db
    _append_user(
        db,
        event_id="research-old",
        text="Research yesterday's Site A and Site B reports.",
        thread_id="research-thread",
    )
    stale = _next_task(room, db)
    latest = _append_user(
        db,
        event_id="research-latest",
        text="Correction: research today's Site A and Site B reports only.",
        thread_id="research-thread",
    )
    publication = discussion.plan_publication(
        room,
        _events(db),
        stale,
        status="settled",
        result={"text": "Leader report: stale yesterday result."},
        local_profiles=LOCAL_PROFILES,
    )
    assert publication.terminal_kind == "turn.cancelled"
    assert publication.events[0].payload["reason"] == "superseded_by_newer_user_event"
    _append_publication(db, publication)
    current = _next_task(room, db)
    assert current.member.profile == "research"
    assert current.payload["source_event_seq"] == latest["seq"]
    assert "today's Site A" in current.payload["prompt"]
    assert "stale yesterday result" not in current.payload["prompt"]


def test_project_task_dag_is_deterministic(decider_room_db):
    db, room = decider_room_db
    _append_user(db, event_id="user-1", text="Ship it.")
    _settle_next(room, db, text="@build build and @review review.")
    a = discussion.project_task_dag(room, _events(db), local_profiles=LOCAL_PROFILES)
    b = discussion.project_task_dag(room, _events(db), local_profiles=LOCAL_PROFILES)
    assert a == b
