import sys
import types


def test_gateway_prefers_project_cron_over_profile_namespace(tmp_path, monkeypatch):
    from gateway import run as gateway_run

    profile_cron = tmp_path / "cron"
    profile_cron.mkdir()

    shadow = types.ModuleType("cron")
    shadow.__file__ = None
    shadow.__path__ = [str(profile_cron)]
    monkeypatch.setitem(sys.modules, "cron", shadow)
    monkeypatch.syspath_prepend(str(tmp_path))

    gateway_run._prefer_project_package("cron")

    from cron.scheduler import _resolve_home_env_var
    from cron.scheduler_provider import resolve_cron_scheduler

    assert _resolve_home_env_var("dingtalk") == "DINGTALK_HOME_CHANNEL"
    assert callable(resolve_cron_scheduler)


def test_cli_gateway_entry_prefers_project_cron_over_profile_namespace(tmp_path, monkeypatch):
    from hermes_cli import gateway as gateway_cli

    profile_cron = tmp_path / "cron"
    profile_cron.mkdir()

    shadow = types.ModuleType("cron")
    shadow.__file__ = None
    shadow.__path__ = [str(profile_cron)]
    monkeypatch.setitem(sys.modules, "cron", shadow)
    monkeypatch.syspath_prepend(str(tmp_path))

    gateway_cli._prefer_project_package("cron")

    from cron.scheduler_provider import resolve_cron_scheduler

    assert callable(resolve_cron_scheduler)
