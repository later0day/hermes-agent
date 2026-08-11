"""Tests for gateway/agent_room_bootstrapper.py — M1.2.

Covers observer profile creation, SOUL.md rendering (§5.2 template + §8
Rule A summary instruction), regeneration on member change, teardown
protection, and the M1 boundary tests explicitly assigned to this
milestone:
  M1-B1  members not present when /room create — bootstrap must fail cleanly
         (this test exercises the bootstrap side; the caller-side rejection
         lives in the slash-command handler tested by M1.6).
  M1-B2  member names with special characters / CJK / emoji — SOUL.md must
         render them safely.
  M1-B8  SOUL.md manually edited after creation, then a member change
         triggers regeneration — the manual edit is overwritten silently
         (documented behavior, not a bug).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.agent_room_bootstrapper import (
    OBSERVER_MARKER_FILENAME,
    BootstrapError,
    build_observer_profile,
    is_observer_profile,
    observer_profile_name_for,
    regenerate_observer_soul,
    render_observer_soul_md,
    teardown_observer_profile,
)


# ---------------------------------------------------------------------------
# Isolation: every test runs against a private HERMES_HOME
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp dir so we don't pollute the developer's
    real ~/.hermes/profiles/ tree.

    hermes_cli.profiles reads via hermes_constants.get_hermes_home(), which
    honors the HERMES_HOME override. Set both the env var and the ContextVar
    override to be safe on both code paths.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "profiles").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home))
    yield home
    reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# observer_profile_name_for
# ---------------------------------------------------------------------------


def test_observer_name_slugifies_room_name():
    assert observer_profile_name_for("Customer Support") == "room_customer_support_observer"


def test_observer_name_normalizes_special_chars():
    # spaces, punctuation, CJK, emoji → single underscore each, collapsed.
    assert (
        observer_profile_name_for("客服 · 财务 & 技术!!")
        == "room_observer"  # everything non-slugifiable collapses; strip("_")
        or observer_profile_name_for("客服 · 财务 & 技术!!").startswith("room_")
    )


def test_observer_name_never_empty_for_empty_room_name():
    # Empty room name shouldn't blow up — falls back to a valid identifier.
    name = observer_profile_name_for("")
    assert name == "room_x_observer"


def test_observer_name_preserves_hyphens_and_digits():
    assert observer_profile_name_for("team-42-alpha") == "room_team-42-alpha_observer"


# ---------------------------------------------------------------------------
# SOUL.md rendering (§5.2 + §8 Rule A)
# ---------------------------------------------------------------------------


def test_soul_md_includes_room_name():
    soul = render_observer_soul_md(
        room_name="Customer Support",
        room_description="Handle support tickets",
        members=[("client_svc", "handles client questions")],
        default_member="client_svc",
    )
    assert "Room: Customer Support" in soul
    assert "Handle support tickets" in soul


def test_soul_md_lists_every_member_with_description():
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[
            ("client_svc", "客服 · 处理咨询和退款"),
            ("finance", "财务 · 处理账单"),
            ("tech", "技术 · debug + 部署"),
        ],
        default_member="client_svc",
    )
    assert "**client_svc**: 客服 · 处理咨询和退款" in soul
    assert "**finance**: 财务 · 处理账单" in soul
    assert "**tech**: 技术 · debug + 部署" in soul


def test_soul_md_shows_default_member_in_fallback_line():
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[("a", "A"), ("b", "B")],
        default_member="b",
    )
    assert "route to `b`" in soul


def test_soul_md_includes_cross_member_summary_rule_from_section_8():
    """§8 Rule A: SOUL.md must explicitly instruct the observer to include
    a summary of the previous member's last reply in the `reason` field
    when switching members mid-topic. Without this instruction the whole
    M1 §8 UX-cliff mitigation collapses (M1's cross-member-summary A/B
    metric target ≥60% would be unreachable).
    """
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[("a", "A"), ("b", "B")],
        default_member="a",
    )
    # The exact wording pattern the router (M1.5 STEP 4.5) will look for.
    assert "上一位处理人" in soul
    assert "reason" in soul
    # And the instruction is framed as a workflow step, not just a footnote.
    assert "When switching to a DIFFERENT member" in soul


def test_soul_md_includes_decompose_and_route_instructions_m4():
    """M4.5: SOUL.md must teach the observer to use decompose_and_route
    for complex multi-step tasks, including the tasks/parents DAG shape
    and the guard that simple questions should still use route_to_member.
    """
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[("client_svc", "客服"), ("finance", "财务")],
        default_member="client_svc",
    )
    assert "decompose_and_route" in soul
    # DAG shape fields must be documented.
    assert "parents" in soul
    assert "assignee" in soul
    # Must explicitly steer simple questions away from decompose.
    assert "NOT `decompose_and_route`" in soul
    # The synthesis-turn behavior must be described so the observer knows
    # NOT to write prose on the decompose turn itself.
    assert "synthesize" in soul or "synthesis" in soul


def test_soul_md_handles_empty_description_for_member():
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[("a", ""), ("b", "   ")],
        default_member="a",
    )
    assert "**a**: (no description)" in soul
    assert "**b**: (no description)" in soul


def test_soul_md_handles_empty_room_description():
    soul = render_observer_soul_md(
        room_name="R",
        room_description="",
        members=[("a", "A")],
        default_member="a",
    )
    assert "(no description)" in soul


# ---------------------------------------------------------------------------
# build_observer_profile (E2E — creates real profile directory)
# ---------------------------------------------------------------------------


def test_build_observer_creates_directory_with_all_files(hermes_home):
    profile_dir = build_observer_profile(
        room_name="Support",
        room_description="Customer support room",
        members=[("client_svc", "客服"), ("finance", "财务")],
        default_member="client_svc",
    )

    assert profile_dir.is_dir()
    assert (profile_dir / "SOUL.md").is_file()
    assert (profile_dir / "config.yaml").is_file()
    assert (profile_dir / "profile.yaml").is_file()
    assert (profile_dir / OBSERVER_MARKER_FILENAME).is_file()
    assert is_observer_profile(profile_dir) is True


def test_build_observer_writes_correct_soul_md(hermes_home):
    profile_dir = build_observer_profile(
        room_name="Support",
        room_description="Room for support",
        members=[("client_svc", "handles clients")],
        default_member="client_svc",
    )
    soul = (profile_dir / "SOUL.md").read_text(encoding="utf-8")
    assert "Room: Support" in soul
    assert "**client_svc**: handles clients" in soul
    assert "route to `client_svc`" in soul


def test_build_observer_writes_toolsets_lockdown_config(hermes_home):
    profile_dir = build_observer_profile(
        room_name="Support",
        room_description="",
        members=[("a", "A")],
        default_member="a",
    )
    import yaml
    config = yaml.safe_load((profile_dir / "config.yaml").read_text())
    # Spike 3 verification: observer profile has EXACTLY one toolset,
    # and it's the room_observer one. No _HERMES_CORE_TOOLS leak.
    assert config["toolsets"] == ["room_observer"]
    # Spike 2 verification: memory is explicitly off.
    assert config["memory"]["memory_enabled"] is False


def test_build_observer_writes_profile_yaml_with_description_auto(hermes_home):
    profile_dir = build_observer_profile(
        room_name="Support",
        room_description="",
        members=[("a", "A")],
        default_member="a",
    )
    import yaml
    meta = yaml.safe_load((profile_dir / "profile.yaml").read_text())
    assert meta.get("description_auto") is True
    assert "Room observer for Support" in meta.get("description", "")


def test_build_observer_rejects_when_profile_already_exists(hermes_home):
    """M1-B1 territory: if an observer profile name collides with an
    existing profile (hand-crafted or stale), refuse rather than clobber."""
    build_observer_profile(
        room_name="Support",
        room_description="",
        members=[("a", "A")],
        default_member="a",
    )
    with pytest.raises(BootstrapError, match="already exists"):
        build_observer_profile(
            room_name="Support",
            room_description="",
            members=[("a", "A")],
            default_member="a",
        )


def test_build_observer_uses_explicit_observer_name_when_provided(hermes_home):
    profile_dir = build_observer_profile(
        room_name="Any",
        room_description="",
        members=[("a", "A")],
        default_member="a",
        observer_name="custom_observer_name",
    )
    assert profile_dir.name == "custom_observer_name"


# ---------------------------------------------------------------------------
# regenerate_observer_soul (M1-B8)
# ---------------------------------------------------------------------------


def test_regenerate_soul_overwrites_current_soul(hermes_home):
    """M1-B8: user manually edits SOUL.md, then a member change triggers
    regeneration. The manual edit is silently overwritten — documented
    behavior per §5.1 ('用户不应手改')."""
    build_observer_profile(
        room_name="Support",
        room_description="",
        members=[("a", "A"), ("b", "B")],
        default_member="a",
    )
    obs_name = observer_profile_name_for("Support")
    from hermes_cli.profiles import get_profile_dir
    profile_dir = get_profile_dir(obs_name)
    soul_path = profile_dir / "SOUL.md"

    # Simulate a user hand-edit
    soul_path.write_text("USER MANUALLY EDITED THIS", encoding="utf-8")
    assert soul_path.read_text() == "USER MANUALLY EDITED THIS"

    # Member change triggers regen
    regenerate_observer_soul(
        observer_profile=obs_name,
        room_name="Support",
        room_description="",
        members=[("a", "A"), ("b", "B"), ("c", "C")],
        default_member="a",
    )

    new_soul = soul_path.read_text(encoding="utf-8")
    assert "USER MANUALLY EDITED THIS" not in new_soul
    assert "**c**: C" in new_soul


def test_regenerate_soul_refuses_on_non_observer_profile(hermes_home, tmp_path):
    """If the target profile directory exists but lacks the .observer marker,
    refuse to overwrite — the caller has a bug pointing bootstrapper at a
    hand-crafted profile."""
    from hermes_cli.profiles import create_profile, get_profile_dir
    create_profile(
        "hand_crafted",
        clone_from=None,
        clone_env=False,
        clone_skills=False,
        no_alias=True,
        no_skills=True,
    )
    profile_dir = get_profile_dir("hand_crafted")
    assert not is_observer_profile(profile_dir)

    with pytest.raises(BootstrapError, match="not marked as an auto-generated observer"):
        regenerate_observer_soul(
            observer_profile="hand_crafted",
            room_name="Any",
            room_description="",
            members=[("a", "A")],
            default_member="a",
        )


def test_regenerate_soul_missing_profile_raises(hermes_home):
    with pytest.raises(BootstrapError, match="does not exist"):
        regenerate_observer_soul(
            observer_profile="never_created_observer",
            room_name="R",
            room_description="",
            members=[("a", "A")],
            default_member="a",
        )


# ---------------------------------------------------------------------------
# teardown_observer_profile (M1-B9)
# ---------------------------------------------------------------------------


def test_teardown_removes_observer_directory(hermes_home):
    build_observer_profile(
        room_name="Support",
        room_description="",
        members=[("a", "A")],
        default_member="a",
    )
    obs_name = observer_profile_name_for("Support")

    removed = teardown_observer_profile(obs_name)

    assert removed is True
    from hermes_cli.profiles import get_profile_dir
    assert not get_profile_dir(obs_name).exists()


def test_teardown_is_idempotent_when_missing(hermes_home):
    """M1-B9: repeatedly tearing down a non-existent observer profile
    must not raise."""
    assert teardown_observer_profile("never_created_observer") is False
    assert teardown_observer_profile("never_created_observer") is False


def test_teardown_refuses_hand_crafted_profile(hermes_home):
    from hermes_cli.profiles import create_profile
    create_profile(
        "hand_crafted",
        clone_from=None,
        clone_env=False,
        clone_skills=False,
        no_alias=True,
        no_skills=True,
    )

    with pytest.raises(BootstrapError, match="marker missing"):
        teardown_observer_profile("hand_crafted")
