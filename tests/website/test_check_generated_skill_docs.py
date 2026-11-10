"""Exercise the generated-skill-docs checker in real temporary git repos."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "website/scripts/check_generated_skill_docs.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


@pytest.fixture()
def docs_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "website/docs").mkdir(parents=True)
    (repo / "website/i18n").mkdir()
    (repo / "website/docs/existing.md").write_text("committed docs\n", encoding="utf-8")
    (repo / "website/sidebars.ts").write_text("export default [];\n", encoding="utf-8")
    script = repo / "website/scripts/check_generated_skill_docs.sh"
    script.parent.mkdir()
    shutil.copy2(CHECK_SCRIPT, script)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Docs Test")
    _git(repo, "config", "user.email", "docs@example.test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial docs")
    return repo


def _check(repo: Path, *generator: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "website/scripts/check_generated_skill_docs.sh"), *generator],
        cwd=repo,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _index_bytes(repo: Path) -> bytes:
    return (repo / ".git/index").read_bytes()


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout


def test_generator_mode_tolerates_hand_edits_and_preserves_real_index(docs_repo: Path) -> None:
    (docs_repo / "website/docs/existing.md").write_text("local hand edit\n", encoding="utf-8")
    (docs_repo / "website/docs/untracked-draft.md").write_text("local draft\n", encoding="utf-8")
    (docs_repo / "release.txt").write_text("staged elsewhere\n", encoding="utf-8")
    _git(docs_repo, "add", "release.txt")
    before_index = _index_bytes(docs_repo)
    before_status = _status(docs_repo)

    result = _check(docs_repo, sys.executable, "-c", "pass")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _index_bytes(docs_repo) == before_index
    assert _status(docs_repo) == before_status


def test_no_generator_reports_preexisting_doc_drift_without_touching_index(docs_repo: Path) -> None:
    (docs_repo / "website/docs/existing.md").write_text("uncommitted output\n", encoding="utf-8")
    before_index = _index_bytes(docs_repo)

    result = _check(docs_repo)

    assert result.returncode == 1
    assert "output differs from the committed docs" in result.stdout
    assert _index_bytes(docs_repo) == before_index


def test_generator_change_to_tracked_page_is_genuine_drift(docs_repo: Path) -> None:
    generated = docs_repo / "website/docs/existing.md"
    generator = (
        "from pathlib import Path; "
        f"Path({str(generated)!r}).write_text('generated replacement\\n', encoding='utf-8')"
    )
    before_index = _index_bytes(docs_repo)

    result = _check(docs_repo, sys.executable, "-c", generator)

    assert result.returncode == 1
    assert "existing.md" in result.stdout
    assert "output differs from the committed docs" in result.stdout
    assert _index_bytes(docs_repo) == before_index


def test_untracked_generated_page_fails_without_real_intent_to_add(docs_repo: Path) -> None:
    generated = docs_repo / "website/docs/new-generated.md"
    generator = (
        "from pathlib import Path; "
        f"Path({str(generated)!r}).write_text('new generated page\\n', encoding='utf-8')"
    )
    before_index = _index_bytes(docs_repo)

    result = _check(docs_repo, sys.executable, "-c", generator)

    assert result.returncode == 1
    assert "new-generated.md" in result.stdout
    assert generated.read_text(encoding="utf-8") == "new generated page\n"
    assert _index_bytes(docs_repo) == before_index
    assert _git(docs_repo, "ls-files", "--", "website/docs/new-generated.md").stdout == ""
    assert "?? website/docs/new-generated.md" in _status(docs_repo)
