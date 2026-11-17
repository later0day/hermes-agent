"""config.yaml ``dingtalk.allow_all_users`` must reach ``DINGTALK_ALLOW_ALL_USERS`` env.

Invariant: every DingTalk authz gate configurable in ``config.yaml`` must be visible to
the code path that actually consults it. ``allow_all_users`` regressed because
``authz_mixin`` reads the env (via the ``allow_all_env`` hook registered at ``register``
time), so a YAML-only ``allow_all_users: true`` was silently dropped -- every unrecognized
DM fell through to the pairing prompt under multiplex with an installed secret-scope.
"""

import importlib
import sys

import pytest


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.dingtalk.adapter", None)
    return importlib.import_module("plugins.platforms.dingtalk.adapter")


def test_yaml_bridge_row_present(adapter_mod):
    """The ``_YAML_BRIDGE`` table must include an ``allow_all_users`` row so
    ``apply_yaml_bridge`` seeds both ``extra`` and the env var the adapter's
    ``allow_all_env`` hook consults."""
    rows = {key: (env, kind) for key, env, kind in adapter_mod._YAML_BRIDGE}
    assert rows.get("allow_all_users") == ("DINGTALK_ALLOW_ALL_USERS", "lower")


def test_yaml_allow_all_users_sets_env_and_extra(adapter_mod, monkeypatch):
    monkeypatch.delenv("DINGTALK_ALLOW_ALL_USERS", raising=False)
    seeded = adapter_mod._apply_yaml_config({}, {"allow_all_users": True})
    assert seeded is not None
    assert seeded.get("allow_all_users") is True
    # apply_yaml_bridge encodes "lower" values via str(v).lower()
    import os
    assert os.environ.get("DINGTALK_ALLOW_ALL_USERS") == "true"


def test_yaml_allow_all_users_false_still_bridged(adapter_mod, monkeypatch):
    """``lower`` kind bridges whenever the key is present -- including
    ``false``. This lets an operator explicitly disable the gate via YAML
    without having to unset the env var."""
    monkeypatch.delenv("DINGTALK_ALLOW_ALL_USERS", raising=False)
    seeded = adapter_mod._apply_yaml_config({}, {"allow_all_users": False})
    assert seeded is not None
    assert seeded.get("allow_all_users") is False
    import os
    assert os.environ.get("DINGTALK_ALLOW_ALL_USERS") == "false"


def test_existing_env_wins_over_yaml(adapter_mod, monkeypatch):
    """``yaml_env_setter`` documents 'env wins over YAML': a pre-existing env
    value must not be overwritten by ``_apply_yaml_config``."""
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "false")
    seeded = adapter_mod._apply_yaml_config({}, {"allow_all_users": True})
    # extra still reflects the YAML input (for adapters that read extra directly)
    assert seeded is not None
    assert seeded.get("allow_all_users") is True
    # but the env var the ``allow_all_env`` hook consults stays as operator set
    import os
    assert os.environ.get("DINGTALK_ALLOW_ALL_USERS") == "false"


def test_yaml_key_absent_leaves_env_untouched(adapter_mod, monkeypatch):
    """A DingTalk section without ``allow_all_users`` must not write the env
    var -- otherwise partial configs would silently flip the gate."""
    monkeypatch.delenv("DINGTALK_ALLOW_ALL_USERS", raising=False)
    seeded = adapter_mod._apply_yaml_config({}, {})
    # seeded may be None (nothing bridged) or a dict without the key
    if seeded is not None:
        assert "allow_all_users" not in seeded
    import os
    assert "DINGTALK_ALLOW_ALL_USERS" not in os.environ
