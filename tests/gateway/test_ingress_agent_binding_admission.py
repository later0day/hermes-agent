"""A message in an ``/agent use``-bound chat of the primary bot is admitted for the BOUND profile.

The fork's binding stamps ``source.profile`` at ingress (``_bound_profile_for_source``); upstream's
session-authority admission re-derived the runtime profile from static ``profile_routes`` only, saw the
primary, and refused every message in a bound DingTalk group with ``profile_mismatch`` (live 2026-10-15).
Real GatewayRunner, real per-home session authorities, real binding store; only the bot is a stand-in."""
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner

DING = Platform("dingtalk")


@pytest.mark.asyncio
async def test_bound_chat_is_admitted_for_the_bound_profile(tmp_path, monkeypatch):
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_ingress_context import _binding, register_transport_home
    from gateway.source_agent_binding import SourceAgentBindingStore

    root, homes = _reserve_homes(tmp_path, monkeypatch, names=("sat",))
    process_ownership.reserve([home for _, home in homes])
    runner = None
    try:
        runner = _runner(root, homes)
        await initialize_gateway_runtime(runner)
        runner.adapters = {DING: object()}  # the primary's DingTalk bot (stand-in: no network)
        register_transport_home(runner, None, root)
        store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
        store.set_binding("source:dingtalk:group:cidBOUND==", "sat")
        runner._source_agent_binding_store = store

        bound = SessionSource(platform=DING, chat_id="cidBOUND==", chat_type="group", user_id="u1",
                              profile="sat")
        _adapter, _home, runtime_home, provenance = _binding(runner, bound, None)
        assert Path(runtime_home) == dict(homes)["sat"].resolve()
        assert provenance["runtime_home"] == str(dict(homes)["sat"].resolve())

        plain = SessionSource(platform=DING, chat_id="cidPLAIN==", chat_type="group", user_id="u1")
        _adapter, _home, runtime_home, _ = _binding(runner, plain, None)
        assert Path(runtime_home) == root.resolve()  # an unbound chat stays with the primary
        store.close()
    finally:
        for authority in list(getattr(runner, "session_authorities", []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)
