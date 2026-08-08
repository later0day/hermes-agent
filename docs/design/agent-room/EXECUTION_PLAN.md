# Agent Room · 执行计划（已选定：全套 M1 → M4）

> 本文档是开发期间的进度追踪基准。设计依据见 `design.html`（`e94a417e5`）。
> 交付路径：**全套 M1 → M4，预估 8-10 周，~7300-8700 行**（不含 review + 上线运维）。

## 进度总览

| 期 | 状态 | 里程碑数 | 预估行数 | 预估周期 |
|---|---|---|---|---|
| M1 | 🔵 进行中 | 8 | ~2500-3000 | 2 周 |
| M2 | ⚪ 未开始 | 4 | ~1500-1800 | 1.5 周 |
| M3 | ⚪ 未开始 | 5 | ~1800-2100 | 3 周 |
| M4 | ⚪ 未开始 | 5 | ~1500-1800 | 2 周 |

**跨期回归原则**（HTML §11cross）：每期交付前必须回归前期全部验收 case。M2 跑 M1；M3 跑 M1+M2；M4 跑 M1+M2+M3。用 `scripts/run_tests.sh` per-file 隔离模式。

**线上测试环境准备**（HTML §11env，M1-M4 共用）：
- 专属测试 DingTalk 群（不用生产群）
- 专属测试 profile 前缀 `room_test_*`
- 生产 `.venv`（`/opt/hermes-agent/.venv`，335 包），不用测试 venv
- 日志级别临时提到 DEBUG，测试完恢复
- 每期结束清理：`/room delete test_*` + `hermes profile delete room_test_*` + 清测试 SQLite 数据

---

## M1 · 核心闭环

### 里程碑（严格依赖顺序 + 可并行段）

- [x] **M1.1** 数据层 `gateway/agent_room_store.py`（~250 行）— **完成**（428 行 + 366 行测试，27/27 通过 + 负控制验证，commit `770950f03`）
- [ ] **M1.2** 观察者构造器 `gateway/agent_room_bootstrapper.py`（~200 行）— 含 §8 规则A（SOUL.md 摘要指令）
- [ ] **M1.3** 路由工具 + Loop 终止 `tools/room_router_tool.py`（~80 行）← §9.2 硬补丁A
- [ ] **M1.4** toolsets 注册 `toolsets.py` 改（~10 行）
- [ ] **M1.5** 路由主流程 `gateway/agent_room_router.py`（~350 行）— 含 §6.1 完整 5 步 + 4.5 步（§8 规则B）
- [ ] **M1.7** run.py 入口分支改（~50 行）
- [ ] **M1.6**（可与 M1.5 并行，只依赖 M1.1+M1.2）Slash Commands `slash_commands.py` 改（~300 行）
- [ ] **M1.8**（可与 M1.5 并行）REST API + Dashboard `web_server.py` 改（~250）+ 前端（~400）

测试代码（~600 行）跟着各里程碑同步写，不设独立里程碑。

### M1 验收：7 项 A/B 指标
- [ ] 路由准确率 ≥85%
- [ ] 用户满意度 B组≥A组
- [ ] 响应时延 ≤A组+3秒
- [ ] N4 沿用率 ≥40%
- [ ] Aux LLM 成本 ≤A组+30%
- [ ] 跨成员摘要有效性 ≥60%
- [ ] 生产稳定性零 crash

### M1 边界测试：14 项（M1-B1 ~ M1-B14）
成员缺失拒建 / 特殊字符转义 / 成员超限 / member空字符串兜底 / webhook过期 / 成员内delegate_task / 1秒3条并发 / SOUL.md手改覆盖 / 不一致状态幂等删除 / 改绑竞态Fence / gateway重启中断 / AuxLLM降级 / SQLite锁重试 / 成员强删兜底

### M1 前置 spike
30 分钟——多路复用交互测试（需 M1 代码写出后才能跑，不能提前）

---

## M2 · 自动规划 room（前提：M1 完整交付）

### 里程碑
- [ ] **M2.1** Prompt 模板 `gateway/agent_room_planner_prompts.py`（~150 行）
- [ ] **M2.2** 规划器 `gateway/agent_room_planner.py`（~350 行）— 复用 kanban_decompose 模板
- [ ] **M2.3** 命令+确认交互 `slash_commands.py` 改（~200 行）— `/room plan` + Y/N 确认
- [ ] **M2.4** REST API + Dashboard 规划预览页 `web_server.py` 改（~180）+ 前端（~300）

测试代码（~400 行）同步写。

### M2 前置 spike
30 分钟——确认 `auxiliary_client.py` 复用模式 + `create_profile`/`profile_describer` 无阻塞调用 + Dashboard 工具链能加新页

### M2 验收：4 项 A/B 指标
- [ ] 成员重叠率（vs 人工）≥70%
- [ ] 规划响应 ≤8 秒
- [ ] 幻觉率 =0%
- [ ] 用户确认率 ≥80%

### M2 边界测试：10 项（M2-B1 ~ M2-B10）
需求过短/过长 / 幻觉成员 / 7个成员超限截取 / 拒绝后清零 / 非法JSON宽容解析 / create_profile中途失败回滚 / room重名 / 并发plan排队 / 特殊字符转义

### M2 关键约束（易漏）
**确认前不能创建任何 profile/room 记录**——DoD 明确要求，防 aux LLM 幻觉产出意外后果

---

## M3 · 完整上下文投影 + 并发多成员（前提：M1 完整交付，不依赖 M2）

### 里程碑
- [ ] **M3.1** 前置 spike（2-3 小时，必须先做完再估工作量）
  - dry-run 迁移脚本验证不丢消息
  - 500 条消息投影性能测试（<100ms）
  - DingTalk QPS 压测（3/5 并发）
- [ ] **M3.2** 消息存储层 `gateway/agent_room_messages_store.py`（~250 行）— canonical order 排序
- [ ] **M3.3** 投影算法 `gateway/agent_room_projection.py`（~400 行）— 单人称视角改写
- [ ] **M3.4** 数据迁移 `scripts/migrate_m1_to_m3.py`（~200 行）
- [ ] **M3.5** 并发派发重写 `agent_room_router.py` 改（+200 行）+ `room_router_tool.py` 改（+50 行）— 同时删除 M1 摘要注入代码（~-100 行）

测试代码（~800 行）同步写。

### M3 验收：5 项 A/B 指标
- [ ] 断链率 ≤A组的50%（至少减半）
- [ ] 平均解决轮次 ≤A组-1轮
- [ ] token成本增量 ≤A组+80%
- [ ] 单次投影 ≤100ms
- [ ] **M1所有A/B指标不能退化**

### M3 边界测试：10 项（M3-B1 ~ M3-B10）
100+条历史截断策略 / 同时到达消息不乱序 / 5成员全并发QPS / 1个成员失败不影响其他 / 迁移幂等空操作 / 旧session_id向后兼容 / 大附件截断4000字符 / 并发中Fence / 写入失败不crash / 迁移后历史不重复不乱序

### M3 明确的删除动作（易漏）
交付时必须**移除 M1 §8 的摘要注入代码**——不是保留两套并存，是完整替换

---

## M4 · 任务拆分（前提：M1必需+kanban；M3可选，仅并行执行才需要）

### 里程碑
- [ ] **M4.1** 前置 spike（1 小时）
  - 确认 kanban_decompose.py 可直接复用
  - 确认 assignee 校验可扩展成"必须是 room 成员"
  - 敲定综合 turn 用观察者 session（不新起）
- [ ] **M4.2** 拆分工具 `tools/room_decompose_tool.py`（~150 行）+ `toolsets.py` 改（~5 行）
- [ ] **M4.3** 任务编排器 `gateway/agent_room_task_orchestrator.py`（~400 行）
- [ ] **M4.4** 综合turn分支 `agent_room_router.py` 改（+150 行）
- [ ] **M4.5** SOUL.md扩展 `agent_room_bootstrapper.py` 改（+50 行）— 只加"复杂任务用 decompose_and_route"段

测试代码（~500 行）同步写，前端任务图可视化（~250 行）可并行开发。

### M4 验收：4 项 A/B 指标
- [ ] 简单需求误拆分率 =0%
- [ ] 复杂需求完成质量提升 ≥1分（5分制）
- [ ] 依赖执行顺序正确率 100%
- [ ] 综合turn覆盖率 100%

### M4 边界测试：8 项（M4-B1 ~ M4-B8）
简单问题误判拆分 / 循环依赖拒绝 / assignee越界拒绝 / 全部子任务失败仍要有综合回复 / room删除中途Fence / 子任务数量超限自动收窄 / 综合turn期间新消息不阻塞 / 拆分/普通模式历史穿插不干扰

### M4 明确不做的（易误加）
不做子任务间实时并发；不做跨room委托；不改M1整体SOUL.md结构（只追加一段）

---

## 变更日志

- 2026-08-08：文档创建，选定全套 M1→M4 路径，开始 M1.1 开发
