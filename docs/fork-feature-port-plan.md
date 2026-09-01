# Fork 特性移植方案（排除 Agent Room）

> 基线：官方最新 HEAD \`3783fd9f\`
> 来源：个人仓库 fork \`mine/main\`（merge-base \`b1ff8722\`，2026-08-29）
> 目标分支：\`integration/port-fork-features\`
> 原则：以官方最新为基线，把 fork 的定制**逐特性移植**过来。**完全排除 Agent Room / hosted_room 房间体系**。
> 已剔除（官方已独立覆盖，无需移植）：Signal 四项补丁、read_file 字节截断、subagent 成本上卷。

---

## 阶段 0 · 建立集成分支

**目标**：在官方最新基线上开一条干净的集成分支。
**要做什么**：
\`\`\`bash
git fetch --all
git checkout -b integration/port-fork-features 3783fd9f
\`\`\`
**实现效果**：拿到官方全部 559 提交的稳定性成果，作为移植底座。
**验收**：
- \`git log -1 --oneline\` = 官方 HEAD。
- \`git status\` 干净。
- \`source .venv/bin/activate && python -c "import gateway.run"\` 无报错（基线可导入）。

---

## 阶段 1 · 直接搬入独立文件（零冲突）

这些文件官方 HEAD **完全没有**，直接从 fork checkout 过来即可，冲突为零。搬入后接线在阶段 3/6。

### 1.1 turn_status_card.py（通用进度卡协调器）
**要合并什么**：\`gateway/turn_status_card.py\`（829 行，纯表现层）。
**实现什么效果**：一次 agent turn 里，把原本刷屏几十条的工具调用消息，**合并成一张可持续编辑的"进度卡"**——含渐长圆点 spinner、每个工具的实时耗时计数、工具 emoji、完成/失败标签（✅/❌）、结尾"N 工具 · M 失败 · Xs · 答案见下方"汇总。不改 agent 历史/prompt/schema，**不破坏 prompt 缓存**。
**命令**：\`git checkout mine/main -- gateway/turn_status_card.py\`
**验收**：
- \`git checkout mine/main -- tests/gateway/test_turn_status_card.py\`
- \`scripts/run_tests.sh tests/gateway/test_turn_status_card.py -q\` 全绿。

### 1.2 terminal_env.py（multiplex 下终端后端 per-profile 隔离）
**要合并什么**：\`tools/terminal_env.py\`（103 行，ContextVar overlay）。
**实现什么效果**：一个网关进程服务多个 profile 时，terminal 后端选择（docker / agentproxy / local）**按 profile 隔离**，不再被"谁先碰谁锁定全进程"互抢。单 profile 场景为 no-op。
**命令**：\`git checkout mine/main -- tools/terminal_env.py\`
**验收**：见阶段 3.4（需配合 ~18 处读取点改写后整体验收）。

### 1.3 agentproxy.py（远程 Docker 执行后端）
**要合并什么**：\`tools/environments/agentproxy.py\`（276 行）。
**实现什么效果**：新增 \`TERMINAL_ENV=agentproxy\`，让 agent 在远端 AgentProxy 的 Docker 容器里执行命令，纯走 Dashboard task API（POST /api/tasks/run + SSE），无需 SSH；用 \`__HERMES_AP_EC__\` sentinel 恢复真实退出码。
**命令**：\`git checkout mine/main -- tools/environments/agentproxy.py\`
**验收**：
- \`python -c "import tools.environments.agentproxy"\` 无报错。
- 阶段 2.4 补 check_terminal_requirements 分支后，\`hermes tools\` 里 agentproxy 不再报 "Unknown TERMINAL_ENV"。

### 1.4 Dashboard 独有页面/组件/词典
**要合并什么**：
- \`web/src/pages/MemoryPage.tsx\`（217 行）
- \`web/src/lib/configLabels.ts\`（1036 行）
- \`web/src/components/SessionSearchModal.tsx\`
- \`web/src/components/ConfigEditors.tsx\`
**实现什么效果**：
- MemoryPage：在 Dashboard 里**可视化查看/编辑** \`MEMORY.md\` / \`USER.md\` / \`SOUL.md\`。
- configLabels：把 config.yaml 的英文键名（agent/auxiliary/browser/compression…）**自动映射成中文标签**，供配置页渲染中文界面。
- SessionSearchModal：**Cmd+K 全局会话搜索**（方向键 + Enter 导航）。
- ConfigEditors：渠道级配置覆盖编辑器。
**命令**：
\`\`\`bash
git checkout mine/main -- web/src/pages/MemoryPage.tsx web/src/lib/configLabels.ts \
  web/src/components/SessionSearchModal.tsx web/src/components/ConfigEditors.tsx
\`\`\`
**验收**：配合阶段 6（api.ts 端点 + 路由挂载 + 翻译键）后统一验收：\`cd web && npm run build\` 通过，Dashboard 打开 Memory 页可读写、Cmd+K 可搜索、配置页显示中文标签。

### 1.5 source_agent_binding.py（source→profile 绑定 store，纯净）
**要合并什么**：\`gateway/source_agent_binding.py\`（263 行）。
**实现什么效果**：提供一张 SQLite 绑定表，把某个消息来源（群/DM/用户）绑定到某个 profile。这是阶段 4 多租户能力的数据基础。**本体零 room 依赖**（已核实：只 import 标准库 + hermes_constants）。
**命令**：\`git checkout mine/main -- gateway/source_agent_binding.py\`
**验收**：
- \`git checkout mine/main -- tests/gateway/test_source_agent_binding.py\`
- \`scripts/run_tests.sh tests/gateway/test_source_agent_binding.py -q\` 全绿。

---

## 阶段 2 · 单函数小改（官方仍无，手动补）

### 2.1 systemd 幻影 \`--user\` 修复
**要合并什么**：\`check_systemd_timing_alignment\` 加 \`--property=LoadState\` 守卫，跳过 \`LoadState != loaded\` 的 manager。
**实现什么效果**：不再因不拥有该单元的 user manager 返回的幻影 90s 值，误报 "Stale systemd unit detected"。
**验收**：\`hermes doctor\`（或对应 gateway 启动检查）在 systemd 系统上不再误报；单测（若 fork 有）随之通过。

### 2.2 docker task_id 路径消毒
**要合并什么**：\`tools/environments/docker.py\` 加 \`_RE_UNSAFE_PATH_SEG\`，构造 bind-mount 源路径时 \`:\` 等 → \`_\`。
**实现什么效果**：\`backend=docker + container_persistent:true\` 时不再因 \`session:<key>\` 里的冒号撞 docker \`-v\` 分隔符导致每次 exit 125。
**验收**：docker persistent 后端下容器能正常启动、命令有输出（本地有 docker 环境时手测）；不引入回归：\`scripts/run_tests.sh tests/tools/test_terminal_task_cwd.py -q\`。

### 2.3 微信过期暂停缩短
**要合并什么**：\`gateway/platforms/weixin.py\` \`asyncio.sleep(600)\` → \`sleep(60)\`。
**实现什么效果**：微信 session 过期后 1 分钟即恢复轮询（原 10 分钟），停摆窗口缩短。
**验收**：\`grep 'pausing for 1 minute' gateway/platforms/weixin.py\` 命中。

### 2.4 agentproxy 注册补分支
**要合并什么**：\`check_terminal_requirements()\` 补 agentproxy 分支（此前落 else 报 Unknown）。
**验收**：见 1.3。

---

## 阶段 3 · run.py 集成接线（在官方新 run.py 中手动插入，排除所有 room 分支）

> 官方 run.py 已大改（+1990/-634 相对旧基线）。以下集成点需**手动在官方新结构里插入**，且**只插 profile 路由分支，绝不插 \`_process_message_via_room_if_bound\` 等房间分支**。

### 3.1 binding profile 路由（single source of truth）
**要合并什么**：\`_binding_profile_for_source(source)\`（run.py ~29095），接入 \`_profile_name_for_source\`。**只保留 per-user → chat-level 两级 profile 查找，不读 fallback_extra['room_id']**。
**实现什么效果**：一个网关把不同群/DM 绑到不同 profile 时，会话历史落到**正确的 profile 命名空间**（\`agent:<profile>:\`），不再串库。
**验收**：
- \`git checkout mine/main -- tests/gateway/test_binding_profile_stamping.py tests/gateway/test_agent_command.py\`
- \`scripts/run_tests.sh tests/gateway/test_binding_profile_stamping.py tests/gateway/test_agent_command.py -q\` 全绿。

### 3.2 默认 handler 全程跟随绑定 profile
**要合并什么**：进入 scope 前用 \`_resolve_profile_home_for_source\` 按 event 解析 profile home，让整个 turn（含 session 记账）在同一个正确 scope 下。
**实现什么效果**：动态绑定到非默认 profile 的 chat，session 记账与 turn 数据落同一库，message_count 正常前进，**prompt caching 不被每 turn 重建摧毁**。
**验收**：
- \`git checkout mine/main -- tests/gateway/test_profile_runtime_context.py tests/integration/test_multi_agent_profiles_interface_flow.py\`
- \`scripts/run_tests.sh tests/gateway/test_profile_runtime_context.py -q\` 全绿。

### 3.3 turn_status_card tool 事件路由 + 图片重发
**要合并什么**：
- tool_progress_callback 挂载条件改为 \`needs_progress_queue OR _turn_status_card_enabled\`（run.py ~30751/30986）。
- 钉钉图片重发：\`_wants_recent_image_resend\`（~4195）+ \`_maybe_resend_recent_image\`（~24870），\`Platform.DINGTALK\` 门控。
**实现什么效果**：卡片启用时 tool.started/completed 不再被静默丢弃（原来 \`tools=0\`）；"再发一遍刚才那张图" 在钉钉可用。
**验收**：
- \`git checkout mine/main -- tests/gateway/test_recent_image_resend.py\`
- \`scripts/run_tests.sh tests/gateway/test_recent_image_resend.py tests/gateway/test_turn_status_card.py -q\` 全绿。

### 3.4 terminal_env overlay 读取点改写（~18 模块）
**要合并什么**：\`terminal_tool.py\`（34 处）+ 其他 ~12 模块（18 处）的 \`os.getenv("TERMINAL_*")\` → \`terminal_env_get(...)\`；\`_profile_runtime_scope\` 在 scope 内安装 overlay。
**实现什么效果**：完成 1.2 的 per-profile 终端隔离闭环。
**验收**：
- \`scripts/run_tests.sh tests/tools/test_terminal_task_cwd.py tests/gateway/test_64674_multiplex_primary_token_scope.py -q\` 全绿；
- 单 profile CLI 跑 terminal 工具行为不变（no-op 验证）。

---

## 阶段 4 · cron 多-profile 多租户套装（依赖阶段 1.5 binding）

> ⚠️ 官方 cron 已重构为 task-scoped workdir（取代旧全局锁+顺序池）。以下 fork 改动需**逐个 rebase 到官方新 cron 结构**，不能直接照搬旧实现。

### 4.1 cron IDOR 作用域收窄（安全）
**要合并什么**：\`_caller_may_touch_job()\` + \`_CRON_ORIGIN_SCOPED_PLATFORMS\`（dingtalk/weixin/slack/telegram…）。
**实现什么效果**：多租户群里，成员只能 list/操作 \`platform+chat_id\` 匹配自己的 cron 任务；跨群 mutation 返回 "not found"（不泄露任务存在）。CLI/TUI/api_server 保留全库访问。**堵住跨群枚举/删改的 IDOR 漏洞**。
**验收**：
- \`git checkout mine/main -- tests/cron/test_cron_profile.py tests/cron/test_relay_delivery_fail_closed.py tests/tools/test_cronjob_stub_ref_resolution.py\`
- \`scripts/run_tests.sh tests/cron/ tests/tools/test_cronjob_stub_ref_resolution.py -q\` 全绿。

### 4.2 create_job / cronjob profile 参数
**要合并什么**：两函数补 \`profile\` 参数，\`create_job\` 存 \`owner_profile/run_profile\`。
**实现什么效果**：非默认 profile 下经 Dashboard/工具建/改 cron 不再 TypeError。
**验收**：并入 4.1 的 \`tests/cron/test_cron_profile.py\`。

### 4.3 cron 投递按 owner profile 作用域
**要合并什么**：\`_deliver_result\` 在 owner profile 的 home override 下 \`load_gateway_config()\`。
**实现什么效果**：多网关服务器上，默认网关跑他人 profile 的 cron 时，dingtalk/weixin 投递不再报 "platform not configured"。
**验收**：并入 4.1 的 \`tests/cron/test_relay_delivery_fail_closed.py\`。

### 4.4 删 profile 清 per-profile cron + 按名引用
**要合并什么**：\`_delete_cron_jobs_for_profile\` 走 \`_call_cron_for_profile\`；stub 改调 \`resolve_job_ref\`（ID + 名字 fallback + 歧义 raise）。
**实现什么效果**：删非默认 profile 时正确注销其 cron；用人类可读名引用任务时得到 "ambiguous, use ID" 而非静默 not-found。
**验收**：并入 4.1 的 \`tests/tools/test_cronjob_stub_ref_resolution.py\`。

---

## 阶段 5 · 钉钉 adapter 三方合并（唯一硬骨头）

> 官方 \`plugins/platforms/dingtalk/adapter.py\` = 1932 行；fork = 4015 行。**以官方为基线，手动叠加以下 4 项 + 中文标签层**。不整文件覆盖。

### 5.1 断线 watchdog（半开连接恢复）
**要合并什么**：应用层 ping/pong watchdog（\`STREAM_PING_INTERVAL=60\` / \`STREAM_PING_TIMEOUT=20\`，可 env 覆盖），超时强关 socket 触发重连；\`disconnect()\` 先取消 watchdog。
**实现什么效果**：钉钉 Stream Mode 半开连接（TCP 在、业务流不来，曾静默 4.5 天）能被主动检测并重连；健康连接不误杀。
**验收**：\`scripts/run_tests.sh tests/gateway/test_dingtalk.py -q\` 全绿（官方该测试文件 + fork 追加用例）。

### 5.2 webhook 过期回退（robot-native 主动发送）
**要合并什么**：\`_send_markdown_proactive\` → \`_send_robot_native_message\`（OrgGroupSend/PrivateChatSend，用 app access token）。
**实现什么效果**：\`session_webhook\` 过期/机器人被移出群时，回复不再丢失，改用 robot-native API 投递；5xx 视瞬时不重试死 webhook。
**验收**：并入 \`tests/gateway/test_dingtalk.py\`。

### 5.3 审批卡片（send_exec_approval，中英双语）
**要合并什么**：\`send_exec_approval()\` 用 AI Card 渲染中英双语审批卡，列出可回复关键词（approve / 👍 / approve session / approve always / deny）。
**实现什么效果**：钉钉里危险命令审批显示为规整卡片而非纯英文文本；用户文字回复由既有解析器处理。
**验收**：并入 \`tests/gateway/test_dingtalk.py\`。

### 5.4 中文标签层
**要合并什么**：\`gateway/platforms/dingtalk.py\` 的 \`_TOOL_STAGE_LABELS\` / \`_TERMINAL_STAGE_LABELS\`（git→🌳提交代码中、pytest→🧪跑测试中、curl→📡请求接口中）+ REACTION 口语化中文；\`allow_all_users → DINGTALK_ALLOW_ALL_USERS\` 桥接。
**实现什么效果**：进度卡/reaction 显示口语化中文阶段，终端命令按前缀细分。
**验收**：
- \`git checkout mine/main -- tests/gateway/test_allow_all_users_config_fallback.py\`
- \`scripts/run_tests.sh tests/gateway/test_allow_all_users_config_fallback.py -q\` 全绿。

---

## 阶段 6 · Dashboard 汉化收尾（配合阶段 1.4）

### 6.1 api.ts 补端点
**要合并什么**：\`web/src/lib/api.ts\` 补 \`getProfileSoul\` / \`updateProfileSoul\` / \`updateProfileMemoryFile\`（及后端 \`hermes_cli/web_routers/profiles.py\` / \`web_server.py\` 对应路由）。
**实现什么效果**：MemoryPage 能读写 SOUL/MEMORY/USER 文件。
**验收**：
- \`git checkout mine/main -- tests/hermes_cli/test_web_server_profile_dashboard.py\`
- \`scripts/run_tests.sh tests/hermes_cli/test_web_server_profile_dashboard.py -q\` 全绿。

### 6.2 翻译键补差
**要合并什么**：官方 zh.ts 599 键 vs fork 1282 键，**并入多出的 ~683 键**（i18n 框架官方已有，无需重建）；其他语言文件同理补差。
**实现什么效果**：新页面/新功能的中文文案齐全，无英文占位泄漏。
**验收**：\`cd web && npm run build\` 通过（严格类型：Translations 接口不缺键）。

### 6.3 路由与挂载
**要合并什么**：\`web/src/App.tsx\` 挂 MemoryPage 路由；全局挂 SessionSearchModal（Cmd+K）。
**验收**：Dashboard 启动后，侧栏有 Memory 入口、Cmd+K 弹搜索框、配置页中文标签。

---

## 阶段 7 · 全量验证与收尾

**要做什么**：
\`\`\`bash
source .venv/bin/activate
# 后端：本次改动涉及的测试域全绿
scripts/run_tests.sh tests/gateway/ tests/cron/ tests/tools/ tests/hermes_cli/ tests/integration/ -q
# 前端
source ~/.nvm/nvm.sh && nvm use 24.11.1
cd web && npm run build && cd ..
git restore --worktree package-lock.json   # 构建会改动，还原
# Dashboard 冒烟
hermes dashboard --skip-build --no-open --host 127.0.0.1 --port 9119   # 以 DSH 后台任务方式启动
\`\`\`
**验收总清单**：
1. 上述所有 fork 独有测试文件全部 checkout 并通过。
2. 官方原有测试无回归（差异域内 \`tests/\` 全绿）。
3. \`web\` 构建通过，Dashboard 可打开，Memory/搜索/中文标签/钉钉进度卡（若可连）均按预期。
4. 单 profile CLI/TUI 行为与官方一致（隔离改动为 no-op）。
5. 全程 **未引入任何 agent_room / RoomsPage / /room / /api/rooms** 代码（\`git grep -i agent_room\` 仅命中被显式排除的注释/无命中）。

---

## 附：功能→验收测试 映射总表

| 阶段 | 功能 | 验收测试（fork 独有） |
|---|---|---|
| 1.1/3.3 | turn_status_card 进度卡 | test_turn_status_card.py |
| 1.5/3.1 | source binding / profile 路由 | test_source_agent_binding.py, test_binding_profile_stamping.py, test_agent_command.py |
| 3.2 | profile home 跟随 | test_profile_runtime_context.py, test_multi_agent_profiles_interface_flow.py |
| 3.3 | 钉钉图片重发 | test_recent_image_resend.py |
| 4.x | cron 多租户/IDOR/名引用 | test_cron_profile.py, test_relay_delivery_fail_closed.py, test_cronjob_stub_ref_resolution.py |
| 5.x | 钉钉 adapter 4项 + allow_all | test_dingtalk.py, test_allow_all_users_config_fallback.py |
| 6.1 | Dashboard Memory 端点 | test_web_server_profile_dashboard.py |

## 附：明确排除清单（不移植）
- 全部 \`gateway/agent_room_*\`（17 文件）、\`tools/room_router_tool.py\`、\`tools/room_fetch_context_tool.py\`、\`tools/room_decompose_tool.py\`
- \`web/src/pages/RoomsPage.tsx\`、\`toolsets.py::room_observer\`
- run.py 的 \`_process_message_via_room_if_bound\` / \`_get_room_for_source\` 及一切读 \`fallback_extra['room_id']\` 的路径
- \`/room\` 命令、\`/api/rooms/*\` 端点、\`scripts/migrate_m1_to_m3.py\`
- 已被官方覆盖：Signal 四项、read_file 字节截断、subagent 成本上卷
