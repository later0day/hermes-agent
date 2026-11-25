"""WellKnownSkillSource: reading skills from a domain's well-known Agent Skills index."""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from tools.skills_hub_sources import WellKnownSkillSource


@pytest.fixture
def allow_public_skill_fetches(monkeypatch):
    monkeypatch.setattr("tools.skills_hub.is_safe_url", lambda _url: True)
    monkeypatch.setattr("tools.skills_hub.check_website_access", lambda _url: None)


@pytest.fixture
def off_path_well_known_server(tmp_path, monkeypatch):
    """Serve an off-path index through the real SSRF-safe HTTP transport."""
    # Loopback is intentionally private; opt in only this test server rather
    # than bypassing the URL-policy or guarded-fetch layers.
    monkeypatch.setattr("tools.url_safety._global_allow_private_urls", lambda: True)
    root = tmp_path / "site"
    skill_dir = root / ".well-known/agent-skills/demo"
    skill_dir.mkdir(parents=True)
    (root / ".well-known/agent-skills/index.json").write_text(json.dumps({
        "skills": [{"name": "demo", "description": "Loopback demo"}],
    }), encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Loopback demo\n---\n# Demo\n",
        encoding="utf-8",
    )
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.usefixtures("allow_public_skill_fetches")
class TestWellKnownSkillSource:
    def _source(self):
        return WellKnownSkillSource()

    @patch("tools.skills_hub._write_index_cache")
    @patch("tools.skills_hub._read_index_cache", return_value=None)
    @patch("tools.skills_hub._ssrf_safe_http_get")
    def test_search_reads_index_from_well_known_url(self, mock_get, _mock_read_cache, _mock_write_cache):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "skills": [
                    {"name": "git-workflow", "description": "Git rules", "files": ["SKILL.md"]},
                    {"name": "code-review", "description": "Review code", "files": ["SKILL.md", "references/checklist.md"]},
                ]
            },
        )

        results = self._source().search("https://example.com/.well-known/skills/index.json", limit=10)

        assert [r.identifier for r in results] == [
            "well-known:https://example.com/.well-known/skills/git-workflow",
            "well-known:https://example.com/.well-known/skills/code-review",
        ]
        assert all(r.source == "well-known" for r in results)

    @patch("tools.skills_hub._write_index_cache")
    @patch("tools.skills_hub._read_index_cache", return_value=None)
    @patch("tools.skills_hub._ssrf_safe_http_get")
    def test_fetch_rejects_unsafe_file_paths_from_well_known_endpoint(self, mock_get, _mock_read_cache, _mock_write_cache):
        def fake_get(url, *args, **kwargs):
            if url.endswith("/index.json"):
                return MagicMock(status_code=200, json=lambda: {
                    "skills": [{
                        "name": "code-review",
                        "description": "Review code",
                        "files": ["SKILL.md", "../../../escape.txt"],
                    }]
                })
            if url.endswith("/code-review/SKILL.md"):
                return MagicMock(status_code=200, text="# Code Review\n")
            raise AssertionError(url)

        mock_get.side_effect = fake_get

        bundle = self._source().fetch("well-known:https://example.com/.well-known/skills/code-review")

        assert bundle is None

    @patch("tools.skills_hub._write_index_cache")
    @patch("tools.skills_hub._read_index_cache", return_value=None)
    @patch("tools.skills_hub._ssrf_safe_http_get")
    def test_identifier_recorded_for_index_outside_base_path_fetches_again(self, mock_get, _mock_read_cache, _mock_write_cache):
        """Regression for #159541: the identifier an install records for an index at another path
        (``/.well-known/agent-skills/``) must fetch again, or check/update report it unavailable."""
        def fake_get(url, *args, **kwargs):
            if url == "https://example.com/.well-known/agent-skills/index.json":
                return MagicMock(status_code=200, json=lambda: {"skills": [{"name": "demo", "description": "d"}]})
            if url == "https://example.com/.well-known/agent-skills/demo/SKILL.md":
                return MagicMock(status_code=200, text="---\nname: demo\n---\n# Demo\n")
            raise AssertionError(url)

        mock_get.side_effect = fake_get
        installed = self._source().fetch("well-known:https://example.com/.well-known/agent-skills/index.json#demo")

        refetched = self._source().fetch(installed.identifier)

        assert refetched is not None and refetched.files == installed.files

        mock_get.reset_mock()
        assert self._source().fetch("https://example.com/.well-known/agent-skills/demo/SKILL.md") is None
        mock_get.assert_not_called()


def test_off_path_identifier_refetches_and_selects_update_over_real_http(
        off_path_well_known_server, tmp_path, monkeypatch):
    """The recorded off-path identifier survives real HTTP and installed-state update selection."""
    import tools.skills_hub as hub
    from tools.skills_guard import content_hash
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_install import check_for_skill_updates

    base_url, requests = off_path_well_known_server
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(hub, "SKILLS_DIR", skills_dir)
    source = WellKnownSkillSource()

    installed = source.fetch(f"{base_url}/.well-known/agent-skills/index.json#demo")

    assert installed is not None
    assert installed.identifier == f"well-known:{base_url}/.well-known/agent-skills/demo"
    refetched = source.fetch(installed.identifier)
    assert refetched is not None and refetched.files == installed.files

    install_dir = skills_dir / "demo"
    install_dir.mkdir(parents=True)
    (install_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Old demo\n", encoding="utf-8")
    lock = HubLockFile(skills_dir / ".hub/lock.json")
    lock.record_install(
        name="demo", source=installed.source, identifier=installed.identifier,
        trust_level=installed.trust_level, scan_verdict="safe",
        skill_hash=content_hash(install_dir), install_path="demo",
        files=list(installed.files), metadata=installed.metadata,
    )

    results = check_for_skill_updates(name="demo", lock=lock, sources=[source])

    assert len(results) == 1
    assert results[0]["identifier"] == installed.identifier
    assert results[0]["status"] == "update_available"
    assert results[0]["bundle"].files == installed.files

    from io import StringIO

    from rich.console import Console

    import hermes_cli.skills_hub as cli_hub
    import tools.skills_hub_search as hub_search

    monkeypatch.setattr(hub, "HubLockFile", lambda: lock)
    monkeypatch.setattr(hub_search, "create_source_router", lambda *args, **kwargs: [source])
    monkeypatch.setattr(cli_hub, "_sources", lambda: [source])
    output = StringIO()
    assert cli_hub.do_update(name="demo", console=Console(file=output, force_terminal=False)) is None

    assert "Updated 1 skill(s)" in output.getvalue()
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8") == installed.files["SKILL.md"]
    assert requests.count("/.well-known/agent-skills/index.json") == 1
    assert requests.count("/.well-known/agent-skills/demo/SKILL.md") == 6
