"""Real disk-cache flow for an external-process provider's model catalog.

The test plugin launches the same resolved executable that the catalog fingerprint identifies.
That makes an unrelated vendor credential rewrite observably cache-friendly, while provider-owned
credential changes and symlink upgrades both cause a real new process launch and fresh picker rows.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest


@pytest.mark.platforms("posix")
def test_model_catalog_cache_tracks_only_provider_identity(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    os_home = tmp_path / "os-home"
    hermes_home.mkdir()
    os_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(os_home))

    import providers

    monkeypatch.setattr(providers, "_HOME_LAYERS", {})
    monkeypatch.setattr(providers, "_PLUGIN_DIR_STAMP_TTL_SECONDS", 0.0)
    providers.list_providers()  # Finish bundled discovery before installing the test plugin.

    provider_account = os_home / ".cache-e2e" / "account.json"
    provider_account.parent.mkdir()
    provider_account.write_text('{"account": "account-one"}', encoding="utf-8")
    vendor_credentials = os_home / ".claude" / ".credentials.json"
    vendor_credentials.parent.mkdir()
    vendor_credentials.write_text('{"accessToken": "vendor-one"}', encoding="utf-8")

    invocations = tmp_path / "invocations.txt"
    versions = tmp_path / "versions"
    versions.mkdir()
    for version in ("v1", "v2"):
        executable = versions / version
        executable.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(f"""
                import json
                from pathlib import Path

                with Path({str(invocations)!r}).open("a", encoding="utf-8") as log:
                    log.write("{version}\\n")
                account = json.loads(Path({str(provider_account)!r}).read_text(encoding="utf-8"))["account"]
                print(f"{version}:{{account}}")
            """),
            encoding="utf-8",
        )
        executable.chmod(0o755)

    launch = tmp_path / "bin" / "cache-e2e"
    launch.parent.mkdir()
    launch.symlink_to(versions / "v1")
    monkeypatch.setenv("CACHE_E2E_COMMAND", str(launch))

    plugin_dir = hermes_home / "plugins" / "cache-e2e"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: cache-e2e\nkind: model-provider\nversion: 1.0.0\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(textwrap.dedent(f"""
        import subprocess

        from hermes_cli.auth_external_process import external_process_command, resolve_external_process_command
        from providers import register_provider
        from providers.base import ProviderProfile


        class ProcessCatalogProfile(ProviderProfile):
            def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
                command = resolve_external_process_command(external_process_command(self))
                result = subprocess.run(
                    [command], capture_output=True, text=True, check=True, timeout=5,
                )
                return result.stdout.splitlines()


        register_provider(ProcessCatalogProfile(
            name="cache-e2e",
            auth_type="external_process",
            base_url="acp://cache-e2e",
            process_command={str(launch)!r},
            process_command_env_vars=("CACHE_E2E_COMMAND",),
            catalog_credential_files=({str(provider_account)!r},),
        ))
    """), encoding="utf-8")
    settled = time.time() - 60
    os.utime(hermes_home / "plugins", (settled, settled))

    assert providers.get_provider_profile("cache-e2e") is not None

    from hermes_cli.models import cached_provider_model_ids

    cache_path = hermes_home / "provider_models_cache.json"

    def cache_entry():
        return json.loads(cache_path.read_text(encoding="utf-8"))["cache-e2e"]

    assert cached_provider_model_ids("cache-e2e") == ["v1:account-one"]
    first_entry = cache_entry()
    assert first_entry["models"] == ["v1:account-one"]
    assert invocations.read_text(encoding="utf-8").splitlines() == ["v1"]

    vendor_credentials.write_text('{"accessToken": "vendor-two"}', encoding="utf-8")
    assert cached_provider_model_ids("cache-e2e") == ["v1:account-one"]
    assert cache_entry() == first_entry
    assert invocations.read_text(encoding="utf-8").splitlines() == ["v1"]

    provider_account.write_text('{"account": "account-two"}', encoding="utf-8")
    assert cached_provider_model_ids("cache-e2e") == ["v1:account-two"]
    second_entry = cache_entry()
    assert second_entry["fp"] != first_entry["fp"]
    assert second_entry["models"] == ["v1:account-two"]
    assert invocations.read_text(encoding="utf-8").splitlines() == ["v1", "v1"]

    launch.unlink()
    launch.symlink_to(versions / "v2")
    assert cached_provider_model_ids("cache-e2e") == ["v2:account-two"]
    third_entry = cache_entry()
    assert third_entry["fp"] != second_entry["fp"]
    assert third_entry["models"] == ["v2:account-two"]
    assert invocations.read_text(encoding="utf-8").splitlines() == ["v1", "v1", "v2"]
