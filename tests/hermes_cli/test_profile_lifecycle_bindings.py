"""``/agent use`` bindings follow their profile through delete and rename.

A deleted profile's bindings used to stay in the root-global store, so a later profile of the same
name inherited its chats (and the cron delivery they authorize); a renamed profile's bindings were
dropped as stale on the next message, silently returning its chats to the default profile.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.source_agent_binding import SourceAgentBindingStore
from hermes_cli.profiles import create_profile, delete_profile, rename_profile


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _bindings(root):
    store = SourceAgentBindingStore(db_path=root / "gateway_source_agent_bindings.sqlite")
    try:
        return {b.source_binding_key: b.profile_name for b in store.list_bindings()}
    finally:
        store.close()


def _bind(root, key, profile):
    store = SourceAgentBindingStore(db_path=root / "gateway_source_agent_bindings.sqlite")
    store.set_binding(key, profile)
    store.close()


def test_deleting_a_profile_drops_its_bindings_only(home):
    create_profile("coder", no_alias=True)
    _bind(home, "source:dingtalk:group:A", "coder")
    _bind(home, "source:dingtalk:group:B", "default")

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch("hermes_cli.profiles.time.sleep"):
        delete_profile("coder", yes=True)

    assert _bindings(home) == {"source:dingtalk:group:B": "default"}


def test_renaming_a_profile_carries_its_bindings(home):
    create_profile("oldname", no_alias=True)
    _bind(home, "source:dingtalk:group:A", "oldname")

    with patch("hermes_cli.profiles.check_alias_collision", return_value="skip"):
        rename_profile("oldname", "newname")

    assert _bindings(home) == {"source:dingtalk:group:A": "newname"}
