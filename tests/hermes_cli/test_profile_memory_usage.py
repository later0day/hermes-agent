"""The dashboard's memory editor shows how full each store is, counted exactly the way the memory tool
counts it (the tool refuses `add` past the limit). Live 2026-10-15 xcx's MEMORY.md sat at 2167/2200 with
nothing on the page saying so."""
import asyncio
from pathlib import Path

from tools.memory_tool_store import MemoryStore

RAW = "  first entry \n§\n\n§\nsecond § entry 👍\n§\n第三条：用户偏好中文\n"


def test_usage_matches_the_memory_tool_and_the_profiles_own_limits(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "memories").mkdir(parents=True)
    (home / "config.yaml").write_text("memory:\n  memory_char_limit: 120\n", encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text(RAW, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_cli.web_server  # router imports resolve through the app module
    from hermes_cli.web_routers.profiles import get_profile_memory

    memory = asyncio.run(get_profile_memory("default", "MEMORY.md"))
    user = asyncio.run(get_profile_memory("default", "USER.md"))

    store = MemoryStore(memory_char_limit=120)
    store.load_from_disk()
    assert memory["chars"] == store._char_count("memory")
    assert memory["char_limit"] == 120
    assert (user["exists"], user["chars"], user["char_limit"]) == (False, 0, 1375)
