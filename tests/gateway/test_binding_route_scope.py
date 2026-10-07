"""``/agent use`` bindings obey the same scope as a ``profile_routes`` entry without ``bot_profile``:
only the primary bot's chats, and only profiles this gateway serves (#104933)."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.profile_routing import ProfileRouteRejected
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.source_agent_binding import SourceAgentBindingStore

SERVED = [("default", Path("/p/default")), ("coder", Path("/p/coder"))]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    for name in ("coder", "solo"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "config.yaml").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    r = object.__new__(GatewayRunner)
    r.config = SimpleNamespace(multiplex_profiles=True, profile_routes=[],
                               group_sessions_per_user=False, thread_sessions_per_user=False)
    r._source_agent_binding_store = SourceAgentBindingStore(db_path=tmp_path / "b.sqlite")
    r._profiles_being_deleted = set()
    yield r
    r._source_agent_binding_store.close()


def _bound_source(runner, profile):
    source = SessionSource(platform=Platform.DINGTALK, chat_id="g1", chat_type="group", user_id="u")
    runner._source_agent_binding_store.set_binding("source:dingtalk:group:g1", profile)
    return source


def test_a_binding_to_a_profile_this_gateway_does_not_serve_is_rejected(runner):
    source = _bound_source(runner, "solo")
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=SERVED):
        with pytest.raises(ProfileRouteRejected):
            runner._profile_name_for_source(source)


def test_a_binding_routes_the_primary_bots_chat_but_not_a_secondary_bots(runner):
    source = _bound_source(runner, "coder")
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=SERVED):
        assert runner._profile_name_for_source(source) == "coder"
        assert runner._profile_name_for_source(source, adapter_profile="solo") is None
