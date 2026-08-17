# Agent Room 相对 hermes-agent 架构的评估

本文档独立于 M1 spec，从 hermes-agent **现有架构**出发评估这套 Room 设计是
否契合、有哪些天然的耦合点、哪些是新增的架构负担、哪些设计假设可能与现有
不变量冲突。目的是在动手前把架构层面的隐性成本暴露出来。

## 1. 契合点：设计天然利用的现有基础设施

### 1.1 `_profile_runtime_scope` context manager (gateway/run.py:1984)

**这是整个 Room 设计的最大幸运**。它已经把"临时切换到指定 profile 的
HERMES_HOME + secret scope + 内嵌 .env"打包成一个 context manager：

```python
with _profile_runtime_scope(profile_home):
    # 这个块里 get_hermes_home() 返回 profile_home
    # get_secret() 读的是这个 profile 的 .env
    # 内部 agent turn 完全跑在这个 profile 的身份下
```

Room 路由需要在一条消息里先跑观察者 profile 一次 turn、再跑成员 profile
一次 turn，两次 scope 嵌套/串接是 hermes-agent 已经支持的原生操作。**不
需要新写"跨 profile 执行子逻辑"的机制**——这项工作在多 profile 网关支持
里已经做完了。

**评估**：+++ Room 设计跟这个 scope 机制**完美契合**。

### 1.2 `SourceAgentBindingStore` (gateway/source_agent_binding.py)

现有的"IM 会话 → profile 名"绑定表，且它的 `fallback_extra` 是 JSON blob，
可以自然扩展新键 `room_id` 而**不改表结构、不写迁移**。运行时读取路径
（`gateway/run.py::_resolve_profile_home_for_source`，line 24690）也已经在
消息入口上被查询。

**评估**：+++ 复用它避免了在同一入口做两次 lookup，且完全向后兼容——所有
现有"群→单profile"绑定继续正常工作，Room 是叠加语义。

### 1.3 `SourceAgentBinding.fallback_extra` 存 DingTalk session_webhook

Fork 已经在这个字段里存 DingTalk 的 `session_webhook` 用于"事后发消息回
群"。Room 场景下"观察者路由完成后发一条 '转交给 X 处理' 消息"和"成员回
复完了发结果回群"用的正是这个 webhook 机制——**基础设施已就位**。

**评估**：++ DingTalk 场景的异步回复所需机制已经存在，不需要新增。

### 1.4 现有的辅助 LLM 客户端 (agent/auxiliary_client.py + 4 个参考实现)

Room 里的两处 LLM 调用都可以复用同一模式：

- **观察者的轻量分类（N4）**：直接照 `kanban_specify.py` / `kanban_decompose.py` /
  `profile_describer.py` 那套 `_extract_json_blob` 模式抄
- **观察者本身（有状态 agent turn）**：走标准 hermes agent turn（不是 aux
  client），复用 `_run_agent_inner`

**评估**：++ 两条 LLM 路径都有现成模板可抄，不用从零设计错误处理/超时/
JSON 解析容错。

### 1.5 Fence 机制的思路借鉴（Studio Group Chat）

Studio 的 fence 是"清空/删除房间上下文时保护正在跑的 turn 不污染新状
态"，跟 M1 的"改绑/删除 room 时保护正在跑的观察者/成员 turn"是同一类问
题。**M1 直接照它抄一个简化版**（只在 room 结构变化时 fence，不做每消息
细粒度 fence），成本很低但预防了一整类竞态 bug。

**评估**：+ 借鉴设计模式，不引入 Studio 的实现复杂度。

## 2. 摩擦点：跟现有架构假设有轻微紧张关系

### 2.1 gateway "只服务一个 profile 或一个多路复用集合"的隐性假设

现有的 `GatewayRunner` 有 `_active_profile_name()`（line 12675），代码里
多处基于"这个 gateway 主要服务的 profile"做默认值兜底（比如
`_kanban_notifier_profile = self._active_profile_name()`）。**Room 打破了
这个假设**：一条消息进来后，先跑观察者（一个 profile）、再跑成员（另一
个 profile），中间没有"主 profile"这个概念。

**具体风险**：
- Room 路由过程中调用某个依赖 `_active_profile_name()` 的辅助函数，它可
  能返回 gateway 启动时的"默认 profile"，跟当前正在跑的观察者/成员的
  profile 不一致，产生审计/通知误路由
- 例：`_kanban_notifier_profile` 如果被观察者/成员 turn 触及，可能把
  kanban 通知记到错的 profile 名下

**M1 缓解**：
- 观察者和成员的 turn 都在 `_profile_runtime_scope` 包裹下跑，
  `get_hermes_home()` / `get_secret()` 是隔离的
- 但**代码里所有裸调 `_active_profile_name()` 的地方**都要审计一次，看
  是不是应该改成"当前 scope 的 profile name"

**评估**：⚠️ 需要 M1 实施时**做一次全仓审计**：grep `_active_profile_name`
调用点，凡是在 agent turn 路径上出现的都要评估是否需要改成 scope-aware。
预计不多但必须做。

### 2.2 SessionSource 的 profile 归属

`SessionSource.profile` 字段在 `build_source` 阶段就被填充（基于 URL 前
缀 `/p/<profile>/` 或 profile_routes 匹配）。Room 路由发生在这之后，观察
者跑 turn 时如果直接沿用原 SessionSource，`source.profile` 可能会指向绑
定前的 profile 或空，不指向观察者的 profile。

**具体风险**：agent turn 内部很多地方读 `source.profile`（比如做 profile
scoped 的 kanban 查询、file access 检查），如果这个字段不对，观察者可能
读到错的 profile 数据。

**M1 缓解**：
- 进入 room 路由时**克隆一份 SessionSource**，把 `profile` 字段改成当前
  正在跑的 profile 名（观察者 turn 时改成 observer_profile，成员 turn
  时改成 target_member）
- 原 SessionSource 保留在外层，供回消息、审计用

**评估**：⚠️ 中等复杂度，但边界清晰——只在 room router 的两次 turn 前后
做 source 克隆/还原即可。

### 2.3 观察者 profile 的 `memory.enabled: false` 需要 config.yaml 支持

设计里让观察者 profile 不启用长期记忆。查现有 config 是否有这个开关：

```
core/profile 里 memory.enabled 是否是标准配置？
如果没有，需要 M1 阶段先扩这一个字段。
```

**评估**：⚠️ 需要 spike 5 分钟确认。如果字段不存在，M1 要多加一个 config
schema 改动，或者用别的方式禁用 memory（比如 skip 加载 MEMORY.md）。

### 2.4 观察者只能用 `route_to_member` 一个工具，其他工具不能装

设计里锁死 `toolsets: [room_observer]` 只包含一个工具。但 hermes-agent 有
一些**默认注入的工具**（如 `read_file`、`terminal`），如果它们是靠
"toolsets 之外的默认注入" 加载的，那"只装一个 toolset"未必真的能锁死观
察者行为。

**评估**：⚠️ 需要 spike 5-10 分钟确认工具注入路径。如果确实有默认工具注
入，M1 需要额外加一个 profile config 开关 `disable_default_tools: true`
或类似机制。

## 3. 新增架构负担

### 3.1 观察者 profile 是"自动生成的 profile"，profile 生态里第一次出现这个概念

**目前所有 profile 都是用户手动创建的**（`hermes profile create` 或
`/agent create`）。观察者 profile 是**代码自动生成 + 用户不该直接编
辑**的东西。这引入几个新问题：

- `hermes profile list` 会不会把观察者一起列出来？会——需要打个 badge 让
  用户知道这是自动生成的（用 `.observer` 标记文件）
- `/agent delete <observer_profile>` 用户能不能直接删？可以，但会孤立一
  个 room，需要保护逻辑（删除时检查是否被 room 引用）
- 观察者的 `SOUL.md` 是代码模板生成的，用户手工改了怎么办？——M1 每次
  room 成员变动都会**覆盖** SOUL.md，用户改动会丢失。需要文档明确说明
  "不要手改观察者 SOUL"

**评估**：⚠️ 引入了新的 profile 类别，需要在多处（list/delete/UI）识别
并区别对待。工作量分散但每处不大。

### 3.2 三个存储位置的一致性维护

Room 数据分散在三处：

1. `~/.hermes/gateway_agent_rooms.sqlite` — Room 元数据
2. `~/.hermes/gateway_source_agent_bindings.sqlite` — 绑定关系（含 room_id）
3. `~/.hermes/profiles/<observer_profile>/` — 观察者 profile 目录

**每个 Room CRUD 操作要同步三处**。M1 要保证：
- 创建 Room：先建 profile 目录 → 再插 rooms 表（profile 目录建失败要能重试）
- 删除 Room：先解绑（清 fallback_extra.room_id）→ fence → 删 rooms 表 → 删 profile 目录
- 部分失败恢复：中途崩溃后重启 gateway 能识别孤立 profile 目录 + 孤立 rooms 记录并清理

**评估**：⚠️ 中等复杂度。M1 需要一个"孤立数据扫描修复"逻辑，跑在 gateway
启动阶段，类似现有的 `restore_agents` 但方向相反（清理而非恢复）。

### 3.3 新的 SQLite 数据库

`gateway_agent_rooms.sqlite` 是全新一个数据库。hermes-agent 已经有很多
SQLite 文件（state.db、cron/executions.db、gateway_source_agent_bindings.sqlite、
kanban.db 等），再加一个不算离谱，但要复用现有的 SQLite 初始化模板（那些
处理 WAL 模式警告、并发锁、迁移等的样板）。

**评估**：⚠️ 需要照 `source_agent_binding.py::_execute_sqlite_init` 那套
模板抄，不能自己发明轮子。

### 3.4 观察者 session_id 的 collision 风险

设计里观察者 session id 是 `room_observer:{room_id}`。现有 hermes-agent 的
`build_session_key` 生成的是 `agent:main:{platform}:...` 或类似格式，不
会撞。但**需要抽样几个 session id 生成/消费点确认冲突不存在**。

**评估**：⚠️ 5 分钟 spike 能确认。

## 4. 潜在架构风险（值得警惕）

### 4.1 观察者 turn 输出为 route_to_member 工具调用，工具调用后 turn 就该结束——但 hermes-agent 的 agent loop 不一定认

观察者预期是"看消息 → 调 route_to_member 工具 → turn 结束"。但 hermes
agent loop 的正常终止条件是"模型输出不含工具调用的纯文本"。如果模型调
了 route_to_member 后还接一段闲聊文本，loop 会继续 iterate，可能触发额
外的模型调用。

**M1 缓解方案**：
- **方案 A**：让 `route_to_member` 工具执行时抛一个内部 sentinel 异常，被
  room router 捕获，强制中断 agent loop
- **方案 B**：观察者 profile 的系统提示明确要求"只输出工具调用，不输出任
  何文本"，靠模型合作（不可靠）
- **方案 C**：给 room_observer toolset 一个标记 `terminate_on_call: true`，
  agent runtime 见到这个标记就在工具调用后终止 loop

**评估**：⚠️⚠️ **关键风险**。方案 C 最干净但需要改 agent runtime；方案
A 最实用；方案 B 不建议。M1 spec 需要明确这一决策，我倾向 A（异常控制流
最容易验证）。

### 4.2 观察者跑 turn 的成本可能不低

即便有 N4 的轻量分类先行做过滤，"新话题"发生时观察者要跑完整 agent turn，
每次至少 1 次模型调用（可能带工具调用回合，2 次）。如果一个群每天新话题
50 次，一天 50-100 次模型调用是净增成本。

**评估**：⚠️ 需要 M1 交付后用真实使用数据评估经济性。如果发现成本失控，
可能要退回到无状态观察者（跟原来的 A1 选项1 打脸）。M1 spec 里应该埋一
个开关能一键切换到无状态版做 fallback。

### 4.3 观察者路由错了怎么办？

观察者判断错误的路由（比如把财务问题分给了客服）在 M1 没有纠错机制。
成员 profile 收到不擅长的问题，可能：
- 强行回答（错误答案）
- 拒答（用户体验差）
- 内部再 delegate 给别的 profile（`delegate_task` 是可用工具，但用户没
  预期 profile 会自己转派）

**评估**：⚠️ M1 接受这个缺陷（B3 用户答复 "先不做"）。但需要日志记录路
由决策 + reason，方便事后审计"观察者为什么这样判断"。这个日志是 M1 必
须做的运维基础。

### 4.4 M1 简化取舍（成员只看自己被路由过的历史）的用户体验断崖

这是 M1 最激进的简化。真实场景里如果用户在一个 room 里连着问"我怎么退
款"→"客服回答"→"那我还想咨询一下之前的账单"，第二问被路由到财务时，
财务 profile **看不到之前客服的对话**，只看到用户当前这一句"那我还想咨
询一下之前的账单"——上下文完全丢失。

**M1 缓解**：观察者的 SOUL.md 系统提示可以让它在检测到"话题延续但换成员"
时，把上一位成员的最后一条回复摘要塞到 `route_to_member` 的 reason 字段
里，成员 profile 的 room router 层可以把这个 reason 拼进给成员的消息前
面。这不是 M3 那种完整投影，但比"完全无跨成员上下文"好。

**评估**：⚠️⚠️ 这是 M1 最大的用户体验风险。上面的缓解措施必须在 M1 里
实施，否则 M1 交付就会看起来很傻。**M1 spec 应该在 §3.2 明确加入这一
条**。

## 5. 长远兼容性评估（对 M2/M3/M4 是否留了退路）

| M1 决策 | 后期升级路径是否通畅 |
|---|---|
| Room 表用 `members_json` 存字符串数组 | ⚠️ M3 并发时可能要给每个成员挂更多元数据（优先级、是否休眠等）。届时需要迁移到独立 `room_members` 表。M1 → M3 迁移成本中等 |
| `fallback_extra.room_id` 复用 SourceBinding | ✅ M2 自动规划完成后仍然沿用同一入口，无迁移 |
| 观察者用有状态 profile | ✅ M2 里自动生成观察者 profile 只是把 M1 的手动流程自动化 |
| 观察者只能用 route_to_member 工具 | ⚠️ M4 拆分模式需要观察者能调 kanban decompose 工具，这时候要给 room_observer toolset 加第二个工具（`decompose_and_route`），M1 单工具的锁死设计需要放松 |
| 成员 session_id `room_member:{room_id}:{member}` | ✅ M3 全上下文投影会重写成员看到的历史，但 session_id 不变 |
| 内存缓存 `last_routed_member` | ⚠️ M2 自动规划成功后有多 room 场景，缓存要按 room_id 分片（M1 已经是了，OK） |
| Fence 只在 room 结构变时触发 | ⚠️ M3 引入更复杂的并发场景后，可能需要每消息细粒度 fence。M1 → M3 需要重构 fence 触发点 |

**评估**：M1 大部分决策为 M2/M3/M4 留了通畅升级路径。**只有两处会成为技
术债**：(a) members_json 存字符串数组需要在 M3 前迁移；(b) Fence 触发粒
度在 M3 需要重构。都不是死结。

## 6. 与 hermes-agent 生态其他子系统的交互评估

| 子系统 | 交互影响 |
|---|---|
| **Cron** | 无冲突。cron 会用 `create_delegated_kanban_task` 派任务，可以派给 room 成员，不影响 room 路由 |
| **Kanban** | Room 里没有直接用 kanban。M4 才引入 |
| **Multiplexer / Multi-profile** | ⚠️ 需要评估：如果 gateway 是多路复用模式（同时服务多个 profile 的入站），Room 路由是否受影响？初步判断不影响（多路复用是"入站消息路由到哪个 gateway 实例"，Room 是"进入实例后的二次路由"），但需要一次代码验证 |
| **Delegate task** | Room 成员在自己 turn 内可以调 `delegate_task` 派生子 agent，没有冲突 |
| **Approval / write-gate** | Room 成员触发的 approval 走原有机制，无冲突 |
| **Notification (kanban notify_sub)** | Room 场景不直接依赖，但 M4 会集成 |
| **Curator / memory** | 观察者 `memory.enabled: false`，不写入 memory.md，无冲突。成员正常写入自己 profile 的 memory.md |

## 7. 最终评估结论

### 7.1 架构契合度：8/10

- **契合点**：`_profile_runtime_scope`、`SourceAgentBindingStore`、DingTalk
  session_webhook、辅助 LLM 客户端模式、Studio fence 设计——五处主要基
  础设施都已就绪或有直接可抄的参考
- **摩擦点**：`_active_profile_name` 的隐性假设需要审计；
  `SessionSource.profile` 需要在 room router 里克隆修正；两个 profile
  config 字段（memory.enabled / disable_default_tools）需要 spike 确认
- **新负担**：三存储位置一致性维护、观察者作为"自动生成 profile"的新概
  念、观察者 turn 终止机制（`route_to_member` 后如何中断 loop）

### 7.2 M1 交付前必须完成的架构 spike（工作量粗算 2-4 小时）

1. **`_active_profile_name()` 全仓审计**（30 分钟）：识别所有调用点，判定
   哪些需要改成 scope-aware
2. **`memory.enabled` config 字段确认**（10 分钟）：是否存在，不存在则加
3. **默认工具注入路径确认**（20 分钟）：能不能靠 `toolsets: [room_observer]`
   真正锁死观察者行为，否则需要 `disable_default_tools` 开关
4. **观察者 loop 终止机制方案敲定**（60 分钟）：方案 A/B/C 选一个并出小
   POC 验证
5. **`SessionSource` 克隆语义确认**（30 分钟）：是不是 `dataclasses.replace()`
   能干的事，有没有隐藏的可变状态
6. **Multiplexer 交互确认**（30 分钟）：多 profile 服务模式下 Room 路由
   是否需要额外处理

### 7.3 M1 spec 需要补充的两条

1. **观察者 turn 终止方案**（§4.1 的方案 A）——加入 `m1-spec.md` §2 观察
   者构造
2. **跨成员上下文摘要注入**（§4.4）——加入 `m1-spec.md` §3.2 步骤 4-5
   之间

### 7.4 M1 工作量修正估计

原估 2500 行 / 1.5-2 周。加入本文暴露的架构 spike + 未预见的必需缓解措
施后，修正为：

**~3000-3500 行 / 2-3 周实际编码时间**（含测试、不含 review 迭代）。

架构复杂度可控，没有发现死结级别的冲突。**建议按 M1 spec + 本评估文档
的 §7.3 补丁执行**，先跑通闭环，再评估 M2 起点。

## 8. 一句话总结

Room 设计跟 hermes-agent 现有架构**契合度良好**：`_profile_runtime_scope`
已经解决了最大的"跨 profile 执行"问题；`SourceAgentBindingStore` 提供了
零成本的绑定入口扩展；辅助 LLM 客户端 / Studio fence / DingTalk webhook
机制都是可直接借鉴的现成资产。**唯一需要警惕的**是观察者 loop 终止机制
和跨成员上下文摘要——这两点如果不在 M1 里处理好，会直接毁掉用户体验。
其他风险都可以接受为已知的技术债留给 M2/M3/M4 解决。
