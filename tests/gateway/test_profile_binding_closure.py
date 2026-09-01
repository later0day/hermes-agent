"""Real-path source binding closure across ingress, session, home, and DB."""

from pathlib import Path

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import (
    SessionSource,
    SessionStore,
    build_session_key,
    build_source_binding_key,
)
from gateway.source_agent_binding import SourceAgentBindingStore
from hermes_constants import get_hermes_home


class _Adapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, *_args, **_kwargs):
        return None

    async def get_chat_info(self, *_args, **_kwargs):
        return {}


def test_binding_closes_ingress_session_home_and_db_loop(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    worker_home = root / "profiles" / "worker"
    (root / "sessions").mkdir(parents=True)
    (worker_home / "sessions").mkdir(parents=True)
    (worker_home / "config.yaml").write_text(
        "model:\n  default: worker-model\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    # The suite-wide hermes_state fixture deliberately pins DEFAULT_DB_PATH.
    # Restore production path resolution so the active profile scope selects
    # its own state.db in this real-path closure test.
    import hermes_state

    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH
    )

    config = GatewayConfig(
        sessions_dir=root / "sessions",
        multiplex_profiles=True,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner._profiles_being_deleted = set()
    runner.session_store = SessionStore(root / "sessions", config)
    runner._source_agent_binding_store = SourceAgentBindingStore(
        tmp_path / "bindings.sqlite"
    )

    probe = SessionSource(
        platform=Platform.DINGTALK,
        chat_id="chat-1",
        chat_type="group",
        user_id="user-1",
    )
    runner._source_agent_binding_store.set_binding(
        build_source_binding_key(probe), "worker"
    )
    adapter = _Adapter(PlatformConfig(), Platform.DINGTALK)
    adapter.gateway_runner = runner

    try:
        source = adapter.build_source(
            chat_id="chat-1",
            chat_type="group",
            user_id="user-1",
        )

        assert source.profile == "worker"
        session_key = build_session_key(
            source,
            group_sessions_per_user=config.group_sessions_per_user,
            thread_sessions_per_user=config.thread_sessions_per_user,
            profile=source.profile,
        )
        assert session_key.startswith("agent:worker:dingtalk:group:chat-1")
        assert runner._resolve_profile_home_for_source(source) == worker_home

        with _profile_runtime_scope(worker_home):
            assert get_hermes_home() == worker_home
            entry = runner.session_store.get_or_create_session(source)
            assert entry.session_key == session_key
            assert runner.session_store._db.db_path == worker_home / "state.db"
    finally:
        runner._source_agent_binding_store.close()
        runner.session_store.close_all_db_handles()
