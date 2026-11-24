"""Integration contracts for disabled model-provider plugins and live re-enablement.

These exercise the real config loader and per-home plugin discovery rather than
calling the disabled predicate directly. The enable case crosses a real CLI
process boundary while the original process retains its provider-home layer.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def provider_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    os_home = tmp_path / "os-home"
    home.mkdir()
    os_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(os_home))

    import providers

    monkeypatch.setattr(providers, "_HOME_LAYERS", {})
    monkeypatch.setattr(providers, "_PLUGIN_DIR_STAMP_TTL_SECONDS", 0.0)
    providers.list_providers()  # Finish bundled discovery before installing the test plugin.
    return home


def _write_import_marker_plugin(plugin_dir: Path, *, name: str, marker: Path) -> None:
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {name}\nkind: model-provider\nversion: 1.0.0\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        from providers import register_provider
        from providers.base import ProviderProfile

        with Path({str(marker)!r}).open("a", encoding="utf-8") as marker_file:
            marker_file.write("imported\\n")
        register_provider(ProviderProfile(name={name!r}, base_url="https://{name}.example/v1"))
    """), encoding="utf-8")


def _settle_plugin_parent(home: Path) -> None:
    settled = time.time() - 60
    os.utime(home / "plugins", (settled, settled))


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with os.scandir(source) as entries:
        for entry in entries:
            src, dst = Path(entry.path), target / entry.name
            if entry.is_symlink():
                dst.symlink_to(os.readlink(src))
            elif entry.is_dir(follow_symlinks=False):
                _copy_tree(src, dst)
            else:
                try:
                    os.link(src, dst)
                except OSError as exc:
                    if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
                        raise
                    shutil.copy2(src, dst)


def _seed_warm_pm(home: Path) -> None:
    """Keep this rediscovery test independent of cold PM provisioning and network speed."""
    from pm.environments import install_key, venv_python
    from pm.packages import Venv
    from pm.runtime import _inputs

    source_venv = Path(sys.prefix)
    source_generation = source_venv.parent
    assert source_generation.parent.name == "test-environment", source_generation
    state = home / "installs" / install_key(REPO_ROOT)
    app_generation = state / "environments" / "e2e"
    _copy_tree(source_generation, app_generation)
    app_python = venv_python(app_generation / "venv")
    (state / "facts.json").write_text(json.dumps({
        "schema": 1,
        "packages": {"venv": {
            "environment": str(app_python.parent.parent),
            "extras": [],
            "resolved_lock": str(REPO_ROOT / "uv.lock"),
            "stamp": Venv(REPO_ROOT).expected_stamp([], plugin_dirs=[]),
        }},
    }), encoding="utf-8")

    pm_generation = state / "pm-runtime" / "generations" / "seed"
    _copy_tree(source_venv, pm_generation)
    pm_python = venv_python(pm_generation)
    inputs = _inputs(REPO_ROOT, pm_python)
    (pm_generation / "pm-runtime.json").write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
    (state / "pm-runtime" / "selected.json").write_text(json.dumps({
        "inputs": inputs, "generation": "generations/seed",
    }), encoding="utf-8")


def test_disabled_providers_are_neither_imported_nor_host_extracted(provider_home, monkeypatch):
    flat_marker = provider_home.parent / "flat-imported.txt"
    nested_marker = provider_home.parent / "nested-imported.txt"
    _write_import_marker_plugin(
        provider_home / "plugins" / "flat-disabled", name="flat-disabled", marker=flat_marker
    )
    _write_import_marker_plugin(
        provider_home / "plugins" / "model-providers" / "nested-disabled",
        name="nested-disabled", marker=nested_marker,
    )
    (provider_home / "config.yaml").write_text(
        "plugins:\n"
        "  isolation: host\n"
        "  disabled:\n"
        "    - flat-disabled\n"
        "    - model-providers/nested-disabled\n",
        encoding="utf-8",
    )

    from hermes_cli import plugin_host_profiles

    hosted_calls = []
    monkeypatch.setattr(
        plugin_host_profiles, "load_hosted_profiles",
        lambda plugin_dir, module_name: hosted_calls.append((plugin_dir, module_name)) or ([], {}),
    )

    import providers

    assert providers.get_provider_profile("flat-disabled") is None
    assert providers.get_provider_profile("nested-disabled") is None
    assert hosted_calls == []
    assert not flat_marker.exists()
    assert not nested_marker.exists()
    assert not (provider_home / "cache" / "plugin_host" / "model-providers").exists()


def test_real_cli_enable_rediscovers_without_touching_plugin_stamps(provider_home):
    plugin_dir = provider_home / "plugins" / "rediscovery-e2e"
    marker = provider_home.parent / "rediscovery-imported.txt"
    _write_import_marker_plugin(plugin_dir, name="rediscovery-e2e", marker=marker)
    (provider_home / "config.yaml").write_text(
        "plugins:\n  disabled: [rediscovery-e2e]\n", encoding="utf-8"
    )
    _settle_plugin_parent(provider_home)

    import providers

    assert providers.get_provider_profile("rediscovery-e2e") is None
    assert not marker.exists()
    watched = (provider_home / "plugins", plugin_dir / "plugin.yaml", plugin_dir / "__init__.py")
    stamps_before = {path: path.stat().st_mtime_ns for path in watched}

    _seed_warm_pm(provider_home)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    try:
        enabled = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "plugins", "enable", "rediscovery-e2e"],
            cwd=provider_home, env=env, capture_output=True, text=True, timeout=90, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        pytest.fail(f"real CLI enable timed out\nstdout:\n{stdout}\nstderr:\n{stderr}")
    assert enabled.returncode == 0, enabled.stdout + enabled.stderr
    assert {path: path.stat().st_mtime_ns for path in watched} == stamps_before

    profile = providers.get_provider_profile("rediscovery-e2e")

    assert profile is not None
    assert profile.name == "rediscovery-e2e"
    assert marker.read_text(encoding="utf-8").splitlines() == ["imported"]
    assert {path: path.stat().st_mtime_ns for path in watched} == stamps_before


def test_ordinary_refresh_does_not_reread_disabled_without_a_prior_skip(provider_home, monkeypatch):
    plugin_dir = provider_home / "plugins" / "always-enabled"
    marker = provider_home.parent / "ordinary-refresh-imported.txt"
    _write_import_marker_plugin(plugin_dir, name="always-enabled", marker=marker)
    (provider_home / "config.yaml").write_text(
        "plugins:\n  disabled: [some-plugin-that-is-not-installed]\n", encoding="utf-8"
    )
    _settle_plugin_parent(provider_home)

    import providers

    assert providers.get_provider_profile("always-enabled") is not None
    assert marker.read_text(encoding="utf-8").splitlines() == ["imported"]

    from hermes_cli import plugins_discovery

    def unexpected_disabled_read():
        raise AssertionError("an ordinary no-skip refresh read plugins.disabled")

    monkeypatch.setattr(plugins_discovery, "_get_disabled_plugins", unexpected_disabled_read)
    providers.refresh_bound_home()

    assert providers.get_provider_profile("always-enabled") is not None
    assert marker.read_text(encoding="utf-8").splitlines() == ["imported"]
