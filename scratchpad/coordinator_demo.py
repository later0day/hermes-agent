"""对照实验：leader/PM/前端/后端/测试 房间场景，验证"研究一下方向"里的核心判断。

跑同一批问题，分别用：
  A) 现状：分类器被限定必须从 members 里选一个（minItems:1，禁止不选），
     default_member 设为 "frontend"（房间成员列表第一个，纯配置，无协调语义）。
  B) 改法：分类器允许输出 route=None（表示"无匹配/需要协调"），此时才真正调用
     一个独立的"coordinator/PM"角色——这个角色不受 route_to_member 工具锁死，
     可以自由回答、反问、或说"这个我来定，转给谁"。

两组用完全一样的 system 设定（同一个模型 qwen3.7-max via dashscope），
唯一变量是"无匹配时的出口"，对比真实回复质量。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, "/opt/hermes-agent")

from agent.auxiliary_client import async_call_llm

BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
MODEL = "qwen3.7-max"

async def _call_with_retry(**kwargs):
    last = None
    for _ in range(3):
        try:
            return await async_call_llm(**kwargs)
        except Exception as exc:
            last = exc
            await asyncio.sleep(2)
    raise last


MEMBERS = [
    ("frontend", "负责前端页面开发、UI实现、交互调试。"),
    ("backend", "负责后端接口开发、数据库、服务部署。"),
    ("qa", "负责功能测试、Bug复现、回归验证。"),
]
VALID = {n for n, _ in MEMBERS}

# 三个测试问题：都不属于 frontend/backend/qa 任何一个专业范畴，
# 需要的是"项目该不该做/优先级/资源"这类决策，只有 leader/PM 能答。
CASES = [
    "这个需求这周五前能不能上线？如果来不及要不要先砍掉次要功能？",
    "客户说预算超了，这个项目要不要继续做下去，还是先停一停？",
    "现在人手不够，前端和后端谁先做，你们内部先定一下优先级",
]


def build_system_A() -> str:
    """现状：分类器必须从 members 里选一个（工具 schema minItems:1）。"""
    roster = "\n".join(f"- {name}: {desc}" for name, desc in MEMBERS)
    return (
        "你是一个群聊路由器。群里有以下成员，每人负责不同领域：\n"
        f"{roster}\n\n"
        "根据【用户消息】判断应该由哪一位成员处理。\n"
        "只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"members": ["成员名"], "reason": "一句话理由"}\n'
        "- members 数组不能为空，必须严格是上面列出的成员名之一。\n"  # <- minItems:1 的真实约束
        "- 如果无法判断，放 frontend 兜底。\n"  # <- default_member = members[0]，无语义
    )


async def run_A(msg: str) -> dict:
    messages = [
        {"role": "system", "content": build_system_A()},
        {"role": "user", "content": f"【用户消息】{msg}"},
    ]
    resp = await _call_with_retry(
        provider="alibaba", model=MODEL, base_url=BASE_URL, api_key=API_KEY, messages=messages,
        temperature=0.0, max_tokens=128, timeout=30,
        extra_body={"response_format": {"type": "json_object"}},
    )
    raw = resp.choices[0].message.content
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    decision = json.loads(m.group(0)) if m else {}
    routed_to = decision.get("members", ["frontend"])
    if not routed_to or routed_to[0] not in VALID:
        routed_to = ["frontend"]

    # 现状：路由决定后，被硬塞的专业成员（如 frontend）必须直接回答用户，
    # 它没有"我不该决定这个"的合法输出通道，只能勉强答或跑题。
    member = routed_to[0]
    member_desc = dict(MEMBERS)[member]
    reply_messages = [
        {"role": "system", "content": f"你是 {member}，职责：{member_desc}\n请直接回复用户的消息。"},
        {"role": "user", "content": msg},
    ]
    reply_resp = await _call_with_retry(
        provider="alibaba", model=MODEL, base_url=BASE_URL, api_key=API_KEY, messages=reply_messages,
        temperature=0.3, max_tokens=300, timeout=30,
    )
    return {
        "routed_to": member,
        "reason": decision.get("reason", ""),
        "final_reply": reply_resp.choices[0].message.content,
    }


def build_system_B() -> str:
    """改法：分类器允许"无匹配"输出（member: []），此时转给独立的协调者角色。"""
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
    resp = await _call_with_retry(
        provider="alibaba", model=MODEL, base_url=BASE_URL, api_key=API_KEY, messages=messages,
        temperature=0.0, max_tokens=128, timeout=30,
        extra_body={"response_format": {"type": "json_object"}},
    )
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
        reply_resp = await _call_with_retry(
            provider="alibaba", model=MODEL, base_url=BASE_URL, api_key=API_KEY, messages=reply_messages,
            temperature=0.3, max_tokens=300, timeout=30,
        )
        return {
            "routed_to": member,
            "reason": decision.get("reason", ""),
            "final_reply": reply_resp.choices[0].message.content,
        }

    # 无匹配 → 转给独立的 coordinator/PM 角色，这个角色不受"必须选一个成员"锁死，
    # 可以自己判断、拍板、反问，甚至直接给出决策意见。
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
    coord_resp = await _call_with_retry(
        provider="alibaba", model=MODEL, base_url=BASE_URL, api_key=API_KEY, messages=coordinator_messages,
        temperature=0.3, max_tokens=300, timeout=30,
    )
    return {
        "routed_to": "(无匹配 → coordinator/PM 接手)",
        "reason": decision.get("reason", ""),
        "final_reply": coord_resp.choices[0].message.content,
    }


async def main() -> None:
    for i, msg in enumerate(CASES, 1):
        print("=" * 100)
        print(f"问题 {i}: {msg}")
        print("-" * 100)
        a = await run_A(msg)
        print(f"[A 现状] 路由到: {a['routed_to']}  (理由: {a['reason']})")
        print(f"[A 现状] 回复:\n{a['final_reply']}")
        print("-" * 100)
        b = await run_B(msg)
        print(f"[B 改法] 路由到: {b['routed_to']}  (理由: {b['reason']})")
        print(f"[B 改法] 回复:\n{b['final_reply']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
