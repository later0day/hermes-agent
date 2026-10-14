"""/agent must not re-route or delete under a running turn (restored from fork 01889c86df).

Re-binding a group while another member's turn runs splits that turn's follow-ups and interrupts
across two profile namespaces; deleting a profile under a live turn removes the home it is writing.
"""
from __future__ import annotations

import pytest

from tests.gateway.test_agent_command_basic import _event, _runner, _source, env  # noqa: F401  (fixture)


@pytest.mark.asyncio
async def test_use_and_clear_refuse_while_any_member_turn_runs_in_the_chat(env):
    r = _runner(env.store, audit_path=env.audit_path)
    # Another participant (u2) of the same group has a turn running under the old profile.
    r._running_agents = {"agent:coder:dingtalk:group:group-x:u2": object()}

    out = await r._handle_agent_command(_event(_source(user_id="u1"), "/agent use reviewer"))

    assert "Agent is running in this chat" in out
    assert env.store.list_bindings() == []
    assert "Agent is running in this chat" in await r._handle_agent_command(
        _event(_source(user_id="u1"), "/agent clear"))
    # A turn in a DIFFERENT chat does not block this one.
    r._running_agents = {"agent:coder:dingtalk:group:other-chat:u2": object()}
    assert "Bound this chat" in await r._handle_agent_command(_event(_source(user_id="u1"), "/agent use reviewer"))


@pytest.mark.asyncio
async def test_delete_refuses_while_the_profile_has_active_turns(env):
    r = _runner(env.store, audit_path=env.audit_path)
    r._running_agents = {"agent:coder:telegram:dm:42": object()}

    out = await r._handle_agent_command(_event(_source(), "/agent delete Coder"))  # name is normalized

    assert "while 1 gateway turn(s) are active" in out
    assert (env.root / "profiles" / "coder").is_dir()
