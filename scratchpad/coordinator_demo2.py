"""同 coordinator_demo.py，但直连 dashscope openai-compatible endpoint，
绕开 auxiliary_client 内部的 provider health-tracking/fallback 熔断器
（上次跑因为熔断器把 alibaba 标记 unhealthy 600s，多次超时排队到 openrouter/nous
都没配额，最终整体挂死）。这里直接用 openai.AsyncOpenAI 打 dashscope，逻辑不变。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, "/opt/hermes-agent")

from openai import AsyncOpenAI

BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ["DASHSCOPE_API_KEY"]
MODEL = "qwen3.7-max"

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)

MEMBERS = [
    ("frontend", "负责前端页面开发、UI实现、交互调试。"),
    ("backend", "负责后端接口开发、数据库、服务部署。"),
    ("qa", "负责功能测试、Bug复现、回归验证。"),
]
VALID = {n for n, _ in MEMBERS}

CASES = [
    "这个需求这周五前能不能上线？如果来不及要不要先砍掉次要功能？",
    "客户说预算超了，这个项目要不要继续做下去，还是先停一停？",
    "现在人手不够，前端和后端谁先做，你们内部先定一下优先级",
]


async def _call(messages, **kw):
    for attempt in range(3):
        try:
            return await client.chat.completions.create(
                model=MODEL, messages=messages, timeout=60, **kw
            )
        except Exception as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(2)


def build_system_A() -> str:
    roster = "\n".join(f"- {name}: {desc}" for name, desc in MEMBERS)
    return (
        "你是一个群聊路由器。群里有以下成员，每人负责不同领域：\n"
        f"{roster}\n\n"
        "根据【用户消息】判断应该由哪一位成员处理。\n"
        "只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"members": ["成员名"], "reason": "一句话理由"}\n'
        "- members 数组不能为空，必须严格是上面列出的成员名之一。\n"
        "- 如果无法判断，放 frontend 兜底。\n"
    )


async def run_A(msg: str) -> dict:
    messages = [
        {"role": "system", "content": build_system_A()},
        {"role": "user", "content": f"【用户消息】{msg}"},
    ]
    resp = await _call(messages, temperature=0.0, max_tokens=128,
                        extra_body={"response_format": {"type": "json_object"}})
    raw = resp.choices[0].message.content
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    decision = json.loads(m.group(0)) if m else {}
    routed_to = decision.get("members", ["frontend"])
    if not routed_to or routed_to[0] not in VALID:
        routed_to = ["frontend"]

    member = routed_to[0]
    member_desc = dict(MEMBERS)[member]
    reply_messages = [
        {"role": "system", "content": f"你是 {member}，职责：{member_desc}\n请直接回复用户的消息。"},
        {"role": "user", "content": msg},
    ]
    reply_resp = await _call(reply_messages, temperature=0.3, max_tokens=300)
    return {
        "routed_to": member,
        "reason": decision.get("reason", ""),
        "final_reply": reply_resp.choices[0].message.content,
    }


def build_system_B() -> str:
    roster = "\n".join(f"- {name}: {desc}" for name, desc in MEMBERS)
    return (
        "你是一个群聊路由器。群里有以下成员，每人负责不同领域：\n"
        f"{roster}\n\n"
        "根据【用户消息】判断应该由哪一位成员处理。\n"
        "只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"members": ["成员名", ...], "reason": "一句话理由"}\n'
        "- members 数组元素必须严格是上面列出的成员名之一。\n"
        "- 如果这个问题不属于任何成员的专业范畴（比如涉及排期决策、"
        "预算、优先级、要不要做这类需要拍板的问题），返回空数组 "
        "members: []，reason 说明为什么不属于任何成员范畴。\n"
    )


async def run_B(msg: str) -> dict:
    messages = [
        {"role": "system", "content": build_system_B()},
        {"role": "user", "content": f"【用户消息】{msg}"},
    ]
    resp = await _call(messages, temperature=0.0, max_tokens=128,
                        extra_body={"response_format": {"type": "json_object"}})
    raw = resp.choices[0].message.content
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    decision = json.loads(m.group(0)) if m else {}
    routed_to = [x for x in decision.get("members", []) if x in VALID]

    if routed_to:
        member = routed_to[0]
        member_desc = dict(MEMBERS)[member]
        reply_messages = [
            {"role": "system", "content": f"你是 {member}，职责：{member_desc}\n请直接回复用户的消息。"},
            {"role": "user", "content": msg},
        ]
        reply_resp = await _call(reply_messages, temperature=0.3, max_tokens=300)
        return {
            "routed_to": member,
            "reason": decision.get("reason", ""),
            "final_reply": reply_resp.choices[0].message.content,
        }

    coordinator_messages = [
        {
            "role": "system",
            "content": (
                "你是这个项目的 PM/leader，团队里有 frontend/backend/qa 三个专业角色。\n"
                "这条消息不属于任何专业角色的职责范畴（已经过路由判断确认），"
                "现在轮到你处理：你有权直接拍板、给出决策意见，或者反问用户澄清，"
                "或者决定接下来该由谁配合执行。不要说'我不知道该找谁'，"
                "你就是那个该做决定的人。"
            ),
        },
        {"role": "user", "content": msg},
    ]
    coord_resp = await _call(coordinator_messages, temperature=0.3, max_tokens=300)
    return {
        "routed_to": "(无匹配 → coordinator/PM 接手)",
        "reason": decision.get("reason", ""),
        "final_reply": coord_resp.choices[0].message.content,
    }


async def main() -> None:
    for i, msg in enumerate(CASES, 1):
        print("=" * 100, flush=True)
        print(f"问题 {i}: {msg}", flush=True)
        print("-" * 100, flush=True)
        a = await run_A(msg)
        print(f"[A 现状] 路由到: {a['routed_to']}  (理由: {a['reason']})", flush=True)
        print(f"[A 现状] 回复:\n{a['final_reply']}", flush=True)
        print("-" * 100, flush=True)
        b = await run_B(msg)
        print(f"[B 改法] 路由到: {b['routed_to']}  (理由: {b['reason']})", flush=True)
        print(f"[B 改法] 回复:\n{b['final_reply']}", flush=True)
        print(flush=True)


if __name__ == "__main__":
    asyncio.run(main())
