"""Tests for binding-store profile stamping (source→profile isolation).

The source→agent binding store maps an IM conversation (e.g. a DingTalk
group) to a profile. Historically that binding was consulted only when
resolving the turn's HERMES_HOME (``_resolve_profile_home_for_source``),
NOT when stamping ``source.profile`` at ingress. Because the session-key
namespace and busy-policy resolution both derive from ``source.profile``,
a bound group's turn ran under the right profile home but its session
history landed in the ACTIVE profile's namespace — an ①/② mismatch that
leaks conversation history across profiles.

This suite pins the fix: ``_binding_profile_for_source`` is the single
source of truth, shared by the ingress stamping path
(``_profile_name_for_source``) and the home resolver, so both agree on
which profile owns a bound conversation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.source_agent_binding import SourceAgentBindingStore


def _source(chat_id="group-jb", user_id="u1", chat_type="group"):
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


def _runner(*, multiplex, store=None, profile_routes=None):
    """A GatewayRunner with only the seams these methods touch."""
    runner = object.__new__(GatewayRunner)

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.multiplex_profiles = multiplex
    cfg.profile_routes = profile_routes
    runner.config = cfg
    if store is not None:
        runner._source_agent_binding_store = store
    return runner


@pytest.fixture
def bound_store(tmp_path: Path) -> SourceAgentBindingStore:
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    src = _source()
    from gateway.session import build_source_binding_key

    store.set_binding(build_source_binding_key(src), "jb")
    return store


# ── _binding_profile_for_source ─────────────────────────────────────────


def test_binding_profile_resolved(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._binding_profile_for_source(_source()) == "jb"


def test_binding_profile_none_when_unbound(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._binding_profile_for_source(_source(chat_id="other")) is None


def test_binding_profile_none_when_store_missing():
    # No _source_agent_binding_store attribute at all → best-effort None.
    r = _runner(multiplex=True)
    assert r._binding_profile_for_source(_source()) is None


def test_binding_profile_swallows_lookup_errors():
    class _Boom:
        def get_binding(self, key):
            raise RuntimeError("db exploded")

    r = _runner(multiplex=True, store=_Boom())
    # Must not raise — routing falls through to the active profile.
    assert r._binding_profile_for_source(_source()) is None


# ── _profile_name_for_source (ingress stamping path) ────────────────────


def test_ingress_stamp_uses_binding(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._profile_name_for_source(_source()) == "jb"


def test_ingress_stamp_none_when_multiplex_off(bound_store):
    # Byte-for-byte legacy behavior: no stamping when multiplexing is off.
    r = _runner(multiplex=False, store=bound_store)
    assert r._profile_name_for_source(_source()) is None


def test_ingress_stamp_none_when_unbound_no_routes(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._profile_name_for_source(_source(chat_id="other")) is None


def test_binding_takes_precedence_over_routes(bound_store):
    # A configured profile_route must NOT override an explicit binding.
    from gateway.profile_routing import ProfileRoute

    route = ProfileRoute(
        name="r1", profile="routed", platform="dingtalk", chat_id="group-jb",
    )
    r = _runner(multiplex=True, store=bound_store, profile_routes=[route])
    assert r._profile_name_for_source(_source()) == "jb"


# ── the ①/② agreement invariant (the whole point) ──────────────────────


def test_session_key_namespace_matches_bound_profile(bound_store):
    """After ingress stamps source.profile from the binding, the session key
    namespace and the turn's profile home BOTH resolve to the bound profile."""
    r = _runner(multiplex=True, store=bound_store)
    src = _source()

    # ingress stamp
    src.profile = r._profile_name_for_source(src)
    assert src.profile == "jb"

    # ① session-key namespace
    sk = build_session_key(src, profile=src.profile)
    assert sk.split(":")[1] == "jb"

    # ② turn home resolution agrees (reuses the same binding helper)
    from unittest.mock import patch

    with patch("hermes_cli.profiles.get_profile_dir",
               return_value=Path("/hermes/profiles/jb")), \
         patch("hermes_cli.profiles.profile_exists", return_value=True), \
         patch("hermes_cli.profiles.get_active_profile_name", return_value="xcx"):
        home = r._resolve_profile_home_for_source(_source())
    assert home == Path("/hermes/profiles/jb")


def test_unbound_source_stays_in_active_namespace(bound_store):
    """An unbound conversation keeps legacy behavior: session key namespace
    is agent:main (profile=None) and home falls back to the active profile."""
    r = _runner(multiplex=True, store=bound_store)
    src = _source(chat_id="unbound")
    src.profile = r._profile_name_for_source(src)  # None
    assert src.profile is None
    sk = build_session_key(src, profile=src.profile)
    assert sk.split(":")[1] == "main"
