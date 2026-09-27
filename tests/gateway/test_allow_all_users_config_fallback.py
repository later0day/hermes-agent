"""The config.yaml fallback for ``{PLATFORM}_ALLOW_ALL_USERS`` must be safe.

HEAD's ``_platform_config_allow_all_users`` (ported from the fork) lets an operator
grant open access from ``gateway.platforms.<p>.extra.allow_all_users`` in
config.yaml when the ``{PLATFORM}_ALLOW_ALL_USERS`` env var is absent/empty.
Because this is an *authorization* path, the fallback carries three
security-critical invariants:

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
``_ALLOW_ALL_ENV`` and does NOT enforce its own access policy, so the
allow-all gate is the deciding factor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.authz_mixin import GatewayAuthorizationMixin
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


def _runner(config: GatewayConfig) -> GatewayAuthorizationMixin:
    runner = object.__new__(GatewayAuthorizationMixin)
    runner.config = config
    runner.adapters = {Platform.DINGTALK: SimpleNamespace(enforces_own_access_policy=False)}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
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
def test_config_bool_true_opens_the_gate(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=True))._is_user_authorized(_source()) is True


def test_config_bool_false_keeps_the_gate_closed(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=False))._is_user_authorized(_source()) is False


def test_config_string_true_opens_the_gate(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="true"))._is_user_authorized(_source()) is True


def test_config_string_yes_opens_the_gate(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="yes"))._is_user_authorized(_source()) is True


def test_config_string_on_opens_the_gate(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="on"))._is_user_authorized(_source()) is True


def test_config_string_1_opens_the_gate(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="1"))._is_user_authorized(_source()) is True


def test_config_string_false_keeps_gate_closed(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="false"))._is_user_authorized(_source()) is False


def test_config_string_maybe_keeps_gate_closed(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users="maybe"))._is_user_authorized(_source()) is False


def test_config_string_empty_keeps_gate_closed(monkeypatch):
    _clear_env(monkeypatch)
    assert _runner(_config(allow_all_users=""))._is_user_authorized(_source()) is False


def test_config_absent_key_default_denies(monkeypatch):
    """No extra key at all → default-deny (no fail-open for network adapters)."""
    _clear_env(monkeypatch)
    assert _runner(_config())._is_user_authorized(_source()) is False
