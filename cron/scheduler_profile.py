"""Per-job Hermes profile scope and its sequential executor.

The ticker late-binds ``cron.scheduler`` so this module owns profile selection without making the
execution monolith import profile machinery. Profile jobs share one worker because legacy provider
and dotenv paths still publish values through the process environment; task-scoped workdirs do not.
"""

import concurrent.futures
import contextlib
import os
from pathlib import Path
from typing import Optional


_sequential_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _job_run_profile(job: dict) -> Optional[str]:
    """Return the profile used to execute this job."""
    for key in ("run_profile", "profile", "owner_profile"):
        text = str(job.get(key) or "").strip()
        if text:
            return text
    return None


def _get_sequential_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Return the persistent worker for profile jobs."""
    global _sequential_pool
    if _sequential_pool is None:
        _sequential_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cron-seq")
    return _sequential_pool


@contextlib.contextmanager
def _job_profile_context(job_id: str, profile: Optional[str]):
    """Resolve a job's profile home context-locally and restore dotenv changes afterward."""
    raw_profile = str(profile or "").strip()
    if not raw_profile:
        yield None
        return

    from agent.runtime_scope import bind_profile_scope
    from cron import scheduler as _sched
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    prior_override = _sched._hermes_home
    env_snapshot = os.environ.copy()
    normalized_profile = normalize_profile_name(raw_profile)
    try:
        profile_home = Path(resolve_profile_env(normalized_profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        _sched.logger.warning(
            "Job '%s': configured profile %r no longer valid (%s) — "
            "falling back to scheduler default", job_id, raw_profile, exc)
        yield None
        return

    try:
        # Bind the whole profile, not just its home: under multiplexing the dotenv loader deliberately
        # skips process-global writes, so file-backed credentials must come from this scoped mapping.
        with bind_profile_scope(profile_home):
            _sched._hermes_home = profile_home
            _sched.logger.info(
                "Job '%s': using Hermes profile '%s' (%s)",
                job_id, normalized_profile, profile_home)
            yield normalized_profile
    finally:
        _sched._hermes_home = prior_override
        added = set(os.environ) - set(env_snapshot)
        for key in added:
            os.environ.pop(key, None)
        for key, value in env_snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


def run_job(
    job: dict, *, defer_agent_teardown: Optional[list] = None,
    extra_prompt: Optional[str] = None, cancel_event=None, execution_id: Optional[str] = None,
) -> tuple[bool, str, str, Optional[str]]:
    """Run the complete job, including canonical admission, under its selected profile."""
    from cron import scheduler as _sched

    with _job_profile_context(job["id"], _job_run_profile(job)):
        return _sched._run_job_impl(
            job, defer_agent_teardown=defer_agent_teardown, extra_prompt=extra_prompt,
            cancel_event=cancel_event, execution_id=execution_id)
