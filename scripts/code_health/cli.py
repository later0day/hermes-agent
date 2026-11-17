"""``python -m scripts.code_health`` / ``scripts/check``: the code-health ratchet."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from scripts.code_health import gitio
from scripts.code_health.compare import compare
from scripts.code_health.config import in_scope
from scripts.code_health.measure import Measurer
from scripts.code_health.report import (
    apply_allows,
    format_findings,
    summarize_tree,
    verdict,
)
from scripts.code_health.ruff_runner import pinned_version, resolve_ruff

_FOOTER = """
Each function and file has its own cap: its value on main if it is already over target,
otherwise the target. New code meets the target; existing code may only go down. Fix the code
(the `fix:` line is the repo's remedy); a genuine exception takes a reviewed comment on the line,
`# health: allow <RULE> -- <why>`. Rules and targets: scripts/code_health/config.py.
Reproduce locally: python scripts/check --only health (every lint check: python scripts/check;
on every push: python scripts/check --install-hook pre-push)."""

_SWITCH_FILE = "scripts/code_health/config.py"
_MODES = ("blocking", "advisory", "off")


def parse_switch(text: str) -> str:
    """The module-level ``ENFORCEMENT`` value (plain or annotated assignment; the last one wins,
    as at runtime). Missing, unparseable or unknown is "blocking", so no edit relaxes it by
    accident."""
    try:
        body = ast.parse(text).body
    except SyntaxError:
        return "blocking"
    found = None
    for node in body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "ENFORCEMENT" for t in targets):
            found = node.value.value if isinstance(node.value, ast.Constant) else None
    return found if found in _MODES else "blocking"


def enforcement(repo: Path, rev: str) -> str:
    """The master switch as committed on ``rev``; a revision without one is blocking."""
    return parse_switch(gitio.read_file(repo, rev, _SWITCH_FILE) or "")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="code_health", description=__doc__)
    p.add_argument("--base", help="base revision (default: merge-base of HEAD with origin/main)")
    p.add_argument("--head", help="head commit or tree (default: the working tree, untracked included)")
    p.add_argument("--report", action="store_true", help="burn-down summary of the whole head tree")
    p.add_argument("--json", action="store_true", help="machine-readable findings")
    p.add_argument("--print-pins", action="store_true",
                   help="print the pinned ruff requirement (CI installs exactly this)")
    return p


def _report(repo: Path, head: str | None) -> int:
    paths = [p for p in gitio.tracked_files(repo, head) if in_scope(p)]
    measurer = Measurer(repo, resolve_ruff(repo), known_env=gitio.known_env_names(repo, head))
    print(summarize_tree(measurer.measure(head, paths)))
    return 0


def _skipped(why: str, as_json: bool) -> None:
    """A run that measured nothing: an empty findings list for --json, the reason for humans."""
    if as_json:
        print("[]")
    print(why, file=sys.stderr if as_json else sys.stdout)


def _merge_changes(repo: Path, bases: list[str], head: str | None):
    """Files byte-identical on either parent are inherited, not code written by the merge."""
    changes = {base: gitio.changed_files(repo, base, head) for base in bases}
    if len(bases) == 1:
        return changes
    inherited = set()
    for base, base_changes in changes.items():
        changed = {c.new for c in base_changes if c.new}
        inherited |= changed - {c.new for c in changes[bases[0]] if c.new}
        inherited |= {c.new for c in changes[bases[0]] if c.new} - changed
    return {base: [c for c in base_changes if c.new not in inherited]
            for base, base_changes in changes.items()}


def _finding_key(finding, head_m):
    fm = head_m.get(finding.path)
    text = fm.code_line(finding.line) if fm else ""
    return finding.path, finding.rule, finding.scope, text


def _common_findings(groups: list[list], head_m: dict) -> list:
    """A merge finding must be new against every parent; one parent's debt cannot pay twice."""
    common = Counter(_finding_key(f, head_m) for f in groups[0])
    for findings in groups[1:]:
        common &= Counter(_finding_key(f, head_m) for f in findings)
    remaining = common.copy()
    result = []
    for finding in groups[0]:
        key = _finding_key(finding, head_m)
        if remaining[key]:
            result.append(finding)
            remaining[key] -= 1
    return result


def run(repo: Path, base: str | list[str], head: str | None, as_json: bool = False,
        switch_rev: str | None = None) -> int:
    """Judge ``head`` against one base or both parents of a merge, never its own policy."""
    started = time.monotonic()
    bases = [base] if isinstance(base, str) else base
    switch_rev = switch_rev or bases[0]
    mode = enforcement(repo, switch_rev)
    if mode == "off":
        _skipped(f"code health: off (ENFORCEMENT in {_SWITCH_FILE} on {switch_rev[:12]})", as_json)
        return 0
    changes = _merge_changes(repo, bases, head)
    head_paths = sorted({c.new for base_changes in changes.values()
                         for c in base_changes if c.new and in_scope(c.new)})
    base_paths = {base: sorted({c.old for c in base_changes if c.old and in_scope(c.old)})
                  for base, base_changes in changes.items()}
    if not head_paths:
        _skipped("code health: no measured files changed", as_json)
        return 0
    # Base files are measured too: a deleted .py still needs ruff on the base side.
    needs_ruff = any(p.endswith(".py") for p in (*head_paths, *(p for paths in base_paths.values() for p in paths)))
    ruff = resolve_ruff(repo) if needs_ruff else []
    known_env = set().union(*(gitio.known_env_names(repo, base) for base in bases))
    head_m = Measurer(repo, ruff, known_env=known_env).measure(head, head_paths)
    moves = gitio.tree_moves(repo, head)
    groups = []
    for base in bases:
        base_m = Measurer(repo, ruff, known_env=gitio.known_env_names(repo, base)).measure(base, base_paths[base])
        groups.append(compare(base_m, head_m, changes[base], moves))
    findings = _common_findings(groups, head_m)
    apply_allows(findings, head_m)
    blocking, advisory = verdict(findings)
    failed = bool(blocking) and mode == "blocking"
    if as_json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
        return 1 if failed else 0
    body = format_findings(findings)
    if body:
        print(body)
    elapsed = time.monotonic() - started
    label = " + ".join(base[:12] for base in bases)
    print(f"\ncode health: {len(head_paths)} files vs {label}: {blocking} blocking, "
          f"{advisory} advisory ({elapsed:.1f}s)")
    if blocking:
        print(_FOOTER)
    if blocking and not failed:
        print(f"\nadvisory mode (ENFORCEMENT in {_SWITCH_FILE}): not failing on the above.")
    return 1 if failed else 0


def _local_bases(repo: Path, base: str, tip: str | None) -> list[str]:
    """A merge onto the local base is judged against both parents; ordinary branches keep their
    merge-base with main, and an explicit --base never reaches this function."""
    if tip:
        parents = gitio.git(repo, "rev-list", "--parents", "-n", "1", tip).split()[1:]
        if len(parents) > 1 and parents[0] == base:
            return parents
    else:
        incoming = gitio.commit_or_none(repo, "MERGE_HEAD")
        first = gitio.commit_or_none(repo, "HEAD")
        if incoming and first:
            return [first, incoming]
    return [base]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = gitio.repo_root(Path.cwd())
    if args.print_pins:
        print(f"ruff=={pinned_version(repo)}")
        return 0
    try:
        head = gitio.resolve_tree(repo, args.head) if args.head else None
        if args.report:
            return _report(repo, head)
        if args.base:  # CI: the base is the target branch tip, which also holds the switch
            return run(repo, gitio.resolve_rev(repo, args.base), head, as_json=args.json)
        # Locally the base is the merge-base, which lags main; the switch comes from the main tip
        # so a flip on main reaches a branch without a rebase. The head never supplies it.
        tip = gitio.commit_or_none(repo, args.head) if args.head else None
        base = gitio.default_base(repo, tip or "HEAD")
        switch_rev = gitio.commit_or_none(repo, "origin/main") or base
        bases = _local_bases(repo, base, tip)
        return run(repo, bases, head, as_json=args.json, switch_rev=switch_rev)
    except RuntimeError as exc:
        print(f"code health: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # A measuring tool (npm install of the pinned TypeScript, ...) failed: that is a broken
        # run, not a finding, so it must not exit 1 behind a traceback.
        print(f"code health: measurement tooling failed: {exc}", file=sys.stderr)
        return 2
