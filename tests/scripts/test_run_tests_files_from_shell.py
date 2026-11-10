"""The canonical shell entry point executes a file-backed explicit test list."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_run_tests_shell_files_from_executes_only_the_listed_probe(tmp_path: Path) -> None:
    executed = tmp_path / "executed.txt"
    decoy_executed = tmp_path / "decoy-executed.txt"
    selected = tmp_path / "test_selected_probe.py"
    selected.write_text(
        "from pathlib import Path\n"
        "def test_selected():\n"
        f"    Path({str(executed)!r}).write_text('selected', encoding='utf-8')\n",
        encoding="utf-8",
    )
    decoy = tmp_path / "test_decoy_probe.py"
    decoy.write_text(
        "from pathlib import Path\n"
        "def test_decoy():\n"
        f"    Path({str(decoy_executed)!r}).write_text('decoy', encoding='utf-8')\n"
        "    raise AssertionError('an unlisted probe must not run')\n",
        encoding="utf-8",
    )
    files_from = tmp_path / "test-files.txt"
    files_from.write_text(f"\n{selected}\n\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts/run_tests.sh"),
            "--files-from",
            str(files_from),
            "-j",
            "1",
            "--file-timeout",
            "30",
            "-q",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HERMES_PYTHON": sys.executable,
            "HERMES_TEST_FILE_RETRIES": "0",
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert executed.read_text(encoding="utf-8") == "selected"
    assert not decoy_executed.exists()
    assert "Running 1 test files" in output
    assert "✓1" in output or "1 passed" in output
