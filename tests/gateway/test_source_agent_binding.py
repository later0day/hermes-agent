from concurrent.futures import ThreadPoolExecutor
import os
import stat

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


def _source(
    *,
    platform=Platform.DINGTALK,
    chat_id="chat-1",
    chat_type="group",
    user_id="user-1",
    user_id_alt=None,
    thread_id=None,
):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_id_alt=user_id_alt,
        thread_id=thread_id,
    )


def test_source_binding_key_is_source_only_for_dm():
    source = _source(chat_id="dm-1", chat_type="dm", user_id="alice")

    key = build_source_binding_key(source)

    assert key == "source:dingtalk:dm:dm-1"
    assert "agent:" not in key
    assert "main" not in key
    assert "coder" not in key


def test_source_binding_key_group_isolates_user_by_default():
    alice = _source(chat_id="group-1", user_id="alice")
    bob = _source(chat_id="group-1", user_id="bob")

    assert build_source_binding_key(alice) == "source:dingtalk:group:group-1:alice"
    assert build_source_binding_key(bob) == "source:dingtalk:group:group-1:bob"
    assert build_source_binding_key(alice) != build_source_binding_key(bob)


def test_source_binding_key_group_can_be_shared():
    alice = _source(chat_id="group-1", user_id="alice")
    bob = _source(chat_id="group-1", user_id="bob")

    assert build_source_binding_key(
        alice,
        group_sessions_per_user=False,
    ) == build_source_binding_key(
        bob,
        group_sessions_per_user=False,
    )


def test_source_binding_key_thread_is_shared_by_default():
    alice = _source(chat_id="group-1", user_id="alice", thread_id="topic-1")
    bob = _source(chat_id="group-1", user_id="bob", thread_id="topic-1")

    assert build_source_binding_key(alice) == "source:dingtalk:group:group-1:topic-1"
    assert build_source_binding_key(alice) == build_source_binding_key(bob)


def test_source_binding_key_thread_can_isolate_user():
    alice = _source(chat_id="group-1", user_id="alice", thread_id="topic-1")
    bob = _source(chat_id="group-1", user_id="bob", thread_id="topic-1")

    assert build_source_binding_key(
        alice,
        thread_sessions_per_user=True,
    ) == "source:dingtalk:group:group-1:topic-1:alice"
    assert build_source_binding_key(
        alice,
        thread_sessions_per_user=True,
    ) != build_source_binding_key(
        bob,
        thread_sessions_per_user=True,
    )



def test_source_binding_key_includes_slack_workspace_scope():
    first = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_type="group",
        user_id="U1",
        scope_id="T1",
    )
    second = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_type="group",
        user_id="U1",
        scope_id="T2",
    )

    assert build_source_binding_key(first) == "source:slack:group:T1:C123:U1"
    assert build_source_binding_key(first) != build_source_binding_key(second)


def test_source_binding_key_dm_without_chat_id_falls_back_to_participant():
    alice = _source(chat_id=None, chat_type="dm", user_id="alice")
    bob = _source(chat_id=None, chat_type="dm", user_id="bob")

    assert build_source_binding_key(alice) == "source:dingtalk:dm:alice"
    assert build_source_binding_key(alice) != build_source_binding_key(bob)


def test_source_binding_key_prospective_thread_matches_followup_thread():
    initiating = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="group",
        user_id="alice",
        prospective_thread_id="thread-1",
    )
    followup = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        user_id="bob",
        thread_id="thread-1",
    )

    assert build_source_binding_key(initiating) == build_source_binding_key(followup)

def test_source_agent_binding_store_crud(tmp_path):
    store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    try:
        binding = store.set_binding(
            "source:dingtalk:group:g1:u1",
            "coder",
            fallback_target={"platform": "dingtalk", "chat_id": "g1"},
            fallback_extra={"session_webhook": "https://api.dingtalk.com/webhook"},
            actor_user_id="u1",
            actor_user_name="Alice",
        )

        assert binding.profile_name == "coder"
        assert binding.agent_id == "coder"
        assert binding.created_by == "u1"
        assert binding.updated_by == "u1"
        assert binding.fallback_target == {"platform": "dingtalk", "chat_id": "g1"}
        assert binding.fallback_extra == {
            "session_webhook": "https://api.dingtalk.com/webhook"
        }

        updated = store.set_binding(
            "source:dingtalk:group:g1:u1",
            "reviewer",
            agent_id="reviewer-agent",
            actor_user_id="u2",
            actor_user_name="Bob",
        )

        assert updated.profile_name == "reviewer"
        assert updated.agent_id == "reviewer-agent"
        assert updated.created_by == "u1"
        assert updated.updated_by == "u2"
        assert store.get_binding("source:dingtalk:group:g1:u1") == updated
        assert store.list_bindings(profile_name="reviewer") == [updated]
        assert store.list_bindings(profile_name="coder") == []

        assert store.delete_binding("source:dingtalk:group:g1:u1") is True
        assert store.delete_binding("source:dingtalk:group:g1:u1") is False
        assert store.get_binding("source:dingtalk:group:g1:u1") is None
    finally:
        store.close()


def test_source_agent_binding_store_validates_required_fields(tmp_path):
    store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    try:
        with pytest.raises(ValueError, match="source_binding_key"):
            store.set_binding("", "coder")
        with pytest.raises(ValueError, match="profile_name"):
            store.set_binding("source:dingtalk:dm:u1", "")
    finally:
        store.close()


def test_source_agent_binding_store_handles_basic_concurrent_writes(tmp_path):
    db_path = tmp_path / "bindings.sqlite"

    def write_binding(index: int):
        store = SourceAgentBindingStore(db_path)
        try:
            return store.set_binding(
                f"source:dingtalk:group:g1:user-{index}",
                f"profile-{index % 3}",
                actor_user_id=f"user-{index}",
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write_binding, range(18)))

    store = SourceAgentBindingStore(db_path)
    try:
        bindings = store.list_bindings()
        assert len(bindings) == 18
        assert {binding.source_binding_key for binding in bindings} == {
            result.source_binding_key for result in results
        }
        assert len(store.list_bindings(profile_name="profile-0")) == 6
    finally:
        store.close()


def test_source_agent_binding_store_deletes_bindings_for_profile(tmp_path):
    store = SourceAgentBindingStore(tmp_path / "bindings.sqlite")
    try:
        store.set_binding("source:telegram:dm:1", "worker")
        store.set_binding("source:telegram:dm:2", "worker")
        store.set_binding("source:telegram:dm:3", "other")

        assert store.delete_bindings_for_profile("worker") == 2
        assert store.list_bindings(profile_name="worker") == []
        assert len(store.list_bindings(profile_name="other")) == 1
    finally:
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_source_agent_binding_store_is_private(tmp_path):
    db_path = tmp_path / "bindings.sqlite"
    store = SourceAgentBindingStore(db_path)
    try:
        store.set_binding(
            "source:dingtalk:dm:secret",
            "default",
            fallback_extra={"session_webhook": "https://example.test/secret"},
        )
        state_files = [
            path
            for path in (
                db_path,
                db_path.with_name(db_path.name + "-wal"),
                db_path.with_name(db_path.name + "-shm"),
            )
            if path.exists()
        ]
        assert state_files
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in state_files)
    finally:
        store.close()
