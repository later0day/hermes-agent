"""Tests for per-job profile support in cron jobs.

Covers data-layer validation/storage, cronjob tool plumbing, scheduler runtime
HERMES_HOME scoping, and tick() serialization for profile jobs.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def isolated_cron_profile_home(tmp_path, monkeypatch):
    """Create an isolated Hermes root with a named profile and temp cron store."""
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "support"
    profile_home.mkdir(parents=True)
    (root / "cron").mkdir(parents=True)
    # A profile job re-loads the profile's .env at run time; create it so
    # load_hermes_dotenv actually reaches the (monkeypatched) dotenv loader.
    (profile_home / ".env").write_text("HERMES_PROFILE_MARKER=1\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("cron.jobs.CRON_DIR", root / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", root / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", root / "cron" / "output")

    return root, profile_home


class TestNormalizeProfile:
    def test_none_and_empty_return_none(self, isolated_cron_profile_home):
        from cron.jobs_normalize import _normalize_profile

        assert _normalize_profile(None) is None
        assert _normalize_profile("") is None
        assert _normalize_profile("   ") is None

    def test_default_profile_is_valid_and_normalized(self, isolated_cron_profile_home):
        from cron.jobs_normalize import _normalize_profile

        assert _normalize_profile("Default") == "default"

    def test_named_profile_must_exist_and_is_normalized(self, isolated_cron_profile_home):
        from cron.jobs_normalize import _normalize_profile

        assert _normalize_profile("Support") == "support"

    def test_invalid_profile_name_is_rejected(self, isolated_cron_profile_home):
        from cron.jobs_normalize import _normalize_profile

        with pytest.raises(ValueError):
            _normalize_profile("invalid!")

    def test_missing_named_profile_is_rejected(self, isolated_cron_profile_home):
        from cron.jobs_normalize import _normalize_profile

        with pytest.raises(FileNotFoundError):
            _normalize_profile("missing")


class TestCreateAndUpdateJobProfile:
    def test_create_stores_profile_id(self, isolated_cron_profile_home):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="hello", schedule="every 1h", profile="Support")
        stored = get_job(job["id"])

        assert stored is not None
        assert stored["profile"] == "support"
        assert stored["run_profile"] == "support"
        assert stored["owner_profile"] == "support"

    def test_create_without_profile_preserves_old_behaviour(self, isolated_cron_profile_home):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="hello", schedule="every 1h")
        stored = get_job(job["id"])

        assert stored is not None
        assert stored.get("profile") is None
        assert stored.get("run_profile") is None
        assert stored["owner_profile"] == "default"

    def test_create_accepts_explicit_default(self, isolated_cron_profile_home):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="hello", schedule="every 1h", profile="default")
        stored = get_job(job["id"])

        assert stored is not None
        assert stored["profile"] == "default"
        assert stored["run_profile"] == "default"
        assert stored["owner_profile"] == "default"

    def test_update_sets_and_clears_profile(self, isolated_cron_profile_home):
        from cron.jobs import create_job, get_job, update_job

        job = create_job(prompt="x", schedule="every 1h")
        update_job(job["id"], {"profile": "Support"})
        stored = get_job(job["id"])
        assert stored is not None
        assert stored["profile"] == "support"
        assert stored["run_profile"] == "support"

        update_job(job["id"], {"profile": ""})
        stored = get_job(job["id"])
        assert stored is not None
        assert stored["profile"] is None
        assert stored["run_profile"] is None

    def test_update_rejects_missing_profile(self, isolated_cron_profile_home):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="x", schedule="every 1h")
        with pytest.raises(FileNotFoundError):
            update_job(job["id"], {"profile": "missing"})


class TestCronjobToolProfile:
    def test_create_and_list_with_profile(self, isolated_cron_profile_home):
        from tools.cronjob_tools import cronjob

        created = json.loads(
            cronjob(
                action="create",
                prompt="hi",
                schedule="every 1h",
                profile="Support",
            )
        )
        assert created["success"] is True
        assert created["job"]["profile"] == "support"
        assert created["job"]["run_profile"] == "support"
        assert created["job"]["owner_profile"] == "support"

        listing = json.loads(cronjob(action="list"))
        assert listing["jobs"][0]["profile"] == "support"

    def test_update_clears_profile_with_empty_string(self, isolated_cron_profile_home):
        from tools.cronjob_tools import cronjob

        created = json.loads(
            cronjob(
                action="create",
                prompt="hi",
                schedule="every 1h",
                profile="Support",
            )
        )
        updated = json.loads(
            cronjob(action="update", job_id=created["job_id"], profile="")
        )

        assert updated["success"] is True
        assert "profile" not in updated["job"]
        assert "run_profile" not in updated["job"]

    def test_schema_advertises_profile(self):
        from tools.cronjob_tools import CRONJOB_SCHEMA

        assert "profile" in CRONJOB_SCHEMA["parameters"]["properties"]
        desc = CRONJOB_SCHEMA["parameters"]["properties"]["profile"]["description"]
        desc_lower = desc.lower()
        assert "hermes profile" in desc_lower
        assert "context-local" in desc_lower
        assert "subprocess" in desc_lower
        assert "temporarily sets hermes_home" not in desc_lower


class TestRunJobProfileContext:
    @staticmethod
    def _install_agent_stubs(monkeypatch, observed: dict):
        import sys
        import cron.scheduler as sched

        class FakeAgent:
            def __init__(self, **kwargs):
                from hermes_constants import get_hermes_home

                observed["env_home_during_init"] = os.environ.get("HERMES_HOME")
                observed["profile_env_only_during_init"] = os.environ.get(
                    "HERMES_PROFILE_TEST_ONLY"
                )
                observed["profile_env_shared_during_init"] = os.environ.get(
                    "HERMES_PROFILE_TEST_SHARED"
                )
                observed["hermes_home_during_init"] = str(get_hermes_home())
                observed["scheduler_home_during_init"] = str(sched._get_hermes_home())
                observed["skip_context_files"] = kwargs.get("skip_context_files")

            def run_conversation(self, *_a, **_kw):
                from hermes_constants import get_hermes_home

                observed["env_home_during_run"] = os.environ.get("HERMES_HOME")
                observed["profile_env_only_during_run"] = os.environ.get(
                    "HERMES_PROFILE_TEST_ONLY"
                )
                observed["profile_env_shared_during_run"] = os.environ.get(
                    "HERMES_PROFILE_TEST_SHARED"
                )
                observed["hermes_home_during_run"] = str(get_hermes_home())
                observed["scheduler_home_during_run"] = str(sched._get_hermes_home())
                return {"final_response": "done", "messages": []}

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

            def close(self):
                observed["closed"] = True

        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = FakeAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            lambda **_kw: {
                "provider": "test",
                "api_key": "test-key",
                "base_url": "http://test.local",
                "api_mode": "chat_completions",
            },
        )

        from cron import scheduler_delivery
        from cron import scheduler_authority
        from tests.cron.conftest import owner_execution_runner

        monkeypatch.setattr(
            scheduler_authority, "run_canonical_job", owner_execution_runner())
        monkeypatch.setattr(sched, "_build_job_prompt", lambda job, prerun_script=None, **_: "hi")
        monkeypatch.setattr(scheduler_delivery, "_resolve_origin", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
        monkeypatch.setattr(sched, "_hermes_home", None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        from hermes_cli import env_loader

        def fake_load_hermes_dotenv(*, hermes_home=None, **_kw):
            from pathlib import Path

            observed.setdefault("dotenv_paths", []).append(
                str(Path(hermes_home) / ".env"))
            return [Path(hermes_home) / ".env"]

        # The scheduler late-imports this public loader for each run, so patch
        # its defining module rather than the parser's former private alias.
        monkeypatch.setattr(env_loader, "load_hermes_dotenv", fake_load_hermes_dotenv)

    def test_run_job_sets_and_restores_profile_home(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "abc",
            "name": "profile-job",
            "profile": "support",
            "schedule_display": "manual",
        }

        success, _output, response, error = sched.run_job(job)

        assert success is True, f"run_job failed: error={error!r} response={response!r}"
        # Canonical admission may load the launch home once before the owning
        # execution starts; the job's own reload must be its selected profile.
        assert observed["dotenv_paths"][-1] == str(profile_home / ".env")
        assert observed["env_home_during_init"] == str(root)
        assert observed["env_home_during_run"] == str(root)
        assert observed["hermes_home_during_init"] == str(profile_home.resolve())
        assert observed["hermes_home_during_run"] == str(profile_home.resolve())
        assert observed["scheduler_home_during_init"] == str(profile_home.resolve())
        assert observed["scheduler_home_during_run"] == str(profile_home.resolve())
        assert observed["skip_context_files"] is True
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_run_job_uses_run_profile_when_legacy_profile_missing(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "run-profile",
            "name": "run-profile-job",
            "run_profile": "support",
            "profile": None,
            "schedule_display": "manual",
        }

        success, _output, _response, error = sched.run_job(job)

        assert success is True, error
        assert observed["dotenv_paths"] == [str(profile_home / ".env")]
        assert observed["hermes_home_during_init"] == str(profile_home.resolve())
        assert os.environ["HERMES_HOME"] == str(root)

    def test_profile_dotenv_environment_is_restored(
        self, isolated_cron_profile_home, monkeypatch
    ):
        from hermes_cli import env_loader
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)
        monkeypatch.setenv("HERMES_PROFILE_TEST_SHARED", "outer")
        monkeypatch.delenv("HERMES_PROFILE_TEST_ONLY", raising=False)

        def fake_load_hermes_dotenv(*, hermes_home=None, **_kw):
            from pathlib import Path

            env_path = Path(hermes_home) / ".env"
            observed.setdefault("dotenv_paths", []).append(str(env_path))
            os.environ["HERMES_PROFILE_TEST_SHARED"] = "profile-value"
            os.environ["HERMES_PROFILE_TEST_ONLY"] = "profile-only"
            os.environ["HERMES_CRON_TIMEOUT"] = "123"
            return [env_path]

        # Patch the public loader the scheduler imports at run time.
        monkeypatch.setattr(env_loader, "load_hermes_dotenv", fake_load_hermes_dotenv)

        job = {
            "id": "env-profile",
            "name": "profile-env-job",
            "profile": "support",
            "schedule_display": "manual",
        }

        success, _output, _response, error = sched.run_job(job)

        assert success is True, error
        assert observed["dotenv_paths"] == [str(profile_home / ".env")]
        assert observed["profile_env_only_during_init"] == "profile-only"
        assert observed["profile_env_shared_during_init"] == "profile-value"
        assert observed["profile_env_only_during_run"] == "profile-only"
        assert observed["profile_env_shared_during_run"] == "profile-value"
        assert os.environ["HERMES_PROFILE_TEST_SHARED"] == "outer"
        assert "HERMES_PROFILE_TEST_ONLY" not in os.environ
        assert os.environ["HERMES_CRON_TIMEOUT"] == "0"
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_multiplex_profile_dotenv_stays_in_secret_scope(
        self, isolated_cron_profile_home, monkeypatch
    ):
        from agent.secret_scope import (
            get_secret, reset_multiplex_context, set_multiplex_context,
        )
        from cron.scheduler_profile import _job_profile_context
        from hermes_cli.env_loader import load_hermes_dotenv

        _root, profile_home = isolated_cron_profile_home
        (profile_home / ".env").write_text(
            "OPENAI_API_KEY=profile-secret\n", encoding="utf-8")
        monkeypatch.setenv("OPENAI_API_KEY", "launch-secret")
        multiplex_token = set_multiplex_context(True)
        try:
            with _job_profile_context("multiplex-profile", "support"):
                load_hermes_dotenv(hermes_home=profile_home)
                assert get_secret("OPENAI_API_KEY") == "profile-secret"
                assert os.environ["OPENAI_API_KEY"] == "launch-secret"
        finally:
            reset_multiplex_context(multiplex_token)
        assert os.environ["OPENAI_API_KEY"] == "launch-secret"

    def test_no_agent_profile_uses_profile_scripts_dir_and_restores_env(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        scripts_dir = profile_home / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "print_home.py").write_text(
            "import os\nprint(os.environ.get('HERMES_HOME', ''))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sched, "_hermes_home", None)

        job = {
            "id": "script1",
            "name": "profile-script",
            "profile": "support",
            "script": "print_home.py",
            "no_agent": True,
        }

        success, _doc, response, error = sched.run_job(job)

        assert success is True, error
        assert response.strip() == str(profile_home.resolve())
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_run_job_without_profile_leaves_hermes_home_untouched(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, _profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "noprof",
            "name": "no-profile-job",
            "profile": None,
            "schedule_display": "manual",
        }

        success, *_ = sched.run_job(job)

        assert success is True
        assert observed["hermes_home_during_init"] == str(root)
        assert os.environ["HERMES_HOME"] == str(root)

    def test_run_job_falls_back_on_missing_runtime_profile(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, _profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "missing-profile",
            "name": "missing-profile-job",
            "profile": "missing",
            "schedule_display": "manual",
        }

        # Should succeed with fallback, not raise
        success, _output, _response, error = sched.run_job(job)

        assert success is True, f"run_job should fallback, not fail: error={error!r}"
        # Verify it used the default home, not the missing profile
        assert observed["hermes_home_during_init"] == str(root)
        assert os.environ["HERMES_HOME"] == str(root)


class TestTickProfilePartition:
    def test_profile_and_workdir_combined(self, isolated_cron_profile_home, monkeypatch):
        """Both profile and workdir set — verify both are applied and restored."""
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        observed: dict = {}
        TestRunJobProfileContext._install_agent_stubs(monkeypatch, observed)
        fake_workdir = str(root / "myproject")
        (root / "myproject").mkdir()

        job = {
            "id": "combo",
            "name": "combo-job",
            "profile": "support",
            "workdir": fake_workdir,
            "schedule_display": "manual",
        }

        success, _output, _response, error = sched.run_job(job)

        assert success is True, error
        assert observed["hermes_home_during_init"] == str(profile_home.resolve())
        assert os.environ.get("TERMINAL_CWD", "") != fake_workdir, \
            "TERMINAL_CWD should be restored after job"
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_profile_jobs_run_sequentially(self, isolated_cron_profile_home, monkeypatch):
        import threading
        import cron.scheduler as sched

        profile_job = {"id": "a", "name": "A", "profile": "default"}
        parallel_job = {"id": "b", "name": "B", "profile": None}

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [profile_job, parallel_job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "claim_job_for_fire", lambda *_a, **_kw: True)

        calls: list[tuple[str, str]] = []

        def fake_run_job(
            job, *, defer_agent_teardown=None, extra_prompt=None,
            cancel_event=None, execution_id=None,
        ):
            calls.append((job["id"], threading.current_thread().name))
            return True, "output", "response", None

        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr(sched, "save_job_output", lambda _jid, _o: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)

        assert n == 2
        ids = [job_id for job_id, _thread_name in calls]
        assert ids.index("a") < ids.index("b")
        # Profile jobs are dispatched to the persistent single-thread
        # cron-seq pool — non-blocking, one at a time — not inline on the
        # ticker thread.
        main_thread_name = threading.current_thread().name
        profile_thread_name = next(thread for job_id, thread in calls if job_id == "a")
        assert profile_thread_name != main_thread_name
        assert profile_thread_name.startswith("cron-seq"), profile_thread_name
