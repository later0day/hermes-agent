# Fork 剩余功能全局盘点

> 审计对象：
>
> - 集成分支：`integration/port-fork-features` @ `a09a5e298d9c`
> - Fork：`mine/main` @ `4cb7a60c17f7`
> - 官方：`origin/main` @ `66e258b4b951`
>
> 本文是完成 DingTalk Plan A、动态 source→profile binding、profile 生命周期和
> runtime 闭环后的**剩余功能盘点**。它更新了
> [fork-feature-port-plan.md](./fork-feature-port-plan.md) 中部分已经过时的状态判断，
> 但不修改或删除那份历史移植计划。

## 总结

Fork 相对共同分叉点有约 204 个独有提交，但不能把“提交没有被
cherry-pick”直接等同于“功能没有合入”：大量功能已经由官方后续实现覆盖，
也有一批旧实现已经被 Hosted Rooms、Bots、Kanban、Terminal Provider Registry
等新架构取代。

目前真正剩余的内容主要分为三类：

1. 少数独立的安全、运维和可观测性修复；
2. 需要基于官方新架构重新实现的产品入口或桥接；
3. 不影响 profile runtime 闭环的 Dashboard 管理和体验增强。

## 一、明确仍缺的独立后端项

### 1. Cron creating-chat IDOR 隔离

Fork 提交：

```text
7471c35b6c fix(cron): scope cronjob list/mutate to creating chat (cross-origin IDOR)
```

同一 profile 可以服务多个群、DM 或线程，而这些来源共享该 profile 的 cron
store。当前 `cronjob(action="list")` 和按 ID/名字进行的
`pause/resume/remove/update/run` 没有按创建来源隔离，因此聊天 A 可能枚举或
操作聊天 B 创建的任务。

Fork 版本按 `origin.platform + origin.chat_id` 过滤 list 和 mutation；跨来源
mutation 返回“not found”，避免确认隐藏任务是否存在。CLI、TUI 和由独立 API key
保护的 API Server 保留全库管理能力。

**判断：真实安全边界缺口，是剩余后端项中最应优先单独评估的一项。**

### 2. Shutdown phantom `systemctl --user` 判定

Fork 提交：

```text
6ee6719cab fix(shutdown): reject phantom systemctl --user answer in timing check
```

`gateway/shutdown_forensics.py::check_systemd_timing_alignment` 查询
`systemctl --user` 时，一个不拥有目标 system unit 的 user manager 也可能返回
成功状态及默认 `TimeoutStopSec`，造成错误的 stale-unit 警告。

Fork 修复同时读取 `LoadState`，只接受 `loaded` 的 manager，否则继续查询
system manager。

**判断：独立、窄范围的运维修复，当前没有等价守卫。**

### 3. Turn 日志记录 resolved profile

Fork 提交：

```text
c215623c97 log(turn): include resolved profile in conversation turn log
```

当前 turn 日志没有明确打印请求最终在哪个 profile scope 下执行。Fork 版本使用
profile-aware home/session fallback 解析并记录 profile，便于排查 multiplex、
`/p/<profile>` 和动态 source binding。

**判断：可观测性增强，不是运行正确性缺口。**

## 二、功能仍有价值，但必须按官方新架构重做

### 4. AgentProxy terminal backend

Fork 有 `tools/environments/agentproxy.py`，实现 HTTPS/SSE 远端执行、远端容器
生命周期和退出码 sentinel。当前分支没有 AgentProxy backend。

旧接线方式已经不适配当前架构：它在 core 中硬编码 backend 分支、读取全局环境和
固定路径、默认关闭 TLS 校验，也没有接入 per-profile
`TerminalEnvironmentProvider` registry、`strip_env_keys`、setup 或 doctor。

**正确方向：**把可复用的 SSE transport、退出码解析和远端容器执行重写为仓外
`TerminalEnvironmentProvider` 插件；不要恢复 core 特例。

### 5. Hosted Room 自然语言 roster planner

Fork Agent Room M2 能从自然语言目标选择已有 profiles、提议新角色、生成 roster，
并在用户确认前保持零副作用。官方 Kanban decomposition 解决的是任务拆分，不是长期
Room roster 规划。

**正确方向：**planner 输出 staged proposal，确认后通过官方 profile/Bot 操作和
`HostedRoomService.create_room()` 落地；不要恢复 observer profile 或旧 room
store。

### 6. Messaging `/room` 管理入口

官方 Hosted Rooms 已有 durable backend 和 TUI/Desktop RPC，但消息平台缺少统一的：

```text
/room create|list|show|add|remove|bind|unbind|disband
```

**正确方向：**实现官方 HostedRoomService 的薄命令适配，而不是带回 Fork 的
`AgentRoomStore`。

### 7. IM source/group ↔ Hosted Room binding

当前集成分支已经实现 `source/group → profile`，但这不等于
`source/group → Hosted Room`。若要让 DingTalk、Telegram、Discord 等群直接进入
多成员 Room，仍需对接官方 authority、event replay 和 hosted worker。

### 8. Hosted Room ↔ Kanban DAG ↔ synthesis

官方 Hosted Rooms 和 Kanban 分别具备 durable discussion 与 durable DAG，但缺少完整
产品桥：

```text
Room 中的复杂目标
→ 创建 Kanban board/swarm
→ 进度发布回 Room
→ 终态 synthesis 回 Room
```

Fork M4 的产品意图仍有价值，但其自建 DAG 和 in-process runner 不应移植。

### 9. 可选语义专家路由

官方已支持 mention、speaker rotation、pass/hold 和多轮讨论。Fork 额外支持在没有
mention 时根据 profile description 选择最合适的专家。

**正确方向：**作为显式启用的 Hosted Room discussion policy，不创建隐藏 observer
profile，也不新增永久 core model tool。

### 10. Session Librarian skill

Fork 的 `skills/productivity/session-librarian/SKILL.md` 支持搜索、总结、重命名、
archive、dry-run prune 和删除前 export。当前没有完全等价 skill。

如要引入，需按当前 skill authoring 标准重构、核对现行 CLI 参数并增加测试；不能原样
复制。Fork 的 plan skill 不算缺口，当前官方已有正式 `/plan`。

## 三、Dashboard / Web 独立缺口

这些项目不影响已完成的 profile runtime 闭环。

### 11. Profile MemoryPage 和 memory-file endpoints

Fork 提供独立 `MemoryPage.tsx` 以及：

```text
GET/PUT /api/profiles/{name}/memory/MEMORY.md
GET/PUT /api/profiles/{name}/memory/USER.md
```

当前 Dashboard 可编辑 `SOUL.md`，但不能独立查看和编辑 `MEMORY.md`、`USER.md`，
也没有 memory 文件/provider/state.db 统计页。

### 12. Profile binding summary

动态 binding 后端和 `/agent` 命令已经完成，但 Dashboard ProfilesPage 还不能显示
某 profile 绑定了哪些来源、群或 fallback webhook。

### 13. Weixin Dashboard QR 登录

Fork ChannelsPage 支持创建二维码、轮询扫码/确认/过期、刷新二维码，并把凭证保存到
目标 profile。当前只有 CLI setup 流程，Dashboard 没有等价入口。

### 14. Skill 删除 UI/API

Fork Dashboard 有 skill delete/uninstall；当前 SkillsPage 和 web router 没有明确的
删除按钮及 DELETE endpoint。

### 15. ConfigEditors / config labels

Fork 有专用配置编辑组件和大量中文标签说明。当前配置能力存在，但没有整体迁入这套
UX。由于官方 config schema 已持续变化，只能按当前 schema 重做，不能覆盖旧文件。

### 16. 当前页面的 i18n 补全

当前已有正式多语言体系，不能说“i18n 没合”。但部分新页面仍存在硬编码英文，因此
局部覆盖仍不完整。正确方式是审计当前页面，不是机械回放 Fork locale 文件。

### 17. Google Dashboard theme

Fork 的 Google 风格主题没有合入。它是完全独立的视觉增强。

### 18. Dashboard ErrorBoundary

Fork 有全局 `ErrorBoundary.tsx`，当前 Web Dashboard 没有明显等价的全局边界。
属于小型前端可靠性增强。

### 19. Hosted Room Web 管理/观察页

普通 Web Dashboard 尚缺 Hosted Room roster、event replay、authority/driver、peer grants、
replication health、Room/Kanban linkage 和 policy trace 的管理或只读 inspector。

不应直接移植 Fork `RoomsPage` 的完整 transcript/composer，因为当前 Dashboard 主聊天
复用 TUI，不能再造第二套 Web Chat。

## 四、已合入或已被官方覆盖

以下不需要重复移植：

- Signal self-mention、stop-typing、echo LRU/TTL、quote cache 四项；
- language-aware compression；
- Alibaba/DashScope rate-limit classifier；
- `/deny <reason>`；
- configurable tool-output limits；
- `read_file` char-budget truncation；
- subagent cost roll-up；
- DingTalk turn status card、AI Card、tool progress/commentary；
- DingTalk robot-native proactive fallback；
- DingTalk recent-image resend；
- dynamic source→profile binding 和 profile lifecycle/runtime 闭环；
- cron per-profile store/scheduler、by-name reference、`extra_prompt` 和 relay fail-closed；
- Dashboard password-only provider 的 SSO guard；
- per-profile terminal/provider scope；
- Docker task-id path sanitization（当前由共享 `path_utils` 实现）；
- Docker shared-key digest 防碰撞和 secret argv hardening；
- keyless Web fallback；
- API Server multi-profile 文档；
- SessionsPage 内建 FTS search；
- Profile SOUL/model/skills/MCP 管理；
- profile 删除及 binding cleanup。

## 五、明确不应移植

### Fork Agent Room 旧主体

以下已被官方 Hosted Rooms、Bots 和 Kanban 取代：

- `gateway/agent_room_store.py`；
- `gateway/agent_room_messages_store.py`；
- `gateway/agent_room_projection.py`；
- `gateway/agent_room_bootstrapper.py`；
- `gateway/agent_room_inprocess_runner.py`；
- `gateway/agent_room_task_orchestrator.py`；
- `tools/room_router_tool.py`；
- `tools/room_decompose_tool.py`；
- `tools/room_fetch_context_tool.py`；
- Fork `/api/rooms` schema；
- Fork 完整 `RoomsPage` chat surface。

直接移植会建立第二套 room identity、authority、event/message history、runner、DAG 和
Web chat，并与 prompt caching、canonical sessions 和官方 hosted worker 冲突。

### 其他不应移植

- 旧 `gateway/profile_runtime.py`；
- 旧 `tools/terminal_env.py` overlay；
- Tavily in-tree backend；
- AgentProxy core 硬编码；
- third-party example dashboard / strike-freedom-cockpit；
- Fork skills 目录整体；
- 旧 locale 文件机械覆盖；
- plugin manifest 单语中文覆盖英文；
- 重复的 API Server 独立文档；
- Fork 旧 Docker 实现。

Fork tip 还保留 `gateway/kanban_delegate.py` 和 `/delegate`/`/swarm` 测试意图，
但生产端 `_handle_delegate_command` 已在合并过程中丢失，因此这是一组悬空
helper/tests，不应当成当前 Fork tip 的完整可用功能直接移植。

## 六、已知但此前明确排除或需先复现

### DingTalk Stream half-open watchdog

Fork 提交 `1ed6be071f` 当前没有合入，但 DingTalk Plan A 已明确
`exclude watchdog`，因此不是遗漏。

### Weixin session-expiry 等待时间

Fork 把接收循环等待从 10 分钟改为 1 分钟。当前官方发送路径已增加失效
`context_token` 移除后重试；旧常量改动不能直接重放，应先复现当前接收循环的具体
故障。

## 七、后续评估分组

### A. 独立、明确

1. Cron creating-chat IDOR 隔离；
2. Shutdown phantom `systemctl --user` 修复；
3. Turn log resolved profile。

### B. 按官方架构重新设计

1. AgentProxy provider 插件；
2. Hosted Room roster planner；
3. Messaging `/room`；
4. IM group ↔ Hosted Room；
5. Hosted Room ↔ Kanban ↔ synthesis；
6. 语义专家路由；
7. Session Librarian skill。

### C. Dashboard 独立增强

1. MemoryPage；
2. binding summary；
3. Weixin QR；
4. Skill delete；
5. ConfigEditors；
6. 当前页面 i18n 补全；
7. Google theme；
8. ErrorBoundary；
9. Hosted Room inspector。

## 最终判断

剩余真正影响安全或运行正确性的后端缺口很少；数量较多的是 Dashboard 管理入口，以及
Agent Room 在官方 Hosted Rooms/Kanban 架构上的产品桥接。后续不应以“批量移植 Fork
提交”为目标，而应逐项验证当前官方行为，再选择现行架构中的最窄落点。
