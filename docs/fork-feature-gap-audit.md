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

**已落地（`6823fbf315`）：** 后端新增 `GET/PUT /api/profiles/{name}/memory/{doc}`，
`{doc}` 用允许清单（`MEMORY.md`/`USER.md`）校验，杜绝穿越 `memories/`；`{name}` 走
`_resolve_profile_dir`（校验名字 + 存在性）。文件缺失返回
`{content:"", exists:false}`（沿用 SOUL.md 读取语义，编辑器可从空白起步），写入用
`atomic_write_text` 原子替换，避免中断的保存把文档截断成空。前端新增独立
`MemoryPage.tsx`，两个编辑器消费全局 profile scope，接入 lazy 路由 + `/memory` 导航
（Brain 图标）；`api.getProfileMemory`/`updateProfileMemory` 镜像 SOUL 方法。线上验证
登录 200；GET default MEMORY(空)/USER(44)、xcx MEMORY(1842)；PUT 含 unicode + 换行
无损往返并成功还原；非法 doc 在 GET/PUT 均 404。`tsc -b` 干净、构建
`MemoryPage-CL-pVAOh.js`、重启后资源 200。

### 12. Profile binding summary

动态 binding 后端和 `/agent` 命令已经完成，但 Dashboard ProfilesPage 还不能显示
某 profile 绑定了哪些来源、群或 fallback webhook。

**已落地（`dfe7224f99`）：** 后端新增只读 `GET /api/profiles/bindings`，按 profile
合并两类路由面——`static`（`gateway.profile_routes`，经 `load_gateway_config` 读到
已解析的 `ProfileRoute`）与 `dynamic`（`SourceAgentBindingStore` 运行时 `/agent use`
绑定，`source_binding_key` 用有界 split 解码成 platform/scope/chat_id，不破坏含 `:`
的 chat id）。绝不返回 fallback webhook 明文，只给 `has_fallback_webhook` 布尔。
前端 `api.getProfileBindings()` + 类型，ProfilesPage 以 best-effort overlay 加载
（失败不阻塞 profile 列表），每卡片渲染紧凑「Bound sources」段：平台 badge +
static/runtime 标签 + 等宽截断 chat id（完整值在 title），超出以「+N more」收起。
线上验证 jb(static=1,dynamic=2)/xcx(dynamic=21)/reverse(dynamic=1)，
`tsc -b` 干净、构建 `ProfilesPage-DSXl9cJI.js`、重启后资源 200。

### 13. Weixin Dashboard QR 登录

Fork ChannelsPage 支持创建二维码、轮询扫码/确认/过期、刷新二维码，并把凭证保存到
目标 profile。当前只有 CLI setup 流程，Dashboard 没有等价入口。

**已落地（`4f70f430e0`）：** 把 CLI `qr_login()`（打印 ASCII 二维码 + 阻塞轮询循环，
浏览器无法驱动）拆成两个 web 无关的 async 原语 `weixin_create_qr()` /
`weixin_poll_qr()`（各只做「取二维码」「单次轮询」，循环/过期刷新/redirect host 跟随/
持久化都由调用方负责；`qr_login()` 本身不变）。web_server 按 WhatsApp/Telegram
onboarding 同款模式加 start/status/apply/cancel 端点 + 后台轮询线程推进服务端会话
状态机（wait/scaned/redirect/expired/confirmed），过期时像 CLI 一样最多刷新 3 次。
**安全：** confirmed 的 `bot_token` 只存在会话记录的私有字段里、绝不序列化——浏览器
只看到可扫二维码串、粗粒度状态、确认后的 account_id。apply 用 `_config_profile_scope`
把 `WEIXIN_*` 写进目标 profile 的 .env + 账号凭证文件、启用平台并重启网关；start 会
拦截「在多路复用的从属 profile 上启用端口绑定平台」。平台 payload 加
`weixin_setup{account_set}`（绝不含 id/token）。前端 ChannelsPage 加
`WeixinOnboardingPanel`（镜像 WhatsApp 面板）、api client 方法 + 类型，并把
`/weixin/onboarding` 加进 `PROFILE_SCOPED_PREFIXES` 自动带 `?profile=`。对真实 iLink
线上验证：create QR（32 位 token + 可扫 URL）、poll→wait；端点往返
start(200，QR 为 URL，不泄漏 token)/status(键集安全)/未确认 apply(409)/cancel(200)/
cancel 后 status(404)；profile-scoped start；`weixin_setup` 正确暴露 account_set。
`tsc -b` 干净、构建 `ChannelsPage-C2Dx-cHJ.js`、重启后 200，既有 30 个 weixin 单测通过。

### 14. Skill 删除 UI/API

Fork Dashboard 有 skill delete/uninstall。**核验（`24dff85fd8` 之后）：后端与
API client 层已就位**——`hermes_cli/web_routers/skills.py` 有
`POST /api/skills/hub/uninstall`，`web/src/lib/api.ts:1308` 有
`uninstallSkillFromHub(name, profile?)` 封装。**唯一缺口是前端**：
`web/src/pages/SkillsPage.tsx` 尚未把这个已有 client 方法接到一个删除/卸载
按钮上（全文件无 uninstall 调用）。因此这不再是"没有 endpoint"，而是一个
很窄的前端收尾——加一个按钮调用现成的 `uninstallSkillFromHub` 即可。

**已落地（`87c55a7b92`）：** SkillsPage 每个 SkillRow 加了 Trash2 卸载按钮（镜像
Edit 按钮的 hover-reveal），走共享 `useConfirmDelete` + `DeleteConfirmDialog` 的
异步守卫流程：先乐观删行，再用一次新的 `getSkills` 对账，好让 CLI 卸载后仍存活的
内置/在用 skill 重新出现。写入 scope 跟随侧栏 profile 切换器（`selectedProfile`），
与本页其他写入一致。后端 `POST /api/skills/hub/uninstall` 与
`api.uninstallSkillFromHub` 早已就位，本次只补齐前端按钮。

### 15. ConfigEditors / config labels

Fork 有专用配置编辑组件和大量中文标签说明。**当前 `web/src/pages/ConfigPage.tsx`
（679 行）已经是 schema 驱动的分类编辑器**：支持分类导航、字段搜索、YAML
原文模式、per-profile scope、reset-to-default。它覆盖了 Fork ConfigEditors 的
大部分意图，因此这一项不再是"整套 UX 缺失"，剩余的只是 Fork 那批中文字段标签/
说明尚未整体迁入。由于官方 config schema 已持续变化，只能按当前 schema 补标签，
不能覆盖旧文件。

**核验（`24dff85fd8` 之后）：** ConfigPage 存在且功能完整；仅字段标签本地化是增量。

**已落地（本次）：** 新增 `web/src/i18n/config-labels.ts` —— 一个按 config 点路径
（即 AutoField 收到的 `schemaKey`）索引的本地化覆盖层，`AutoField` 通过 `useI18n()`
读取当前 locale 后查表，命中则用 overlay 的 `label`/`description` 覆盖“从 key 反推的
英文标签 + 后端 schema.description”，未命中则原样回退。首批填了后端 schema 里真正带
手写用户可见说明的那批字段（model / timezone / terminal.* / browser.headed /
display.* / proxy.* / updates.* / plugins.hook_callback_timeout 等 25 项）的简体中文。
因为按 key 查表且只做覆盖，schema 增删字段时不会“覆盖旧文件”——失配的 key 直接走英文
回退。另外把 `ConfigPage.prettyCategoryName` 的兜底从“仅首字母大写”改为下划线→空格
Title Case，让尚未翻译的新分类（gateway / kanban / wake_word /
tool_loop_guardrails …）显示为 “Wake Word” 而非 “Wake_word”，对所有 locale 生效。
786 字段 schema 端点保持 200；`npx tsc -b` 干净、`npm run build` 通过、dashboard 重启
正常。后续 locale 只需往 overlay 里加对应语言块即可增量扩展。

### 16. 当前页面的 i18n 补全

当前已有正式多语言体系，不能说“i18n 没合”。但部分新页面仍存在硬编码英文，因此
局部覆盖仍不完整。正确方式是审计当前页面，不是机械回放 Fork locale 文件。

### 17. Google Dashboard theme

Fork 的 Google 风格主题没有合入。它是完全独立的视觉增强。

### 18. Dashboard ErrorBoundary

Fork 有全局 `ErrorBoundary.tsx`，当前 Web Dashboard 没有明显等价的全局边界。
属于小型前端可靠性增强。

**已落地（`94668ea0f0`）：** 新增 `web/src/components/ErrorBoundary.tsx`（自包含
class 组件，不依赖 i18n hook/数据获取/app context——因为这些正是崩溃时可能已失效
的东西），在 `App.tsx` 里包住 routed `<Routes>` 子树。单页渲染/生命周期异常或
chunk 加载失败不再整站白屏；`resetKeys={[pathname]}` 在导航时自动清除错误，用户
可切到正常页面而非卡在 fallback。文案默认英文字面量，App 传入 `t.common.retry`/
`t.common.refresh` 本地化。已构建（`index-BY8T2e6I.js`）、重启、JS 200、auth 回归干净。

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

### A. 独立、明确 — 已全部落地并线上验证

1. Cron creating-chat IDOR 隔离 — `213e669e30`（choke-point `_caller_may_touch_job`），
   已用部署后的 `cronjob()` 工具跑通 13 项功能 + 8 项状态级真实用例；
2. Shutdown phantom `systemctl --user` 修复 — `cf40701a42`，真实 systemd A/B 验证
   （phantom 90s 被拒、真实 210s 采用）；
3. Turn log resolved profile — `a9f0eb352a`，真实 `/p/xcx/` API 用例验证日志
   `profile=xcx`。

**A 组衍生修复**：在验证 (1) 时发现 preflight escape hatch 只看静态
`profile_routes`，对动态 `source_agent_bindings.sqlite`（运行时 `/agent use`）绑定的
satellite profile 视而不见，导致 xcx 的 dingtalk cron 任务全部被
`blocked_config`（"no gateway credentials configured (not connected)"）永久拦在 LLM
调用之前。修复 `db16a085b0`（`cron/scheduler.py::_delivery_platform_routed_from_primary_gateway`
同时查询动态绑定 store）。线上真实自然 cron tick 验证：job `3a383d1cde67`
（`*/10 * * * *`）在修复前每次 ~80ms `failed/blocked_config`、无 LLM 调用；部署后首个
tick（11:50:39，PID 1933784）运行完整 ~2m45s LLM turn 并 `completed`、成功投递。

**A 组第二个衍生修复**：preflight 打通后，satellite（xcx）的 dingtalk cron
任务能跑完 LLM turn，但**投递仍被静默跳过**，Dashboard 上每个任务显示
`delivery: platform 'dingtalk' not configured/enabled`。根因是两道闸门：

1. multiplex ticker 只把 default 的共享 adapter 交给 default，secondary
   profile 用 `profile_adapters[name]`（只含它自己连上的 bot），dingtalk 的
   凭证只在 default 上（satellite 再配一份是 `duplicate_credential` fatal），
   所以 xcx 的 tick adapter map 里根本没有 dingtalk。
2. 即便借到 adapter，`_deliver_result` 里 `resolve_delivery_transport` 仍会因
   xcx 自己的 dingtalk 配置 `enabled=False` 而返回 None，触发
   `not configured/enabled` 跳过；且 live-send 路径会重建
   `DeliveryRouter(config, adapters)` 再解析一次，同样失败并回退到无凭证的
   standalone 路径（`No adapter configured for dingtalk`）。

修复分两半（对称于既有 relay 逃生舱、复用 preflight 的
`_delivery_platform_routed_from_primary_gateway` 授权判定）：

- `cron/scheduler_provider.py`：`_bound_platforms_for_profile` +
  `_augment_secondary_adapters_from_shared` — 仅当该 profile 有该平台的动态
  source binding、且自己没有该平台 adapter 时，才把共享 adapter 借入本次 tick
  的 map（无绑定证据不借，前向兼容多租户自带 bot：一旦租户自配 adapter 即短路
  不借，杜绝错 bot 投递）。
- `cron/scheduler.py::_deliver_result`：satellite-borrow 逃生舱——当共享 adapter
  在 map 中、且 primary 把该平台路由到本 profile（静态 route 或动态 binding），
  在**本次调用的** config 副本上把该平台置 `enabled=True` 并重解析 transport，
  使随后重建的 `DeliveryRouter` 也解析到同一借用 adapter；未路由/无 live
  adapter 时保持 fail-closed。

线上真实自然 cron tick 验证（PID 2020755，16:40）：job `3a383d1cde67`
（`deliver=dingtalk:cidUKHyy...`）修复前每 tick 被
`not configured/enabled` 跳过；部署后 16:40:19 跑完 ~2m30s LLM turn，
`last_delivery_error=None`，借用的共享 dingtalk adapter 于 16:42:47 对目标群
实际发起 robot-native proactive send（此前从未到达 adapter）。整批 `not
configured/enabled` 与 `No adapter configured` 归零。

**A 组第三、第四个衍生修复（DingTalk 顶层 platform key 未桥接进 `extra`）**：

追查"每次回复两次"与持续刷屏的 AI Card `500 未知错误`（此前一度误判为钉钉服务端
既有告警——**该判断已被本次调查证伪**）时，定位到一个系统性根因：`gateway/config.py`
的 `bridged` 白名单（顶层 `platforms.<p>` key → `PlatformConfig.extra`）只覆盖
`require_mention` 等少数 key，**不含 DingTalk adapter 实际从 `extra` 读取的多数 key**
（`allow_all_users`、`card_template_id`、`card_content_key`、`app_code`、`corp_id`、
`agent_id`、`reply_at_sender`、`allowed_users`、`allowed_chats`、`free_response_chats`）。
这些 key 只配在顶层 `dingtalk:` 块时进不了 `extra`；插件 `_apply_yaml_config` 钩子
虽把其中部分映射进 `os.environ`，但 multiplex + installed secret-scope 下鉴权/渲染
路径读 scope 与 `extra`、不读 `os.environ`（#72348 隔离），于是这些 key **静默失效**。

两个具体故障：

1. **配对（allow_all_users 失效）**：GM 等陌生 DM 路由到 xcx 后 `extra.allow_all_users`
   为 None → 鉴权判定未授权 → 弹配对码。修复：把 `allow_all_users` 移入
   `platforms.dingtalk.extra`，经 `_platform_config_allow_all_users`
   （`gateway/authz_mixin.py:974`，读 `extra` 非 `os.environ`）生效。
2. **卡片双发 + 500**：`card_template_id` 只读 `extra`（无 `load_config_readonly`
   兜底）→ 读到空 → `_card_template_id` 掉到内置默认模板 `382e4302`
   （其字段名 `msgContent`），而 `card_content_key` 经 fallback 读到 `content`。
   默认模板 `382e4302` 配错配的 key `content` → 三步卡片流程的第 3 步
   `streaming_update` 往默认模板不存在的 `content` 字段写内容 → 钉钉服务端返回
   `500 未知错误`（create/deliver 已成功，卡片已投递可见，仅第 3 步炸）；失败后
   降级另发 webhook/robot-native → 用户看到【空/半截卡片】+【文本】=**两条回复**。
   修复：把 `card_template_id` + `card_content_key` 移入
   `platforms.dingtalk.extra`，使 `_card_uses_default_template=False`、初始
   param_map 走 `{content:""}` 正确分支、streaming key 与模板匹配。

线上验证（default + xcx 两 scope；网关重启 PID 2058406，18:42:55）：
- 两 scope `extra.allow_all_users==True`；真实 DM `msg='你好'` 直接路由 xcx 进对话
  循环、**不再弹配对码**（18:23~ 起 0 条 `unauthorized`/`pairing code`）。
- 两 scope `extra.card_template_id=='26e55230...'`、`_card_uses_default_template=False`；
  启动日志 `Card SDK initialized with template: 26e55230`；重启后带时间戳的
  AI Card 500 计数 **0**，18:43:03 出现今日 01:30 以来**首次** `AI Card
  created+finalized` 成功，双发消失。

当前修复在**线上 config**（`/root/.hermes/config.yaml` + `profiles/xcx/config.yaml`，
非 git 追踪）。**待落地的代码级根因修复**：把上述 DingTalk key（凭据 `client_id`/
`client_secret`/`robot_code` 除外——它们另走 secret scope + `os.getenv` 兜底）加入
`gateway/config.py` 的 `bridged` 白名单，使顶层 `dingtalk:` 块的配置对新装用户也自动
进入 `extra`，避免再踩同类断链。`allow_all_users` 为多平台通用 key，可在共享 loop
中无条件桥接；card 相关 key 为 dingtalk 专属，按 `plat == Platform.DINGTALK` 条件桥接。

### B. 按官方架构重新设计

1. AgentProxy provider 插件；
2. Hosted Room roster planner；
3. Messaging `/room`；
4. IM group ↔ Hosted Room；
5. Hosted Room ↔ Kanban ↔ synthesis；
6. 语义专家路由；
7. Session Librarian skill。

### C. Dashboard 独立增强

1. MemoryPage — 已落地 `6823fbf315`；
2. binding summary — 已落地 `dfe7224f99`；
3. Weixin QR — 已落地 `4f70f430e0`；
4. Skill delete — 已落地 `87c55a7b92`；
5. ConfigEditors；
6. 当前页面 i18n 补全；
7. Google theme；
8. ErrorBoundary — 已落地 `94668ea0f0`；
9. Hosted Room inspector。

## 最终判断

剩余真正影响安全或运行正确性的后端缺口很少；数量较多的是 Dashboard 管理入口，以及
Agent Room 在官方 Hosted Rooms/Kanban 架构上的产品桥接。后续不应以“批量移植 Fork
提交”为目标，而应逐项验证当前官方行为，再选择现行架构中的最窄落点。
