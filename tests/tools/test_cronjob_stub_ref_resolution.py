"""Public cronjob actions resolve job names and store canonical context references."""

from __future__ import annotations

import json

import pytest

from cron import jobs as cron_jobs
from tools.cronjob_tools import cronjob


@pytest.fixture
def store(tmp_path):
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.ensure_dirs()
        yield


def _create(*, name: str, prompt: str = "Collect a report", paused: bool = False) -> dict:
    result = json.loads(cronjob(
        action="create", name=name, prompt=prompt, schedule="every 1h", paused=paused,
    ))
    assert result["success"] is True, result
    return cron_jobs.get_job(result["job_id"])


def test_update_manages_a_job_by_name(store):
    job = _create(name="hourly-sync", prompt="old prompt")

    result = json.loads(cronjob(
        action="update", job_id="HOURLY-SYNC", prompt="new prompt",
    ))

    assert result["success"] is True, result
    assert result["job"]["job_id"] == job["id"]
    assert cron_jobs.get_job(job["id"])["prompt"] == "new prompt"


def test_update_reports_an_ambiguous_job_name(store):
    first = _create(name="sync", prompt="first")
    second = _create(name="sync", prompt="second")

    result = json.loads(cronjob(action="update", job_id="sync", prompt="replacement"))

    assert result["success"] is False
    assert "ambiguous" in result["error"]
    assert {match["id"] for match in result["matches"]} == {first["id"], second["id"]}
    assert cron_jobs.get_job(first["id"])["prompt"] == "first"
    assert cron_jobs.get_job(second["id"])["prompt"] == "second"


def test_list_filters_disabled_jobs_unless_requested(store):
    active = _create(name="active")
    paused = _create(name="paused", paused=True)

    visible = json.loads(cronjob(action="list"))
    everything = json.loads(cronjob(action="list", include_disabled=True))

    assert visible["success"] is True
    assert {job["job_id"] for job in visible["jobs"]} == {active["id"]}
    assert everything["success"] is True
    assert {job["job_id"] for job in everything["jobs"]} == {active["id"], paused["id"]}


@pytest.mark.parametrize("reference_kind", ["name", "id"])
def test_create_context_from_stores_the_canonical_job_id(store, reference_kind):
    upstream = _create(name="daily-report")
    reference = upstream["name"] if reference_kind == "name" else upstream["id"]

    result = json.loads(cronjob(
        action="create", name="summary", prompt="Summarize the report",
        schedule="every 2h", context_from=reference,
    ))

    assert result["success"] is True, result
    assert cron_jobs.get_job(result["job_id"])["context_from"] == [upstream["id"]]


def test_update_context_from_name_stores_the_canonical_job_id(store):
    upstream = _create(name="daily-report")
    downstream = _create(name="summary", prompt="Summarize the report")

    result = json.loads(cronjob(
        action="update", job_id=downstream["id"], context_from="DAILY-REPORT",
    ))

    assert result["success"] is True, result
    assert result["job"]["context_from"] == [upstream["id"]]
    assert cron_jobs.get_job(downstream["id"])["context_from"] == [upstream["id"]]


def test_create_context_from_reports_an_ambiguous_name_without_saving(store):
    first = _create(name="report")
    second = _create(name="report")
    jobs_before = {job["id"] for job in cron_jobs.load_jobs()}

    result = json.loads(cronjob(
        action="create", name="summary", prompt="Summarize the report",
        schedule="every 2h", context_from="report",
    ))

    assert result["success"] is False
    assert "ambiguous" in result["error"]
    assert first["id"] in result["error"]
    assert second["id"] in result["error"]
    assert {job["id"] for job in cron_jobs.load_jobs()} == jobs_before
