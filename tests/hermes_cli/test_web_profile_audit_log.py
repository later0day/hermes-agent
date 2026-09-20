"""Profile lifecycle mutations (create/rename/delete/import) emit a structured
``profile_audit`` INFO line via the shared ``hermes_cli.web_server`` logger
(port of fork 9d48544d2c).

Before this, only a mutation's *failure* left a trace (``_log.exception``) — a
successful create/rename/delete/import left errors.log/agent.log silent, so
there was no way to answer "who removed profile X, and when". The audit line
carries no secrets: action, profile name, outcome, and an optional bounded
non-secret detail (rename target, clone source, import archive basename).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Path.home() and HERMES_HOME both point into the temp dir (profile ops are HOME-anchored)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return tmp_path


@pytest.fixture()
def client(profile_env):
    from hermes_cli import web_server

    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {web_server._SESSION_TOKEN}"
        yield c


def _audit_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("profile_audit ")]


@pytest.fixture(autouse=True)
def _capture_audit(caplog):
    caplog.set_level(logging.INFO, logger="hermes_cli.web_server")


def test_create_success_audits_ok(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "seed_profile_skills", lambda *a, **k: None)

    resp = client.post("/api/profiles", json={"name": "sable"})

    assert resp.status_code == 200, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='create'" in l and "profile='sable'" in l and "outcome='ok'" in l
        for l in lines
    ), lines


def test_create_duplicate_audits_rejected(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "seed_profile_skills", lambda *a, **k: None)
    client.post("/api/profiles", json={"name": "sable"})
    caplog.clear()

    resp = client.post("/api/profiles", json={"name": "sable"})

    assert resp.status_code == 400, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='create'" in l and "profile='sable'" in l and "outcome='rejected'" in l
        for l in lines
    ), lines


def test_rename_success_audits_ok_with_target_detail(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **k: None)
    profiles_mod.create_profile("oldname", no_alias=True)
    caplog.clear()

    resp = client.patch("/api/profiles/oldname", json={"new_name": "newname"})

    assert resp.status_code == 200, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='rename'" in l and "profile='oldname'" in l and "outcome='ok'" in l
        and "detail='new_name=newname'" in l
        for l in lines
    ), lines


def test_rename_missing_profile_audits_not_found(client, caplog):
    resp = client.patch("/api/profiles/ghost", json={"new_name": "whatever"})

    assert resp.status_code == 404, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='rename'" in l and "profile='ghost'" in l and "outcome='not_found'" in l
        for l in lines
    ), lines


def test_delete_success_audits_ok(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **k: None)
    profiles_mod.create_profile("gone", no_alias=True)
    caplog.clear()

    resp = client.delete("/api/profiles/gone")

    assert resp.status_code == 200, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='delete'" in l and "profile='gone'" in l and "outcome='ok'" in l
        for l in lines
    ), lines


def test_delete_settlement_pending_audits_ok_with_detail(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **k: None)
    monkeypatch.setattr(profiles_mod, "_live_default_multiplexer", lambda: True)
    profiles_mod.create_profile("gone", no_alias=True)
    caplog.clear()

    resp = client.delete("/api/profiles/gone")

    assert resp.status_code == 200, resp.text
    assert resp.json()["settlement_pending"] is True
    lines = _audit_lines(caplog)
    assert any(
        "action='delete'" in l and "profile='gone'" in l
        and "outcome='ok'" in l and "detail='settlement_pending'" in l
        for l in lines
    ), lines


def test_delete_filesystem_failure_audits_error(client, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **k: None)

    def boom(profile_dir, onexc_handler):
        raise OSError("device busy")

    monkeypatch.setattr(profiles_mod, "_rmtree_with_retry", boom)
    profiles_mod.create_profile("gone", no_alias=True)
    caplog.clear()

    resp = client.delete("/api/profiles/gone")

    assert resp.status_code == 500, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='delete'" in l and "profile='gone'" in l and "outcome='error'" in l
        for l in lines
    ), lines


def test_import_success_audits_ok_with_archive_detail(client, monkeypatch, caplog, tmp_path):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *a, **k: None)
    profiles_mod.create_profile("origin", no_alias=True)
    archive_path = profiles_mod.export_profile("origin", str(tmp_path / "origin_export"))
    caplog.clear()

    resp = client.post(
        "/api/profiles/import", json={"archive": str(archive_path), "name": "restored"}
    )

    assert resp.status_code == 200, resp.text
    imported_name = resp.json()["name"]
    lines = _audit_lines(caplog)
    assert any(
        "action='import'" in l and f"profile='{imported_name}'" in l
        and "outcome='ok'" in l and f"detail='archive={archive_path.name}'" in l
        for l in lines
    ), lines


def test_import_missing_archive_audits_not_found(client, caplog):
    resp = client.post("/api/profiles/import", json={"archive": "/nonexistent/archive.tar.gz"})

    assert resp.status_code == 404, resp.text
    lines = _audit_lines(caplog)
    assert any(
        "action='import'" in l and "profile='/nonexistent/archive.tar.gz'" in l
        and "outcome='not_found'" in l
        for l in lines
    ), lines
