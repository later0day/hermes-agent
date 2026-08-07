# Agent Room — Design Overview

Agent Room 给 hermes-agent 增加一层新的会话组织概念：**一组预定义 profile
成员 + 一个观察者 profile**，绑定到某个 IM 群（初期为 DingTalk）后，群消息
由观察者判断路由给成员中的一个 profile 应答，异步回结果。

## 与现有概念的关系

- **Profile**：hermes-agent 原生概念，一个独立 agent 身份（`~/.hermes/profiles/<name>/`）。
- **SourceAgentBinding**：现有的"IM 会话 → 单个 profile"绑定表
  （`gateway/source_agent_binding.py`）。Agent Room **不替代**它——现有
  的"群绑定单 profile"用法保持不变，Room 是叠加在同一张表上的第二种绑定
  模式（通过 `fallback_extra.room_id` 字段区分）。
- **Kanban delegate**：现有的"派任务给指定 assignee profile + 异步通知回原
  会话"机制（`gateway/kanban_delegate.py` + `hermes_cli/kanban_db.py`）。
  Agent Room 的观察者路由决策在 M4 阶段会复用 kanban 的任务拆分能力，M1
  暂不集成。
- **Kanban 自动分诊器**（`hermes_cli/kanban_decompose.py`）：现有的
  "aux LLM 读 profile 名册 → 分派 assignee"算法。Agent Room 的观察者路由决
  策**借鉴同一套 aux LLM 模式**，但独立实现（观察者的输入是群消息而非 kanban
  triage task，输出通过 tool call 而非 JSON 文本）。

## 分期规划

Agent Room 完整能力按四期落地。当前设计文档只覆盖 M1。

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 手动创建 room、手动绑定群、观察者单成员路由、异步回复；用户上下文以"成员被路由到过的历史"为准（简化的理解 X） | 设计中，本文档 |
| M2 | 从一句自然语言需求自动规划 room（能力域拆解 → 现有 profile 匹配 → 缺失 profile 自动创建 + description 生成） | 未开始 |
| M3 | 完整上下文投影层：成员看到 room 完整历史（单人称改写）+ 消息乱序修复 + 并发多成员回复 | 未开始 |
| M4 | 观察者决策"这是复杂任务"时走 kanban 拆分路径，子任务图执行 + 综合回群 | 未开始 |

## 关键设计决策一览（用户拍板结果）

| 决策 | 选择 |
|---|---|
| A1 观察者形态 | 有状态 agent（真实 profile，非无状态 LLM 调用） |
| A2 路由粒度 | 默认单成员选定，用户显式请求多方讨论时才拆分（拆分放 M4） |
| A3 回复时序 | 异步（DingTalk 场景强制） |
| A4 成员并发 | 上下文共享 + 单点触发（**理解 X**）：所有成员看到 room 历史，但一次只由观察者选中的成员回复 |
| A5 命令入口 | Slash command + Dashboard UI 双入口 |
| A6 绑定关系 | 群 ↔ Room 一对一（改绑必须先解绑） |
| N1 观察者记忆 | 每 room 独立观察者 profile，各自记各自 |
| N2 消息乱序处理 | M1 简单按时间戳，暂不做保序 |
| N3 room 规模上限 | 最多 5 成员/room，优先复用现有 profile |
| N4 观察者调用时机 | 先跑轻量 aux LLM 分类（新话题?），是则跑完整观察者 turn，否则沿用上次路由 |

详见 `m1-spec.md`。

## 相关文档

- [`m1-spec.md`](./m1-spec.md) — M1 完整技术规格
- [`architecture-assessment.md`](./architecture-assessment.md) — 相对 hermes-agent 现有架构的评估
