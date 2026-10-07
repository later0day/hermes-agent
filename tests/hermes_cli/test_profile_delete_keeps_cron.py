"""A refused profile delete must leave that profile's cron jobs alone.

The dashboard delete endpoint used to wipe the profile's cron store before validating the
delete, so ``DELETE /api/profiles/default`` answered 400 and still destroyed every job.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException


def test_refused_delete_keeps_the_profiles_cron_jobs(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_cli.web_server  # noqa: F401  — mounts the routers' late() targets
    from hermes_cli.web_routers.profiles import delete_profile_endpoint
    from hermes_cli.web_server_cron import _call_cron_for_profile

    _call_cron_for_profile("default", "create_job", prompt="keep me", schedule="every 1h", name="precious")
    before = [j["name"] for j in _call_cron_for_profile("default", "list_jobs", True)]

    with pytest.raises(HTTPException):
        asyncio.run(delete_profile_endpoint("default"))

    assert [j["name"] for j in _call_cron_for_profile("default", "list_jobs", True)] == before == ["precious"]
