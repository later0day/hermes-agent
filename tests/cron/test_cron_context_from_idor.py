"""``context_from`` obeys the creating-chat gate.

context_from injects a referenced job's latest output into the new job's prompt, so creating a job
that references another group's job id read that group's reports — the one cron path the IDOR gate
(``_caller_may_touch_job``) did not cover. A foreign ref now reads as "not found", like every other
cross-origin job operation; a chat's own jobs and trusted surfaces (CLI) are unaffected.
"""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars

GROUP_A = "cidAAAAAAAAAAAAAAAAAAAAA=="
GROUP_B = "cidBBBBBBBBBBBBBBBBBBBBB=="


@pytest.fixture
def store(tmp_path):
    from cron import jobs as cron_jobs

    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.ensure_dirs()
        jobs = cron_jobs.load_jobs()
        for job_id, chat in (("aaaa1111", GROUP_A), ("bbbb2222", GROUP_B)):
            jobs.append({"id": job_id, "name": f"job-{job_id}", "schedule": "every 1h", "prompt": "noop",
                         "enabled": True, "deliver": "origin", "origin": {"platform": "dingtalk", "chat_id": chat}})
        cron_jobs.save_jobs(jobs)
        yield


def _create_from(chat_id, ref):
    from tools.cronjob_tools import cronjob

    tokens = set_session_vars(platform="dingtalk", chat_id=chat_id, chat_name="", cron_session="") if chat_id else None
    try:
        return json.loads(cronjob(action="create", schedule="every 2h", prompt="summarise", context_from=[ref]))
    finally:
        if tokens is not None:
            clear_session_vars(tokens)


def test_a_chat_cannot_pull_another_groups_job_output(store):
    res = _create_from(GROUP_A, "bbbb2222")
    assert res["success"] is False and "not found" in res["error"]


def test_a_chat_may_chain_its_own_jobs(store):
    assert _create_from(GROUP_A, "aaaa1111")["success"] is True


def test_cli_may_reference_any_job(store):
    assert _create_from(None, "bbbb2222")["success"] is True
