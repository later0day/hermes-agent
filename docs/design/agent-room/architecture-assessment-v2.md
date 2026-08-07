# Agent Room 架构评估 v2 —— Spike 验证后修订版

**版本说明**：v1（`architecture-assessment.md`）基于对现有代码的**印象判断**，
本 v2 基于**实际代码抽样验证**（8 组 spike）做出修订。凡是与 v1 结论冲突
之处以本文为准。

## Spike 概览

| # | Spike 目标 | v1 假设 | v2 实际发现 | 影响 |
|---|---|---|---|---|
| 1 | `_profile_runtime_scope` 通用性 | 假设是无条件通用的原语 | **只在 multiplex_profiles=True 时被现有代码调用**，但机制本身无条件工作 | Room 场景要**主动**在非多路复用模式下也调它，代码模式是新增的但机制成熟 |
| 2 | `memory.enabled` config 字段 | 假设需要新加此字段 | **不需要**——`memory.memory_enabled` 已存在且默认 `False` | 观察者 profile 什么都不写就自动无 memory，M1 spec §2.1 需修正 |
| 3 | 默认工具注入 | 担心 `toolsets: [room_observer]` 锁不住 | **能锁住**——`_HERMES_CORE_TOOLS` 是按 toolset 名打包的，不是自动注入的 | Room 观察者行为边界确实能被单一 toolset 锁死 |
| 4 | 观察者 loop 终止 | 三方案备选 | **方案 A 有清晰实现路径**：`agent/interrupt_compat.py::request_hard_interrupt(agent)` + `agent/subagent_lifecycle.py::get_active_subagent_parent()` 已提供 agent 引用注入 | 不需要改 agent runtime，`route_to_member` 工具内部就能触发中断 |
| 5 | `SessionSource` 克隆 | 猜是 `dataclasses.replace` 能干 | **确认可以**——纯 dataclass，无隐藏可变状态 | 直接 `replace(source, profile="...")` 即可 |
| 6 | `_active_profile_name` 全仓审计 | 担心 93 处调用点会返回 gateway 默认 profile | **绝大多数是 scope-aware 的**——通过 `get_hermes_home()` 读 ContextVar，跟随 scope 自动切换 | v1 §2.1 的担忧**基本推翻**，唯一例外是 `_kanban_notifier_profile` 在 gateway __init__ 里固化，跟 Room 无关 |
| 7 | agent turn 入口 | 假设复用 `_run_agent_inner` | **确认可行**——`_run_agent`（外层）就是"多路复用时套 scope、否则直通"的 wrapper。Room 应新写一个平级 `_run_room_agent`，无条件套 scope | 结构清晰，模式可仿 |
| 8 | ContextVar 隔离 | 假设 async-safe | **确认**——`_HERMES_HOME_OVERRIDE` 是 ContextVar，copy_context 后每个 asyncio task 独立 | 并发消息不用加锁 |

## 修订后的架构契合度评分：**9/10**（v1 打 8/10）

v1 打 8/10 时对两处主要摩擦点估计过悲观：

1. `_active_profile_name` 会污染 scope —— **实际不会**（ContextVar 传播）
2. `memory.enabled` 需要新加字段 —— **实际已存在**

这两处消解后，摩擦点只剩：
- `_profile_runtime_scope` 从"多路复用专属调用模式"扩展为"任意场景可调"是新用法，但不改机制本身
- 三个存储位置的一致性维护仍在（sqlite room 表 + binding 表 + profile 目录）
- 观察者作为"自动生成 profile"仍是新概念

**没有发现任何架构死结级别的冲突**。

## v1 → v2 具体修订清单

### 修订 1：v1 §2.1 "gateway 的 `_active_profile_name()` 隐性假设"

**v1 说**：需要 M1 前审计所有 `_active_profile_name()` 调用点。

**v2 修订**：**不需要**。`get_active_profile_name()`（`hermes_cli/profiles.py:1855`）实
现是 scope-aware 的——它通过 `get_hermes_home()` 读 `_HERMES_HOME_OVERRIDE`
ContextVar，在 `_profile_runtime_scope` 里调用会自动返回当前 scope 的
profile 名。93 处调用点里绝大多数是 tools/agent 层的，它们在观察者/成员
turn 内会跑在正确的 scope 下，自然拿到正确的 profile 名。

**唯一需要主动关注**的是 `gateway/run.py:6310` 处 `self._kanban_notifier_profile
= self._active_profile_name()`——它是 gateway `__init__` 时（在任何 scope
之外）固化的，但那本来就是它的意图（gateway 启动时的默认通知 profile），
跟 Room 场景无逻辑冲突。

### 修订 2：v1 §2.3 "`memory.enabled` config 字段"

**v1 说**：需要 spike 确认字段是否存在，不存在需要 M1 加。

**v2 修订**：**字段名是 `memory.memory_enabled`（不是 `memory.enabled`）**，
`agent/agent_init.py:1691` 读取，**默认 `False`**。也就是说 M1 spec §2.1
里让观察者 profile 显式写 `memory.enabled: false` 是错的——**观察者 profile
什么都不用写就自动无 memory**，只在需要开 memory 的 profile 才显式写
`memory.memory_enabled: true`。M1 spec §2.1 需修改。

### 修订 3：v1 §2.4 "默认工具注入可能锁不住"

**v1 说**：担心 hermes-agent 有默认工具自动注入，`toolsets: [room_observer]`
锁不死观察者。

**v2 修订**：**确认能锁死**。查 `toolsets.py:31`，`_HERMES_CORE_TOOLS`
是一个具体工具名列表，被打包成 `hermes-cli`/`hermes-cron`/`hermes-telegram`
等命名 toolsets，只在 `enabled_toolsets` 明确列出这些名字时才装载。
不写 `hermes-cli` 就没有 `_HERMES_CORE_TOOLS` 里的任何工具。M1 观察者只列
`room_observer` toolset 是能真正锁死行为的。**不需要 `disable_default_tools`
额外开关**。

### 修订 4：v1 §4.1 "观察者 loop 终止机制"（**这个是关键修订**）

**v1 说**：方案 A/B/C 三选一，倾向方案 A 但需要额外验证。

**v2 修订**：**方案 A 有明确落地路径**，走三步：

1. **拿到当前 agent 引用**：`route_to_member` 工具函数体内调
   `from agent.subagent_lifecycle import get_active_subagent_parent`
   拿到当前正在跑的 agent 实例（现有 `_ACTIVE_PARENT_AGENT` ContextVar
   已经在 tool 调度期间绑定）
2. **强制中断 loop**：从 `agent.interrupt_compat import request_hard_interrupt`
   → `request_hard_interrupt(agent, "route decided")`
3. **工具返回**：正常返回 `{"action": "route_to_member", "member": ..., "reason": ...}`
   ——即使 loop 不立即中断，下一次 iteration 检查 `_interrupt_requested`
   会看到 True 并退出

**不需要改 agent runtime**、**不需要新的 turn 终止协议**——所有零件都已
存在。这是 M1 最重要的架构风险澄清。

### 修订 5：v1 §4.4 "跨成员上下文摘要注入" 落地方案

**v1 说**：M1 spec 应该加入"观察者在 `route_to_member` 的 reason 字段里
带上一位成员的最后回复摘要"的缓解措施。

**v2 修订**：这个措施仍然必要，且**实施细节明确**：

- 观察者的 SOUL.md 系统提示里加一段："如果检测到话题延续但要换成员，在
  `reason` 字段里带上上一位成员的最后一句总结"
- Room router 在派发给成员前，读取 `route_to_member` 的 `reason` 字段，
  如果里面含跨成员摘要，前置到给成员的消息里作为 context prefix
- 具体的"上一位成员的最后回复"数据源：观察者 session 里最近的历史消息
  （观察者 session 本身就记录了完整路由链，只是观察者自己的对话消息很
  少，因为它只调 tool 不产文本）

**M1 spec §3.2 步骤 5 需要新增这一步**。

### 修订 6：v1 §3.1 "新增 SQLite 数据库"

**v2 补充**：现有的 SQLite 初始化模板在 `gateway/source_agent_binding.py::_execute_sqlite_init`
（跟 WAL 模式、并发锁、重试等）已经处理完备，M1 直接照抄即可，不需要重
新处理这些细节。

## v2 修订后的 M1 前置架构 spike 清单

原 v1 §7.2 列了 6 项 spike（2-4 小时），v2 已经**做完 5 项**，剩下的:

**必须还要做**（约 30 分钟）：
- **Multiplexer 交互确认（v1 §7.2 项 6 的完整版）**：本 v2 SPIKE 8 只验证了
  ContextVar 的隔离性和入口结构，还需要**跑一次真实测试**：在
  `multiplex_profiles: True` 的 gateway 里发一条消息触发 Room 路由，确认
  Room 的 `_profile_runtime_scope` 嵌套（多路复用外层已经套了一层，Room
  再套一层）不会因 ContextVar 重复 set/reset 出错。这个测试只能在 M1 实
  现出来后跑，不能提前。

**不再需要做**（v2 已消解）：
- ~~`_active_profile_name` 全仓审计~~（scope-aware，不需要）
- ~~`memory.enabled` 字段确认~~（已存在为 `memory.memory_enabled`）
- ~~默认工具注入路径确认~~（`_HERMES_CORE_TOOLS` 是显式打包，不自动注入）
- ~~观察者 loop 终止机制选型 + POC~~（方案 A 落地路径明确）
- ~~`SessionSource` 克隆语义确认~~（纯 dataclass，`dataclasses.replace` 可用）

**M1 前置架构工作量从 v1 的 2-4 小时降到 30 分钟**（且是可以在编码开始后
再做的测试）。

## v2 修订后的 M1 工作量估计

原估 2500 行 / 1.5-2 周（v1 一开始的估计），v1 加入架构风险缓解后修正为
3000-3500 行 / 2-3 周。

v2 又消解了几个假想风险后：

**~2700-3000 行 / 2 周实际编码时间**（含测试、不含 review 迭代）。

## v2 修订后的 M1 spec 需补充/修改点

（这些是 M1 spec `m1-spec.md` 需要作为附录或补丁应用的）

### M1 spec 需要修改：

1. **§2.1 观察者 profile 目录结构**：
   - ❌ ~~config.yaml 里写 `memory.enabled: false`~~
   - ✅ 什么都不写（默认就是 `memory.memory_enabled: false`）
   - 或者显式写 `memory: {memory_enabled: false}` 作为明确的文档注释

### M1 spec 需要新增：

2. **§2.5 观察者 loop 终止实现**（方案 A 落地）：
   ```python
   # tools/room_router_tool.py
   def route_to_member(member: str, reason: str, is_new_topic: bool = False) -> dict:
       from agent.subagent_lifecycle import get_active_subagent_parent
       from agent.interrupt_compat import request_hard_interrupt
       agent = get_active_subagent_parent()
       if agent is not None:
           request_hard_interrupt(agent, f"Route decided: {member}")
       return {
           "action": "route_to_member",
           "member": member,
           "reason": reason,
           "is_new_topic": is_new_topic,
       }
   ```

3. **§3.2 Step 4.5（在 Step 4 和 Step 5 之间新增）**：跨成员上下文摘要
   注入
   ```
   Step 4.5: 如果 route_to_member 的 reason 字段带有跨成员摘要
             （观察者判断 is_new_topic=False 但换了成员时的产物），
             把这段摘要作为 context prefix 拼到给成员的消息前面，
             格式如：
             > 上一位处理人 {previous_member} 的回复摘要：{summary}
             > ---
             > 用户消息：{original_message}
   ```

4. **§7.4 验收补充**：新增一个测试用例——**多路复用 gateway 下的 Room
   路由**：确认 `_profile_runtime_scope` 双重嵌套（多路复用外层 + Room 内
   层）在同一条消息处理中不出错。

## v2 总结

**架构契合度上调到 9/10**。之前 v1 里担心的两处主要摩擦点都被 spike 证实是
误判。观察者 loop 终止方案 A 有明确、无需改动 agent runtime 的实现路径，
且用的都是现有基础设施（`request_hard_interrupt` + `get_active_subagent_parent`）。

**M1 前置工作量从 2-4 小时降到 30 分钟的验证测试**（且这个测试可以在
M1 编码开始之后再做）。

**总 M1 工作量估计**：~2700-3000 行 / 2 周实际编码。

**没有发现任何设计层面必须回退或大改的问题**。Room 设计跟 hermes-agent 现有
架构**能直接融合**：
- 用 `_profile_runtime_scope` 做跨 profile 执行——机制现成
- 用 `SourceAgentBindingStore.fallback_extra.room_id` 扩展绑定——零迁移
- 用 `request_hard_interrupt` + `get_active_subagent_parent` 做 loop 终止——现成
- 用 `dataclasses.replace(source, profile=...)` 做 SessionSource 修正——纯 stdlib
- 用 kanban 现有的 `_execute_sqlite_init` 模板做新库初始化——照抄
- 用 aux LLM 客户端模式做轻量分类——4 个现成参考实现（specify/decompose/describer/goals）

**唯一保留的、必须在 M1 里做好的两条**：
1. 观察者 loop 终止用方案 A 的现成路径（不发明新协议）
2. 跨成员上下文摘要注入（否则用户体验断崖）

以此为准，M1 可以进入编码阶段。
