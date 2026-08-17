"""Tests for tools/room_fetch_context_tool.py (Raft AX 改造 3 · pull tool)."""

from __future__ import annotations

import json

import pytest

from gateway.agent_room_messages_store import AgentRoomMessagesStore
from tools.room_fetch_context_tool import (
    bind_room_context,
    room_fetch_context,
    _MAX_QUERY_HITS,
    _MAX_RANGE_ROWS,
)


@pytest.fixture()
def store(tmp_path):
    s = AgentRoomMessagesStore(tmp_path / "msgs.sqlite")
    # room R: 6 messages of mixed kinds.
    s.append("R", sender_kind="user", sender_name="alice", content="please refund order 42")
    s.append("R", sender_kind="observer", sender_name="obs", content="routing to finance")
    s.append("R", sender_kind="member", sender_name="finance", content="refund of $50 processed")
    s.append("R", sender_kind="member", sender_name="client_svc", content="anything else?")
    s.append("R", sender_kind="user", sender_name="alice", content="also send the invoice")
    s.append("R", sender_kind="member", sender_name="finance", content="invoice emailed")
    # a second room to prove scope isolation
    s.append("OTHER", sender_kind="user", sender_name="bob", content="secret other-room text")
    yield s
    s.close()


def _bind(store, room="R", member="finance"):
    return bind_room_context(store, room, member)


def test_no_context_bound_returns_error():
    # Nothing bound in this fresh contextvar → structured error, no raise.
    out = json.loads(room_fetch_context(query="anything"))
    assert out["ok"] is False
    assert "no room context" in out["error"]


def test_query_finds_matching_rows(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(query="invoice"))
    finally:
        tok.reset()
    assert out["ok"] is True
    assert out["mode"] == "query"
    contents = [r["content"] for r in out["rows"]]
    assert any("send the invoice" in c for c in contents)
    assert any("invoice emailed" in c for c in contents)


def test_query_is_case_insensitive(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(query="REFUND"))
    finally:
        tok.reset()
    assert out["count"] >= 1
    assert any("refund" in r["content"].lower() for r in out["rows"])


def test_range_returns_inclusive_rows(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(start_seq=2, end_seq=4))
    finally:
        tok.reset()
    assert out["ok"] is True
    assert out["mode"] == "range"
    seqs = [r["seq"] for r in out["rows"]]
    assert seqs == [2, 3, 4]


def test_own_rows_attributed_as_me(store):
    tok = _bind(store, member="finance")
    try:
        out = json.loads(room_fetch_context(start_seq=3, end_seq=3))
    finally:
        tok.reset()
    assert out["rows"][0]["who"] == "me"  # seq 3 is finance's own


def test_other_member_attributed_by_name(store):
    tok = _bind(store, member="finance")
    try:
        out = json.loads(room_fetch_context(start_seq=4, end_seq=4))
    finally:
        tok.reset()
    assert out["rows"][0]["who"] == "client_svc"


def test_user_and_observer_attribution(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(start_seq=1, end_seq=2))
    finally:
        tok.reset()
    whos = {r["seq"]: r["who"] for r in out["rows"]}
    assert whos[1] == "user"
    assert whos[2] == "observer"


def test_scope_isolation_cannot_read_other_room(store):
    # Bound to R → must NOT see OTHER's rows even by query.
    tok = _bind(store, room="R")
    try:
        out = json.loads(room_fetch_context(query="secret other-room"))
    finally:
        tok.reset()
    assert out["ok"] is True
    assert out["count"] == 0


def test_both_query_and_range_rejected(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(query="x", start_seq=1))
    finally:
        tok.reset()
    assert out["ok"] is False
    assert "not both" in out["error"]


def test_neither_query_nor_range_rejected(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context())
    finally:
        tok.reset()
    assert out["ok"] is False


def test_reversed_range_is_normalized(store):
    tok = _bind(store)
    try:
        out = json.loads(room_fetch_context(start_seq=4, end_seq=2))
    finally:
        tok.reset()
    seqs = [r["seq"] for r in out["rows"]]
    assert seqs == [2, 3, 4]


def test_range_row_cap(store, tmp_path):
    big = AgentRoomMessagesStore(tmp_path / "big.sqlite")
    for i in range(_MAX_RANGE_ROWS + 10):
        big.append("B", sender_kind="user", sender_name="u", content=f"m{i}")
    tok = bind_room_context(big, "B", "m")
    try:
        out = json.loads(room_fetch_context(start_seq=1, end_seq=10**9))
    finally:
        tok.reset()
    big.close()
    assert out["count"] == _MAX_RANGE_ROWS
    assert out["truncated"] is True


def test_token_reset_clears_context(store):
    tok = _bind(store)
    tok.reset()
    # After reset, no context is bound again.
    out = json.loads(room_fetch_context(query="invoice"))
    assert out["ok"] is False


def test_registered_in_room_member_toolset():
    from tools.registry import registry
    defs = registry.get_definitions({"room_fetch_context"})
    assert len(defs) == 1
    assert defs[0]["function"]["name"] == "room_fetch_context"
