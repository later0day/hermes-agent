"""Writers that run after a profile is deleted mid-turn must not rebuild its home. Live 2026-10-15 an
interrupted turn's teardown recreated profiles/<p>/ with sessions/<id>.jsonl (transcript divert),
runtime/active_sessions.lock and projects.db; each created its directory with a raw mkdir(parents=True)
instead of mkdir_under_hermes_home's tombstone guard."""

import shutil

import pytest

from hermes_cli.active_sessions import _FileLock
from hermes_cli.sqlite_util import open_db
from hermes_constants import mark_named_profile_deleted, reset_hermes_home_override, set_hermes_home_override
from hermes_state import divert_session_transcript_jsonl

WRITERS = {
    "transcript_divert": lambda home: divert_session_transcript_jsonl("s1", [{"role": "user", "content": "x"}]),
    "active_session_lock": lambda home: _FileLock(home / "runtime" / "active_sessions.lock").__enter__(),
    "open_db": lambda home: open_db(home / "projects.db", db_label="projects.db", wal=False).close(),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_writer_works_live_and_refuses_after_delete(tmp_path, writer):
    home = tmp_path / ".hermes" / "profiles" / "gone"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    token = set_hermes_home_override(home)
    try:
        WRITERS[writer](home)  # a live profile is unaffected
        shutil.rmtree(home)
        mark_named_profile_deleted(home)
        with pytest.raises(FileNotFoundError, match="Named profile home does not exist"):
            WRITERS[writer](home)
    finally:
        reset_hermes_home_override(token)
    assert not home.exists()
