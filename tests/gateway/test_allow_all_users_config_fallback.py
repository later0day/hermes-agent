"""The config.yaml fallback for ``{PLATFORM}_ALLOW_ALL_USERS`` must be safe.

The fork added ``_platform_config_allow_all_users`` so an operator can grant
open access from ``gateway.platforms.<p>.extra.allow_all_users`` in
config.yaml instead of the environment.  Because this is an *authorization*
path, the fallback carries three security-critical invariants that had no
test coverage:

1. It fires **only** when the platform's ``{PLATFORM}_ALLOW_ALL_USERS`` env var
   is absent/empty — an explicitly-set env var (true *or* false) is always
   authoritative and the config is never consulted.  This is what stops a
   config ``allow_all_users: true`` from silently overriding an operator's
   deliberate ``DINGTALK_ALLOW_ALL_USERS=false`` in ``.env``.
2. Absent both env var and config key, the platform default-denies
   (SECURITY.md §2.6: network-exposed adapters must not fail open).
3. The config value is parsed strictly — only true-ish strings/bools open the
   gate; anything else (including a bare present key with a false-ish value)
   denies.

These tests pin all three.  ``DINGTALK`` is used as the concrete platform
because it is a network-exposed adapter with an entry in
``platform_allow_all_map`` and does NOT enforce its own access policy, so the
allow-all gate is the deciding factor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource

_ENV_KEYS = (
    "DINGTALK_ALLOW_ALL_USERS",
    "DINGTALK_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
)


def _clear_env(monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _runner(config: GatewayConfig):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    # DingTalk does not own its access policy — the allow-all gate decides.
    adapter = SimpleNamespace(send=AsyncMock(), enforces_own_access_policy=False)
    runner.adapters = {Platform.DINGTALK: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    return runner


def _config(*, allow_all_users=None) -> GatewayConfig:
    extra = {}
    if allow_all_users is not None:
        extra["allow_all_users"] = allow_all_users
    return GatewayConfig(
        platforms={Platform.DINGTALK: PlatformConfig(enabled=True, extra=extra)}
    )


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DINGTALK,
        user_id="stranger",
        chat_id="cid-1",
        user_name="stranger",
        chat_type="dm",
    )


# ---------------------------------------------------------------------------
# Invariant 1: env is authoritative when set.
# ---------------------------------------------------------------------------
def test_env_true_authorizes_regardless_of_config(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "true")
    assert _runner(_config(allow_all_users=False))._is_user_authorized(_source()) is True


def test_env_false_denies_even_if_config_says_true(monkeypatch):
    """An explicit env=false must NOT be overridden by config=true."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "false")
    assert _runner(_config(allow_all_users=True))._is_user_authorized(_source()) is False


# ---------------------------------------------------------------------------
# Invariant 2: config fallback only when env is absent/empty.
# ---------------------------------------------------------------------------
def test_config_true_authorizes_when_env_absent(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=True))._is_user_authorized(_source()) is True


def test_empty_env_falls_back_to_config_true(monkeypatch):
    """An env var set to the empty string is treated as unset -> config wins."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "")
    assert _runner(_config(allow_all_users=True))._is_user_authorized(_source()) is True


def test_empty_env_and_no_config_default_denies(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "")
    assert _runner(_config())._is_user_authorized(_source()) is False


def test_no_env_no_config_default_denies(monkeypatch):
    """SECURITY.md §2.6: no allowlist configured must fail closed."""
    _clear_env(monkeypatch)
    assert _runner(_config())._is_user_authorized(_source()) is False


# ---------------------------------------------------------------------------
# Invariant 3: strict parsing of the config value.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "TRUE", " Yes ", True])
def test_config_truthy_values_open_the_gate(monkeypatch, truthy):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=truthy))._is_user_authorized(_source()) is True


@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "", "maybe", False])
def test_config_falsy_values_keep_the_gate_closed(monkeypatch, falsy):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=falsy))._is_user_authorized(_source()) is False
