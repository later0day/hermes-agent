"""Text-only DashScope chat models inherit a working vision backend; vision-capable ones see natively.

``auxiliary.vision`` on auto resolves the main provider's ``default_vision_model()``; DashScope declared
none, so every profile on ``qwen-plus`` had no vision at all (it fell through to an unconfigured Nous).
"""
from __future__ import annotations

import pytest


def _write_config(home, body):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "hh"
    monkeypatch.setenv("HERMES_HOME", str(path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    return path


MAIN = ("model:\n  provider: alibaba\n  default: qwen-plus\n"
        "  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1\n")


def test_text_only_dashscope_profile_inherits_kimi_vision(home):
    _write_config(home, MAIN)
    from agent.auxiliary_client import resolve_vision_provider_client

    provider, client, model = resolve_vision_provider_client()[:3]
    assert (provider, model) == ("alibaba", "kimi-k3") and client is not None


def test_an_explicit_vision_backend_still_wins(home):
    _write_config(home, MAIN + "auxiliary:\n  vision:\n    provider: alibaba\n    model: qwen3-vl-plus\n")
    from agent.auxiliary_client import resolve_vision_provider_client

    assert resolve_vision_provider_client()[2] == "qwen3-vl-plus"


def test_token_plan_tiers_keep_the_generic_fallback():
    from providers import get_provider_profile

    assert get_provider_profile("alibaba").default_vision_model() == "kimi-k3"
    assert get_provider_profile("alibaba-token-plan").default_vision_model() is None
