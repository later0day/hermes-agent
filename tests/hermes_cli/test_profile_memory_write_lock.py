"""The dashboard's memory editor writes under the memory tool's file lock.

The agent's memory tool does lock → read → modify → write; a dashboard save that skipped the lock
could land between its read and write and be silently overwritten (or overwrite the agent's update).
"""
import asyncio
import contextlib
from pathlib import Path

from tools.memory_tool_store import MemoryStore


def test_dashboard_memory_save_happens_inside_the_memory_lock(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_cli.web_server  # noqa: F401  — mounts the routers' late() targets
    from hermes_cli.web_routers.profiles import ProfileMemoryUpdate, update_profile_memory

    held = []
    real_lock = MemoryStore._file_lock

    @contextlib.contextmanager
    def recording_lock(path):
        with real_lock(path):
            held.append(Path(path).name)
            yield
            held.pop()

    writes_under_lock = []
    import utils
    real_write = utils.atomic_write_text

    def recording_write(path, *args, **kwargs):
        writes_under_lock.append(list(held))
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "_file_lock", staticmethod(recording_lock))
    monkeypatch.setattr(utils, "atomic_write_text", recording_write)

    asyncio.run(update_profile_memory("default", "MEMORY.md", ProfileMemoryUpdate(content="note")))

    assert writes_under_lock == [["MEMORY.md"]]
    assert (home / "memories" / "MEMORY.md").read_text() == "note"
