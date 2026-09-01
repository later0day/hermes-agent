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
def bound_store(tmp_path: Path, monkeypatch) -> SourceAgentBindingStore:
    root = tmp_path / "hermes-home"
    (root / "profiles" / "jb").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "bindings.sqlite")
    src = _source()
    from gateway.session import build_source_binding_key

    store.set_binding(build_source_binding_key(src), "jb")
    yield store
    store.close()


# ── _binding_profile_for_source ─────────────────────────────────────────


def test_binding_profile_resolved(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._binding_profile_for_source(_source()) == "jb"


def test_binding_profile_none_when_unbound(bound_store):
    r = _runner(multiplex=True, store=bound_store)
    assert r._binding_profile_for_source(_source(chat_id="other")) is None


def test_binding_profile_none_when_store_missing(tmp_path, monkeypatch):
    # No runner attribute: lazily open the configured store, without touching
    # the developer's real ~/.hermes during the test.
    store = SourceAgentBindingStore(db_path=tmp_path / "lazy.sqlite")
    monkeypatch.setattr(
        "gateway.source_agent_binding.SourceAgentBindingStore", lambda: store
    )
    r = _runner(multiplex=True)
    try:
        assert r._binding_profile_for_source(_source()) is None
        assert r._source_agent_binding_store is store
    finally:
        store.close()


def test_binding_profile_swallows_lookup_errors():
    class _Boom:
        def get_binding(self, key):
            raise RuntimeError("db exploded")

    r = _runner(multiplex=True, store=_Boom())
    # Must not raise — routing falls through to the active profile.
    assert r._binding_profile_for_source(_source()) is None


def test_stale_binding_is_ignored_and_removed(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "stale.sqlite")
    src = _source(chat_id="stale")
    from gateway.session import build_source_binding_key

    key = build_source_binding_key(src)
    store.set_binding(key, "missing")
    runner = _runner(multiplex=True, store=store)

    assert runner._binding_profile_for_source(src) is None
    assert store.get_binding(key) is None
    store.close()


def test_invalid_binding_profile_is_ignored_before_path_resolution(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "invalid.sqlite")
    src = _source(chat_id="invalid")
    from gateway.session import build_source_binding_key

    key = build_source_binding_key(src)
    store.set_binding(key, "../../outside")
    runner = _runner(multiplex=True, store=store)

    assert runner._binding_profile_for_source(src) is None
    assert store.get_binding(key) is None
    store.close()

def test_binding_profile_name_is_canonicalized(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home"
    (root / "profiles" / "worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    store = SourceAgentBindingStore(db_path=tmp_path / "canonical.sqlite")
    src = _source(chat_id="canonical")
    from gateway.session import build_source_binding_key

    key = build_source_binding_key(src)
    store.set_binding(key, "Worker")
    runner = _runner(multiplex=True, store=store)

    assert runner._binding_profile_for_source(src) == "worker"
    assert store.get_binding(key).profile_name == "worker"
    store.close()


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

    # ② turn home resolution agrees through the real profile path.
    home = r._resolve_profile_home_for_source(_source())
    assert home.name == "jb"
    assert home.parent.name == "profiles"


def test_unbound_source_stays_in_active_namespace(bound_store):
    """An unbound conversation keeps legacy behavior: session key namespace
    is agent:main (profile=None) and home falls back to the active profile."""
    r = _runner(multiplex=True, store=bound_store)
    src = _source(chat_id="unbound")
    src.profile = r._profile_name_for_source(src)  # None
    assert src.profile is None
    sk = build_session_key(src, profile=src.profile)
    assert sk.split(":")[1] == "main"
