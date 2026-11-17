"""Without a configured secret, basic auth signs with ONE key per process.

Every management-profile switch re-runs ``register()``; a key minted per call rotated the signing secret
and bounced a logged-in user to /login seconds later (``session_verify_failure``).
"""
from __future__ import annotations

import importlib


def test_unconfigured_secret_is_stable_across_re_registration(monkeypatch):
    basic = importlib.import_module("plugins.dashboard_auth.basic")
    monkeypatch.delenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", raising=False)
    monkeypatch.setattr(basic, "_FALLBACK_SECRET", None)

    first = basic._resolve_secret({})

    assert first and basic._resolve_secret({}) == first
    assert basic._resolve_secret({"secret": "explicit-key"}) != first  # an explicit secret still wins
