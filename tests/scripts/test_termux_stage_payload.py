"""Behavior contracts for the Termux payload orchestration shell."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def staged_script(tmp_path: Path) -> tuple[Path, Path]:
    script_dir = tmp_path / "termux"
    script_dir.mkdir()
    script = script_dir / "stage_payload.sh"
    shutil.copy2(REPO_ROOT / "scripts/termux/stage_payload.sh", script)
    call_log = tmp_path / "calls.log"

    (script_dir / "termux_pkg_build.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'build' >> "$CALL_LOG"
printf '\\t%s' "$@" >> "$CALL_LOG"
printf '\\n' >> "$CALL_LOG"
if [ "$1" = "${FAIL_PACKAGE:-}" ]; then
  exit 42
fi
mkdir -p "$3/$1"
""",
        encoding="utf-8",
    )
    (script_dir / "stage_runtime_libs.py").write_text(
        """import os
from pathlib import Path
import sys

with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(f"runtime\\t{sys.argv[1]}\\n")
Path(sys.argv[1], "runtime-libs").mkdir(parents=True, exist_ok=True)
""",
        encoding="utf-8",
    )
    return script, call_log


def _run(
    script: Path, call_log: Path, payload: Path, *packages: str, fail_package: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), str(payload), *packages],
        cwd=script.parent,
        env={**os.environ, "CALL_LOG": str(call_log), "FAIL_PACKAGE": fail_package},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_packages_stage_in_argument_order_before_runtime_libraries(
    staged_script: tuple[Path, Path], tmp_path: Path
) -> None:
    script, call_log = staged_script
    payload = tmp_path / "payload with spaces"

    result = _run(script, call_log, payload, "python", "node")

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"build\tpython\tpython\t{payload}",
        f"build\tnode\tnode\t{payload}",
        f"runtime\t{payload}",
    ]
    assert (payload / "python").is_dir()
    assert (payload / "node").is_dir()
    assert (payload / "runtime-libs").is_dir()


def test_empty_package_list_still_stages_runtime_libraries(
    staged_script: tuple[Path, Path], tmp_path: Path
) -> None:
    script, call_log = staged_script
    payload = tmp_path / "payload"

    result = _run(script, call_log, payload)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [f"runtime\t{payload}"]
    assert (payload / "runtime-libs").is_dir()


def test_package_failure_stops_later_packages_and_runtime_staging(
    staged_script: tuple[Path, Path], tmp_path: Path
) -> None:
    script, call_log = staged_script
    payload = tmp_path / "payload"

    result = _run(
        script, call_log, payload, "python", "broken", "node", fail_package="broken"
    )

    assert result.returncode == 42
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"build\tpython\tpython\t{payload}",
        f"build\tbroken\tbroken\t{payload}",
    ]
    assert (payload / "python").is_dir()
    assert not (payload / "node").exists()
    assert not (payload / "runtime-libs").exists()
