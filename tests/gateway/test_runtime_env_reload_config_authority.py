"""Gateway runtime env reload and profile overlay tests."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import yaml

from gateway import run as gateway_run


def test_reload_runtime_env_preserves_config_max_turns(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"max_turns": 9000}}),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "HERMES_MAX_ITERATIONS=90\nOPENROUTER_API_KEY=fresh-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "9000")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["OPENROUTER_API_KEY"] == "fresh-key"
    assert os.environ["HERMES_MAX_ITERATIONS"] == "9000"


def test_reload_runtime_env_keeps_env_max_iterations_when_config_omits_key(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({"agent": {}}), encoding="utf-8")
    (hermes_home / ".env").write_text("HERMES_MAX_ITERATIONS=123\n", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["HERMES_MAX_ITERATIONS"] == "123"


def test_runtime_env_overlay_restores_profile_env_after_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "HERMES_INFERENCE_PROVIDER=openrouter\nOPENROUTER_API_KEY=profile-key\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fake_resolve_runtime_provider(requested=None, **_kwargs):
        return {
            "provider": requested,
            "api_key": os.environ.get("OPENROUTER_API_KEY"),
            "base_url": "https://example.test",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    runtime = gateway_run._resolve_runtime_agent_kwargs(hermes_home=profile_home)

    assert runtime["provider"] == "openrouter"
    assert runtime["api_key"] == "profile-key"
    assert os.environ.get("HERMES_INFERENCE_PROVIDER") is None
    assert os.environ.get("OPENROUTER_API_KEY") is None


def test_runtime_env_overlay_serializes_concurrent_profile_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    (profile_a / ".env").write_text("OPENROUTER_API_KEY=key-a\n", encoding="utf-8")
    (profile_b / ".env").write_text("OPENROUTER_API_KEY=key-b\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    observed: list[str] = []

    def fake_resolve_runtime_provider(requested=None, **_kwargs):
        first = os.environ.get("OPENROUTER_API_KEY")
        time.sleep(0.02)
        second = os.environ.get("OPENROUTER_API_KEY")
        assert first == second
        observed.append(first or "")
        return {
            "provider": requested or "openrouter",
            "api_key": first,
            "base_url": "https://example.test",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    results: list[str] = []

    def worker(home: Path):
        results.append(gateway_run._resolve_runtime_agent_kwargs(hermes_home=home)["api_key"])

    threads = [
        threading.Thread(target=worker, args=(profile_a,)),
        threading.Thread(target=worker, args=(profile_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["key-a", "key-b"]
    assert sorted(observed) == ["key-a", "key-b"]
    assert os.environ.get("OPENROUTER_API_KEY") is None
