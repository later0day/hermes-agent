"""/agent through the gateway's real slash dispatch and slash-access policy.

The handler tests call ``_handle_agent_command`` directly, which is how the command shipped
unreachable: it was never in the dispatch tables, so ``/agent use x`` went to the model as
chat text. These go through ``_hm_dispatch_canonical_command`` and the real admin policy.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_source_binding_key
from gateway.source_agent_binding import SourceAgentBindingStore


@pytest.fixture
def runner(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    (root / "profiles" / "coder").mkdir(parents=True)
    (root / "profiles" / "coder" / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    r = object.__new__(GatewayRunner)
    r.config = SimpleNamespace(
        multiplex_profiles=True, profile_routes=[], platforms={},
        group_sessions_per_user=True, thread_sessions_per_user=False,
    )
    r._source_agent_binding_store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    r._profiles_being_deleted = set()
    r._agent_audit_path = None

    @contextlib.asynccontextmanager
    async def _scope(_source):
        yield

    r._async_profile_scope_for_source = _scope
    yield r
    r._source_agent_binding_store.close()


async def _dispatch(runner, text, user_id):
    source = SessionSource(platform=Platform.DINGTALK, chat_id="group-x", chat_type="group", user_id=user_id)
    event = SimpleNamespace(source=source, text=text, raw_message=None)
    return source, await runner._hm_dispatch_canonical_command(event, source, "k", "agent")


@pytest.mark.asyncio
async def test_agent_command_is_handled_by_the_gateway_not_sent_to_the_model(runner):
    _source, (handled, reply) = await _dispatch(runner, "/agent status", "u1")
    assert handled is True
    assert reply and "binding" in reply.lower()


@pytest.mark.asyncio
async def test_only_an_explicitly_configured_admin_can_rebind_a_chat(runner):
    def bound(source):
        key = build_source_binding_key(source, group_sessions_per_user=True, thread_sessions_per_user=False)
        return runner._source_agent_binding_store.get_binding(key)

    # Default config: slash gating is off, which must not mean "everyone may rebind".
    source, (handled, reply) = await _dispatch(runner, "/agent use coder", "u1")
    assert handled and "admin" in reply and bound(source) is None

    runner.config.platforms = {Platform.DINGTALK: {"group_allow_admin_from": ["boss"]}}
    source, (_, reply) = await _dispatch(runner, "/agent use coder", "u1")
    assert "admin" in reply and bound(source) is None

    source, _ = await _dispatch(runner, "/agent use coder", "boss")
    assert bound(source).profile_name == "coder"
