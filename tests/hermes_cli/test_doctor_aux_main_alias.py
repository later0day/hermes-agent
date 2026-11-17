"""``auxiliary.<task>.provider: main`` is inheritance, not an unknown provider.

The auxiliary client maps ``main`` to the profile's own main provider (``_normalize_aux_provider``), so
those tasks run fine; doctor fed it to ``resolve_runtime_provider`` and reported every such block as
"does not resolve" (16 false failures on a stock older config).
"""
from __future__ import annotations


def test_main_alias_is_not_reported_but_a_bogus_provider_still_is(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from hermes_cli import doctor_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"auxiliary": {
        "compression": {"provider": "main", "model": ""},
        "title_generation": {"provider": "MAIN"},
        "background_review": {"provider": "no-such-provider", "model": "m"},
    }}))
    issues = []
    doctor_config._validate_auxiliary_config(cfg_file, issues)
    assert len(issues) == 1 and "auxiliary.background_review" in issues[0]
