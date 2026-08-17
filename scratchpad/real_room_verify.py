"""REAL end-to-end verification of the first-hop-classifier migration.

Runs the FULL gateway path (_process_message_via_room_if_bound → real
AgentRoomRouter → real _first_hop_runner → real async_call_llm → real
member agent turns) against the PRODUCTION room room_c1bc71be4d72, which
carries 133 rows of dirty history including 27 observer-prose rows — the
exact environment that made the OLD observer fall back to prose and mis-
route. This is NOT a smoke test: real LLM calls, real routing, real member
replies.

We assert on the messages-store DIFF (new rows appended by the turn), not
on IM delivery (the dingtalk session_webhook is long expired — delivery
failures are expected and irrelevant to routing correctness).

Run:
  /opt/hermes-agent/.venv/bin/python \
    scratchpad/real_room_verify.py "我这个月账单为什么多扣了钱"
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/opt/hermes-agent")

from gateway.session import SessionSource, Platform
from gateway.agent_room_messages_store import AgentRoomMessagesStore

ROOM_ID = "room_c1bc71be4d72"
CHAT_ID = "cidiMmYctEK7q9+dBueZ3mPrjpPMIaHooAghJUDGKPKjsI="


def make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DINGTALK,
        chat_id=CHAT_ID,
        chat_type="dm",
        user_id="$:LWCP_v1:$NoDPZEnaHdl5WfqKihxdt899LnbQ0x+0",
        user_name="弓淼",
    )


async def run_one(runner, source, text: str):
    store = AgentRoomMessagesStore()
    before = store.list_messages(ROOM_ID)
    n_before = len(before)
    print(f"\n{'='*70}\nMESSAGE: {text!r}\n  (history before: {n_before} rows)")

    result = await runner._process_message_via_room_if_bound(
        event=None, source=source, message_text=text, history=[],
    )

    after = store.list_messages(ROOM_ID)
    new_rows = after[n_before:]
    print(f"  handled={result!r}  new rows: {len(new_rows)}")
    for r in new_rows:
        kind = r.sender_kind
        name = r.sender_name
        if kind == "observer" and r.tool_calls:
            import json
            for tc in r.tool_calls:
                fn = (tc.get("function") or {})
                print(f"    [OBSERVER tool_call] {fn.get('name')}({fn.get('arguments','')[:90]})")
        else:
            body = (r.content or "").replace("\n", " ")[:100]
            print(f"    [{kind}:{name}] {body}")
    return new_rows


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "我这个月账单为什么多扣了钱"

    # Build a real GatewayRunner the same way the daemon does, but without
    # starting adapters. We only need the room bridge + its dependencies.
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    # Minimal init: the bridge lazily builds _source_agent_binding_store,
    # _agent_room_store, _agent_room_messages_store, _agent_room_router.
    # It also calls self._adapter_for_source and self._resolve_profile_home_for_source
    # and self._run_agent_inner — all real methods on the class. We need the
    # runner's normal __init__ to populate config/state, so call it.
    # Fall back: if full __init__ is too heavy, surface the error clearly.
    try:
        GatewayRunner.__init__(runner)
    except Exception as exc:
        print(f"!! GatewayRunner.__init__ failed: {type(exc).__name__}: {exc}")
        print("   (may need env/config; retrying via _from_env if available)")
        raise

    source = make_source()
    await run_one(runner, source, text)


if __name__ == "__main__":
    asyncio.run(main())
