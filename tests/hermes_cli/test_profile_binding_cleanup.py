"""Profile deletion integration with dynamic source bindings."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.source_agent_binding import SourceAgentBindingStore
from hermes_cli.profiles import delete_profile


@pytest.fixture()
def deletion_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    profile_dir = root / "profiles" / "worker"
    (profile_dir / "skills").mkdir(parents=True)
    (profile_dir / "workspace").mkdir()
    return root, profile_dir


def _seed_bindings(root: Path) -> Path:
    db_path = root / "gateway_source_agent_bindings.sqlite"
    store = SourceAgentBindingStore(db_path)
    try:
        store.set_binding("source:dingtalk:group:one", "worker")
        # Legacy mixed-case rows must be removed with the canonical profile.
        store.set_binding("source:dingtalk:group:two", "Worker")
    finally:
        store.close()
    return db_path


def _delete_without_services(*, cleanup_bindings=True):
    with (
        patch("hermes_cli.profiles._check_gateway_running", return_value=False),
        patch("hermes_cli.profiles._cleanup_gateway_service"),
        patch("hermes_cli.profiles._maybe_unregister_gateway_service"),
        patch("hermes_cli.profiles._stop_profile_backends"),
    ):
        return delete_profile(
            "worker", yes=True, cleanup_bindings=cleanup_bindings
        )


def test_delete_profile_cleans_central_source_bindings(deletion_env):
    root, profile_dir = deletion_env
    db_path = _seed_bindings(root)

    removed = _delete_without_services()

    assert removed == profile_dir
    assert not profile_dir.exists()
    store = SourceAgentBindingStore(db_path)
    try:
        assert store.list_bindings() == []
    finally:
        store.close()


def test_delete_without_dynamic_bindings_does_not_create_store(deletion_env):
    root, _profile_dir = deletion_env
    db_path = root / "gateway_source_agent_bindings.sqlite"

    _delete_without_services()

    assert not db_path.exists()


@pytest.mark.asyncio
async def test_dashboard_delete_uses_shared_binding_cleanup(deletion_env):
    from hermes_cli.web_routers.profiles import delete_profile_endpoint

    root, profile_dir = deletion_env
    db_path = _seed_bindings(root)

    with (
        patch("hermes_cli.profiles._check_gateway_running", return_value=False),
        patch("hermes_cli.profiles._cleanup_gateway_service"),
        patch("hermes_cli.profiles._maybe_unregister_gateway_service"),
        patch("hermes_cli.profiles._stop_profile_backends"),
    ):
        result = await delete_profile_endpoint("worker")

    assert result["ok"] is True
    assert result["path"] == str(profile_dir)
    store = SourceAgentBindingStore(db_path)
    try:
        assert store.list_bindings() == []
    finally:
        store.close()


def test_delete_profile_failure_preserves_source_bindings(
    deletion_env, monkeypatch
):
    root, profile_dir = deletion_env
    db_path = _seed_bindings(root)
    monkeypatch.setattr("hermes_cli.profiles.time.sleep", lambda _seconds: None)

    with (
        patch("hermes_cli.profiles._check_gateway_running", return_value=False),
        patch("hermes_cli.profiles._cleanup_gateway_service"),
        patch("hermes_cli.profiles._maybe_unregister_gateway_service"),
        patch("hermes_cli.profiles._stop_profile_backends"),
        patch(
            "hermes_cli.profiles.shutil.rmtree",
            side_effect=PermissionError("locked"),
        ),
        pytest.raises(RuntimeError, match="Could not remove profile directory"),
    ):
        delete_profile("worker", yes=True)

    assert profile_dir.exists()
    store = SourceAgentBindingStore(db_path)
    try:
        assert len(store.list_bindings()) == 2
    finally:
        store.close()


def test_binding_cleanup_failure_does_not_undo_profile_deletion(
    deletion_env, monkeypatch, caplog
):
    root, profile_dir = deletion_env
    _seed_bindings(root)

    class _FailingStore:
        def __init__(self, db_path):
            self.db_path = db_path

        def delete_bindings_for_profile(self, _profile_name):
            raise OSError("binding db busy")

        def close(self):
            pass

    monkeypatch.setattr(
        "gateway.source_agent_binding.SourceAgentBindingStore", _FailingStore
    )

    removed = _delete_without_services()

    assert removed == profile_dir
    assert not profile_dir.exists()
    assert "binding db busy" in caplog.text


def test_caller_can_own_post_delete_binding_lifecycle(deletion_env):
    root, profile_dir = deletion_env
    db_path = _seed_bindings(root)

    _delete_without_services(cleanup_bindings=False)

    assert not profile_dir.exists()
    store = SourceAgentBindingStore(db_path)
    try:
        assert len(store.list_bindings()) == 2
    finally:
        store.close()
