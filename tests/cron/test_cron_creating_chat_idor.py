"""Cron creating-chat IDOR isolation (fork port-plan §4.1).

A single multiplex profile can bind many distinct chats/DMs to one cron
store (source->profile binding). Without a gate, cronjob(action="list")
returns the whole store and resolve_job_ref-based mutations
(remove/pause/resume/run/update) operate on any job by id/name — so any
member of any bound chat could enumerate and mutate EVERY other chat's
jobs in the same profile.

The gate (_caller_may_touch_job) scopes list + mutation to the creating
chat for multi-tenant chat platforms (dingtalk/weixin/slack/...), while
trusted management surfaces — CLI/TUI (no session origin) and api_server
(its own API key gates access) — retain full-store access. Cross-origin
mutations report "not found" (not "forbidden") so the gate does not
confirm the existence of jobs the caller cannot see.
"""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars


@pytest.fixture
def temp_cron_home(tmp_path):
    from cron import jobs as cron_jobs

    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.ensure_dirs()
        yield tmp_path


def _seed_job(job_id, name, *, platform, chat_id):
    """Persist a job carrying an origin, bypassing the tool layer."""
    from cron import jobs as cron_jobs

    jobs = cron_jobs.load_jobs()
    jobs.append(
        {
            "id": job_id,
            "name": name,
            "schedule": "every 1h",
            "prompt": "noop",
            "enabled": True,
            "deliver": "origin",
            "origin": {"platform": platform, "chat_id": chat_id},
        }
    )
    cron_jobs.save_jobs(jobs)


def _as_chat(platform, chat_id):
    return set_session_vars(
        platform=platform,
        chat_id=chat_id,
        chat_name="",
        cron_session="",
    )


def _list_names(include_disabled=False):
    from tools.cronjob_tools import cronjob

    res = json.loads(cronjob(action="list", include_disabled=include_disabled))
    assert res["success"] is True
    return {j["name"] for j in res["jobs"]}


GROUP_A = "cidAAAAAAAAAAAAAAAAAAAAA=="
GROUP_B = "cidBBBBBBBBBBBBBBBBBBBBB=="


@pytest.fixture
def two_group_store(temp_cron_home):
    _seed_job("aaaa1111", "job-A", platform="dingtalk", chat_id=GROUP_A)
    _seed_job("bbbb2222", "job-B", platform="dingtalk", chat_id=GROUP_B)
    return temp_cron_home


def test_group_lists_only_its_own_jobs(two_group_store):
    tok = _as_chat("dingtalk", GROUP_A)
    try:
        names = _list_names()
    finally:
        clear_session_vars(tok)
    assert names == {"job-A"}


def test_cli_sees_full_store(two_group_store):
    # No session origin (CLI/TUI) -> full-store visibility.
    assert _list_names() == {"job-A", "job-B"}


def test_api_server_sees_full_store(two_group_store):
    tok = _as_chat("api_server", "api")
    try:
        names = _list_names()
    finally:
        clear_session_vars(tok)
    assert names == {"job-A", "job-B"}


def test_cross_group_remove_is_blocked_and_reports_not_found(two_group_store):
    from tools.cronjob_tools import cronjob
    from cron import jobs as cron_jobs

    tok = _as_chat("dingtalk", GROUP_A)
    try:
        res = json.loads(cronjob(action="remove", job_id="bbbb2222"))
    finally:
        clear_session_vars(tok)
    assert res["success"] is False
    assert "not found" in res["error"].lower()
    # The victim job must still exist (was not deleted cross-origin).
    assert any(j["id"] == "bbbb2222" for j in cron_jobs.load_jobs())


def test_cross_group_pause_is_blocked(two_group_store):
    from tools.cronjob_tools import cronjob

    tok = _as_chat("dingtalk", GROUP_A)
    try:
        res = json.loads(cronjob(action="pause", job_id="bbbb2222"))
    finally:
        clear_session_vars(tok)
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_own_group_remove_succeeds(two_group_store):
    from tools.cronjob_tools import cronjob
    from cron import jobs as cron_jobs

    tok = _as_chat("dingtalk", GROUP_A)
    try:
        res = json.loads(cronjob(action="remove", job_id="aaaa1111"))
    finally:
        clear_session_vars(tok)
    assert res["success"] is True
    assert not any(j["id"] == "aaaa1111" for j in cron_jobs.load_jobs())


def test_cli_can_remove_any_job(two_group_store):
    from tools.cronjob_tools import cronjob
    from cron import jobs as cron_jobs

    # No session origin -> trusted, can remove group B's job.
    res = json.loads(cronjob(action="remove", job_id="bbbb2222"))
    assert res["success"] is True
    assert not any(j["id"] == "bbbb2222" for j in cron_jobs.load_jobs())
