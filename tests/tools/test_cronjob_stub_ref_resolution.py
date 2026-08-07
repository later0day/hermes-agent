"""Cronjob tool stubs must resolve job references by ID *or* name.

The fork introduced ``_context_ref_visible`` / ``_resolve_job_ref_for_scope``
as scope-routing extension points but wired them to ``cron.jobs.get_job``,
which only matches by ID.  Every call site catches
``AmbiguousJobReference`` — that catch is dead code unless the stub goes
through ``resolve_job_ref`` (ID-first with a fallback to a case-insensitive
name match), which is what upstream's inline ``_get_job = get_job as _get_job``
does not use either — upstream users still refer to jobs by name via the
LLM tool surface, and the fork silently degraded that path to
"not found" for any name-only reference.
"""

from __future__ import annotations
from unittest.mock import patch

import pytest

from tools import cronjob_tools


class _MockJobsStore:
    def __init__(self, jobs):
        self._jobs = list(jobs)

    def resolve_job_ref(self, ref):
        # Mimic cron.jobs.resolve_job_ref: ID-exact first, then case-insensitive
        # name; raise AmbiguousJobReference if the name has multiple matches.
        for j in self._jobs:
            if j["id"] == ref:
                return j
        matches = [j for j in self._jobs if (j.get("name") or "").lower() == ref.lower()]
        if not matches:
            return None
        if len(matches) > 1:
            from cron.jobs import AmbiguousJobReference
            raise AmbiguousJobReference(ref, matches)
        return matches[0]


def test_context_ref_visible_matches_by_name(monkeypatch):
    """A NAME reference must be found — upstream parity."""
    store = _MockJobsStore([
        {"id": "cj_abc", "name": "daily-report"},
    ])
    with patch("tools.cronjob_tools.resolve_job_ref", side_effect=store.resolve_job_ref):
        assert cronjob_tools._context_ref_visible("daily-report", None) is True
        assert cronjob_tools._context_ref_visible("cj_abc", None) is True
        assert cronjob_tools._context_ref_visible("missing", None) is False


def test_context_ref_visible_raises_on_ambiguous_name():
    """A name matching multiple jobs must raise so the caller can say so."""
    from cron.jobs import AmbiguousJobReference

    store = _MockJobsStore([
        {"id": "cj_a", "name": "report"},
        {"id": "cj_b", "name": "report"},
    ])
    with patch("tools.cronjob_tools.resolve_job_ref", side_effect=store.resolve_job_ref):
        with pytest.raises(AmbiguousJobReference):
            cronjob_tools._context_ref_visible("report", None)


def test_resolve_job_ref_for_scope_matches_by_name():
    """The primary manage-by-name path must find a NAME reference."""
    store = _MockJobsStore([
        {"id": "cj_abc", "name": "hourly-sync"},
    ])
    with patch("tools.cronjob_tools.resolve_job_ref", side_effect=store.resolve_job_ref):
        j = cronjob_tools._resolve_job_ref_for_scope("hourly-sync", None)
        assert j is not None and j["id"] == "cj_abc"


def test_resolve_job_ref_for_scope_raises_on_ambiguous_name():
    from cron.jobs import AmbiguousJobReference

    store = _MockJobsStore([
        {"id": "cj_x", "name": "sync"},
        {"id": "cj_y", "name": "sync"},
    ])
    with patch("tools.cronjob_tools.resolve_job_ref", side_effect=store.resolve_job_ref):
        with pytest.raises(AmbiguousJobReference):
            cronjob_tools._resolve_job_ref_for_scope("sync", None)


def test_current_origin_scope_stub_returns_none():
    """Scope routing is intentionally not implemented on this host — pin the
    stub's contract so a future refactor that changes it (and forgets to
    update the isolation caveat) is caught in review."""
    assert cronjob_tools._current_origin_scope() is None


def test_list_jobs_for_scope_delegates_to_upstream_list():
    """The default (scope=None) list must be equivalent to cron.jobs.list_jobs."""
    sentinel_all = [{"id": "cj_1"}, {"id": "cj_2"}]
    sentinel_active = [{"id": "cj_1"}]

    def _list(include_disabled=False):
        return sentinel_all if include_disabled else sentinel_active

    with patch("tools.cronjob_tools.list_jobs", side_effect=_list):
        assert cronjob_tools._list_jobs_for_scope() == sentinel_active
        assert cronjob_tools._list_jobs_for_scope(include_disabled=True) == sentinel_all
