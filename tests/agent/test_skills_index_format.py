"""Shape of the <available_skills> index rows (single-skill categories, headers, clipped descriptions)."""
import contextlib
import importlib
import re
import sys
from types import ModuleType

import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import SKILL_PROMPT_DESC_LIMIT, clip_skill_description
from agent.skills_index_delta import index_entries

LONG = "Use when cutting, publishing, or abandoning a hermes-agent release and its notes."


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _skill(root, rel, desc):
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: {desc}\n---\n\nBody.\n", encoding="utf-8")


def test_index_rows_carry_each_name_once_and_never_a_half_word(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills = tmp_path / "skills"
    _skill(skills, "creative/ascii-art", "ASCII art banners.")
    _skill(skills, "creative/pixel-art", "Pixel art sprites.")
    (skills / "creative" / "DESCRIPTION.md").write_text(
        "---\ndescription: Creative content generation and visual design tools.\n---\n", encoding="utf-8")
    _skill(skills, "computer-use", "Drive the desktop.")  # a top-level skill is its own category
    _skill(skills, "github/hermes-release", LONG)

    prompt = build_skills_system_prompt()
    block = re.search(r"<available_skills>\n(.*?)\n</available_skills>", prompt, re.DOTALL).group(1)
    lines = block.splitlines()

    assert "  creative:" in lines and "Creative content generation" not in block
    assert "  - computer-use: Drive the desktop." in lines and "  computer-use:" not in lines
    release = next(line for line in lines if "hermes-release" in line)
    assert release.startswith("  - hermes-release: ")
    shown = release.split(": ", 1)[1]
    assert len(shown) <= SKILL_PROMPT_DESC_LIMIT and shown.endswith("...")
    kept = shown[:-3]
    assert LONG.startswith(kept) and LONG[len(kept)] in " ,"  # cut lands between words
    # The turn-delta parser reads flattened rows like nested ones.
    assert set(index_entries(prompt)) == {"ascii-art", "pixel-art", "computer-use", "hermes-release"}


@pytest.mark.parametrize("sep", ["\t", "\u00a0"], ids=["tab", "nbsp"])
def test_clip_cuts_at_any_whitespace_never_inside_a_word(sep):
    desc = f"alpha{sep}" + f"bravo{sep}" * 12
    shown = clip_skill_description(desc)
    assert len(shown) <= SKILL_PROMPT_DESC_LIMIT and shown.endswith("...")
    assert set(shown[:-3].split(sep)) == {"alpha", "bravo"}  # whole words only, no "bra..."



def test_index_desc_limit_widens_the_index_and_rebuilds_a_stale_snapshot(monkeypatch, tmp_path):
    """skills.index_desc_limit shows the full description; flipping it back clips again even though
    no SKILL.md changed (the on-disk snapshot and LRU must not serve rows clipped at the old width)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills", "github/hermes-release", LONG)
    config = tmp_path / "config.yaml"

    def release_row(fresh_process: bool):
        if fresh_process:  # drop only the in-process LRU; the disk snapshot stays
            clear_skills_system_prompt_cache()
        return index_entries(build_skills_system_prompt())["hermes-release"]

    assert release_row(fresh_process=False).endswith("...")
    config.write_text("skills:\n  index_desc_limit: 200\n", encoding="utf-8")
    assert release_row(fresh_process=False) == f"  - hermes-release: {LONG}"  # LRU must not serve the old width
    config.write_text("skills:\n  index_desc_limit: 5\n", encoding="utf-8")  # below the floor: 60
    assert release_row(fresh_process=True).endswith("...")  # nor the disk snapshot
    config.write_text("skills:\n  index_desc_limit: \"\u00b2\"\n", encoding="utf-8")  # isdigit() but not int()
    assert release_row(fresh_process=True).endswith("...")


def test_index_desc_limit_isolated_between_two_live_profiles(monkeypatch, tmp_path):
    """Interleaved multiplex builds keep each profile's width in its prompt snapshots, tool schemas,
    and every authoring surface that consumes the same setting."""
    import model_tools
    from agent import curator
    from agent.learn_prompt import build_learn_prompt
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools import skill_usage
    from tools.skill_linter import lint_content

    homes = {}
    for limit in (60, 160):
        home = tmp_path / f"profile-{limit}"
        _skill(home / "skills", "github/hermes-release", LONG)
        (home / "config.yaml").write_text(f"skills:\n  index_desc_limit: {limit}\n", encoding="utf-8")
        homes[limit] = home

    def in_profile(home, fn):
        token = set_hermes_home_override(str(home))
        try:
            return fn()
        finally:
            reset_hermes_home_override(token)

    for home in homes.values():
        in_profile(home, lambda: skill_usage.mark_agent_created("hermes-release"))

    # Isolate only the memo storage; all reads below go through the public schema provider.
    monkeypatch.setattr(model_tools, "_tool_defs_cache", {})
    monkeypatch.setattr(model_tools, "_tool_defs_expiry", {})
    multiplex = set_multiplex_context(True)
    review_prompts = []

    def capture_review(prompt):
        review_prompts.append((get_hermes_home(), prompt))
        return {"final": "", "summary": "reviewed", "model": "", "provider": "", "tool_calls": [], "error": None}

    monkeypatch.setattr(curator, "_run_llm_review", capture_review)

    def public_index_and_schema(home):
        def build():
            row = index_entries(build_skills_system_prompt())["hermes-release"]
            schema = next(t["function"]["description"] for t in model_tools.get_tool_definitions(
                ["skills"], quiet_mode=True) if t["function"]["name"] == "skill_manage")
            return row, schema
        return in_profile(home, build)

    try:
        expected = {}
        for limit, home in homes.items():  # populate both live LRU and schema-cache entries
            row, schema = public_index_and_schema(home)
            assert row.endswith("...") if limit == 60 else row == f"  - hermes-release: {LONG}"
            assert f"max {limit} chars" in schema
            expected[limit] = (row, schema)
        for limit in (160, 60):  # reverse order: a warm hit must not serve the other profile
            assert public_index_and_schema(homes[limit]) == expected[limit]

        clear_skills_system_prompt_cache()  # disk snapshots remain; schema memo stays warm
        for limit, home in homes.items():
            assert public_index_and_schema(home) == expected[limit]

        content = "---\nname: hermes-release\ndescription: " + "x" * 80 + "\n---\n\nBody.\n"
        for limit, home in homes.items():
            def authoring_surfaces():
                assert f"**<={limit} characters**" in build_learn_prompt("document the release process")
                length_findings = [f for f in lint_content(content) if f.rule == "description-length"]
                assert bool(length_findings) == (limit == 60)
                curator.run_curator_review(synchronous=True, consolidate=True, dry_run=True)
                assert review_prompts[-1][0] == home
                assert f"skill index shows {limit} chars" in review_prompts[-1][1]
            in_profile(home, authoring_surfaces)
    finally:
        reset_multiplex_context(multiplex)
@contextlib.contextmanager
def _without_modules(*names: str):
    saved = []
    for name in names:
        parent_name, _, attr = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        had_attr = parent is not None and hasattr(parent, attr)
        saved_attr = getattr(parent, attr, None) if had_attr else None
        saved_mod = sys.modules.pop(name, None)
        if had_attr:
            delattr(parent, attr)
        saved.append((name, saved_mod, parent, attr, had_attr, saved_attr))
    try:
        yield
    finally:
        for name, saved_mod, parent, attr, had_attr, saved_attr in reversed(saved):
            if saved_mod is not None:
                sys.modules[name] = saved_mod
            else:
                sys.modules.pop(name, None)
            if parent is not None:
                if had_attr:
                    setattr(parent, attr, saved_attr)
                elif hasattr(parent, attr):
                    delattr(parent, attr)


@pytest.mark.parametrize("mod_name", ["agent.curator", "agent.learn_prompt", "tools.skill_linter"])
def test_lazy_skill_consumers_load_and_run_against_n1_skill_utils(monkeypatch, mod_name):
    import agent
    import agent.skill_utils as real_skill_utils

    legacy_utils = ModuleType("agent.skill_utils")
    for attr, val in vars(real_skill_utils).items():
        if attr != "get_skill_desc_limit":
            setattr(legacy_utils, attr, val)
    monkeypatch.setitem(sys.modules, "agent.skill_utils", legacy_utils)
    monkeypatch.setattr(agent, "skill_utils", legacy_utils, raising=False)

    with _without_modules(mod_name):
        mod = importlib.import_module(mod_name)
        if mod_name == "agent.curator":
            assert mod.get_skill_desc_limit() == SKILL_PROMPT_DESC_LIMIT
        elif mod_name == "agent.learn_prompt":
            prompt = mod.build_learn_prompt("summarize api")
            assert f"<={SKILL_PROMPT_DESC_LIMIT} characters" in prompt
            assert "summarize api" in prompt
        else:
            findings = mod.lint_content(
                f"---\nname: long-desc\ndescription: {'x' * (SKILL_PROMPT_DESC_LIMIT + 10)}\n---\n\n# Body\n",
            )
            desc_findings = [f for f in findings if f.rule == "description-length"]
            assert desc_findings and str(SKILL_PROMPT_DESC_LIMIT) in desc_findings[0].message


@pytest.mark.parametrize("mod_name", ["agent.curator", "agent.learn_prompt", "tools.skill_linter"])
def test_lazy_skill_consumers_do_not_swallow_missing_skill_utils(monkeypatch, mod_name):
    monkeypatch.setitem(sys.modules, "agent.skill_utils", None)
    with _without_modules(mod_name), pytest.raises(ModuleNotFoundError):
        importlib.import_module(mod_name)
