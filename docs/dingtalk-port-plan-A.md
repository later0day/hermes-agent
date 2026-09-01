# 钉钉移植 · 方案A 完整执行方案（排除断线 watchdog）

> 基线：官方 HEAD \`3783fd9f\`，集成分支 \`integration/port-fork-features\`
> 来源：\`mine/main\`，merge-base \`b1ff8722\`
> 关键事实：官方钉钉 adapter = merge-base，**一字未改**；你的 54 个新增函数全是纯叠加，0 删除。
> 本方案 = 完整移植（含 turn_status_card 实时进度卡），仅**排除断线 watchdog**。

---

## 已核实的前提（全部通过）

1. adapter.py 依赖的 12 个仓库模块（gateway.config / platforms.base / platforms.helpers / agent.secret_scope / _http_client_limits / lazy_deps / url_safety / send_message_tool / setup / cli_output / dingtalk_auth / config）**官方 HEAD 全部存在**。
2. \`SUPPORTS_TURN_STATUS_CARD\` / \`notify_tool_started\` 是 fork 在 adapter 内**自定义**的，不依赖官方基类 → 拿过来即用。
3. 官方 base.py 已有 \`edit_message\` / \`REQUIRES_EDIT_FINALIZE\`（消息编辑契约就绪）。
4. turn_status_card 在 run.py 的 45 处接线，收敛到 **7 个逻辑锚点**，全部锚点在官方 HEAD 存在（行号偏移但结构一致）。

---

## 要排除的 watchdog（整文件拿来后删掉这些）

| 位置(fork行) | 内容 |
|---|---|
| 153-165 | 注释 + \`STREAM_PING_INTERVAL\` / \`STREAM_PING_TIMEOUT\` 常量 |
| 375-381 | \`__init__\` 里 \`self._watchdog_task\` + 两个 ping 调优字段 |
| 513 | \`connect()\` 里 \`self._watchdog_task = asyncio.create_task(self._run_watchdog())\` |
| 545 起 | 整个 \`async def _run_watchdog()\` 方法 |
| disconnect() 内 | 取消 watchdog 的几行 |

> 排除后，webhook 过期回退（块5，与 watchdog 无关）**保留** —— 这是另一条可靠性路径，不受影响。

---

## 步骤 1 · 搬 turn_status_card 本体（独立文件，零冲突）

\`\`\`bash
git checkout mine/main -- gateway/turn_status_card.py
python -c "import gateway.turn_status_card"   # 验证可导入
\`\`\`
**效果**：拿到进度卡协调器（829 行，纯表现层，只依赖 adapter.send/edit_message 契约）。
**验收**：
\`\`\`bash
git checkout mine/main -- tests/gateway/test_turn_status_card.py
scripts/run_tests.sh tests/gateway/test_turn_status_card.py -q   # 全绿
\`\`\`

---

## 步骤 2 · 搬 adapter.py 本体 + 删 watchdog

\`\`\`bash
git checkout mine/main -- plugins/platforms/dingtalk/adapter.py plugins/platforms/dingtalk/plugin.yaml
# 然后手工删除上表 watchdog 5 处
python -c "import plugins.platforms.dingtalk.adapter"   # 验证可导入
\`\`\`
**覆盖的功能块**（含义见钉钉梳理）：
- 块1 能力声明（3函数）
- 块2 进度卡渲染方法（notify_tool_started 等 6 函数）— 方法就位，生效需步骤3接线
- 块3 emotion 情感标签（3函数）
- 块5 webhook 过期回退 + robot-native 发送（5函数）
- 块6 富媒体收发：图片/视频/语音/文档（~30函数）
- 块7 审批卡 send_exec_approval + 配置/setup（~6函数）

**验收**：
\`\`\`bash
git checkout mine/main -- tests/gateway/test_dingtalk.py tests/gateway/test_allow_all_users_config_fallback.py
scripts/run_tests.sh tests/gateway/test_dingtalk.py tests/gateway/test_allow_all_users_config_fallback.py -q
\`\`\`

---

## 步骤 3 · turn_status_card 接线到官方 run.py（唯一手工活）

> 官方 run.py 骨架与 fork 一致，按下列 7 个锚点**叠加卡片分支**（不是重写）。每处保留 fork 的 \`# fork:\` 注释以便日后辨识。

### 锚点 A — ctx 增加 turn_status_card_holder 字段
- **官方位置**：~30437（\`repeat_count=..., long_tool_hint_fired=...\` 构造 ctx 处）+ TurnRunnerContext 定义处。
- **插入**：\`turn_status_card_holder = [None]\`（progress_queue 旁），并作为字段传入 ctx（\`turn_status_card_holder=turn_status_card_holder\`）。

### 锚点 B — 卡片启用判断
- **官方位置**：~30308（\`_live_status_adapter = self._adapter_for_source(source)\` 之后）。
- **插入**：fork 的 \`_turn_status_adapter\` + \`_turn_status_card_enabled = bool(... SUPPORTS_TURN_STATUS_CARD ... and (tool_progress_enabled or _thinking_enabled or ...))\`。
- **同时**：\`needs_progress_queue\` 计算处（官方 30357）追加 \`and not _turn_status_card_enabled\`（卡片自己就是队列）。

### 锚点 C — callback arming 条件
- **官方位置**：~6180（\`ctx.needs_progress_queue or ctx.log_mode_enabled or ctx._live_status_adapter is not None\`）。
- **插入**：追加一项 \`or ctx.turn_status_card_holder[0] is not None  # fork: DingTalk status card\`。

### 锚点 D — progress_callback 工具事件路由
- **官方位置**：4695 起 \`def progress_callback\`，其 \`tool.started\` / \`tool.completed\` / \`subagent.complete\` 分支（官方 4705/4741/4748）。
- **插入**：开头 \`turn_status_card = ctx.turn_status_card_holder[0]\`；在各分支加 \`if turn_status_card is not None: turn_status_card.on_tool_progress(...)\` / \`notify_tool_started(...)\` / \`on_commentary(...)\`（对应 fork 4521-4705）。

### 锚点 E — 流式 delta/commentary 路由
- **官方位置**：5882-5925（\`_want_stream_deltas\` / \`_stream_delta_cb\` / GatewayStreamConsumer）。
- **插入**：fork 5754-5811 的 \`_turn_status_card.on_delta(text)\` / \`on_commentary(text)\` 分支（卡片启用时接管 delta，不走 GatewayStreamConsumer）。

### 锚点 F — 构造 + 启动协调器
- **官方位置**：进度 task 启动区（官方 30772 \`if needs_progress_queue:\` 附近）。
- **插入**：fork 30986-31003 \`if _turn_status_card_enabled: from gateway.turn_status_card import TurnStatusCardCoordinator; turn_status_card_holder[0] = TurnStatusCardCoordinator(adapter=..., chat_id=...)\`；以及 31202 \`turn_status_task = asyncio.create_task(turn_status_card_holder[0].run())\`。

### 锚点 G — 收尾 finish
- **官方位置**：turn 结束/cleanup 区。
- **插入**：fork 32208 \`_turn_status_card = turn_status_card_holder[0]; if ...: _turn_status_card.finish()\`。

**效果**：钉钉里一次 turn 只维护一张可编辑进度卡（spinner + 每工具实时耗时 + 中文阶段标签 git→🌳提交代码中 / pytest→🧪跑测试中 + ✅/❌ 完成标签 + 结尾"N工具·M失败·Xs·答案见下方"）。tool.started/completed 不再被静默丢弃。

**验收**：
\`\`\`bash
git checkout mine/main -- tests/gateway/test_run_heartbeat_expect_edits.py tests/gateway/test_recent_image_resend.py
scripts/run_tests.sh tests/gateway/test_turn_status_card.py \
  tests/gateway/test_run_heartbeat_expect_edits.py \
  tests/gateway/test_recent_image_resend.py -q
\`\`\`

---

## 步骤 4 · 图片重发（钉钉专属，run.py）

- **要合并什么**：\`_wants_recent_image_resend\`（fork 4195）+ \`_maybe_resend_recent_image\`（fork 24870）+ \`_RECENT_IMAGE_RESEND_*\` 哨兵，\`Platform.DINGTALK\` 门控。
- **效果**：用户说"再发一遍刚才那张图"时，钉钉重发最近附件图。
- **验收**：\`tests/gateway/test_recent_image_resend.py\`（步骤3已引入）全绿。

---

## 步骤 5 · 全量钉钉验收

\`\`\`bash
source .venv/bin/activate
scripts/run_tests.sh \
  tests/gateway/test_dingtalk.py \
  tests/gateway/test_turn_status_card.py \
  tests/gateway/test_run_heartbeat_expect_edits.py \
  tests/gateway/test_recent_image_resend.py \
  tests/gateway/test_allow_all_users_config_fallback.py -q
python -c "import plugins.platforms.dingtalk.adapter; import gateway.turn_status_card; import gateway.run"
\`\`\`
**通过标准**：
1. 上述 5 个测试文件全绿。
2. 三个关键模块可导入无错。
3. \`git grep -n _run_watchdog plugins/platforms/dingtalk/adapter.py\` **无输出**（watchdog 已排除）。
4. \`git grep -n agent_room gateway/run.py\` 无新增（未混入房间代码）。

---

## 功能→验收测试 映射

| 功能块 | 验收测试 |
|---|---|
| 富媒体收发 / webhook回退 / 审批卡 / emotion | test_dingtalk.py |
| turn_status_card 进度卡 | test_turn_status_card.py, test_run_heartbeat_expect_edits.py |
| 图片重发 | test_recent_image_resend.py |
| allow_all_users 桥接 | test_allow_all_users_config_fallback.py |

## 风险与回退
- **唯一风险点**：步骤3的 7 处手工接线（官方 run.py 行号偏移）。逐锚点插入后立即跑 test_turn_status_card 验证。
- **回退**：本方案全在 \`integration/port-fork-features\` 分支；任何步骤失败 \`git checkout -- <file>\` 或 \`git checkout main\` 秒回。

---

## ✅ 收尾定稿（最终状态）

**执行日期状态**：方案A 原范围全部完成并验收通过。

### 最终改动清单（vs 官方 HEAD `3783fd9f`，+5829 / -180，11 文件）

| 文件 | 增删 | 内容 |
|---|---|---|
| `gateway/turn_status_card.py` | +829 | 每轮可编辑进度卡协调器（新文件） |
| `plugins/platforms/dingtalk/adapter.py` | +2315 | 54 新函数：富媒体/webhook回退/审批卡/emotion/进度卡渲染；**watchdog 精确剔除、0 残留** |
| `gateway/run.py` | +327 | turn_status_card 7 处接线 + 图片重发（哨兵/正则/2辅助函数/方法/调用点） |
| `gateway/turn_context.py` | +4 | `turn_status_card_holder` 字段 |
| `gateway/platforms/base.py` | +3 | `MessageEvent.media_errors` 字段（富媒体依赖） |
| `gateway/authz_mixin.py` | +40 | `allow_all_users` config.yaml 回退 + `_platform_config_allow_all_users` helper |
| 4 个测试文件 | +2489 | test_dingtalk / test_turn_status_card / test_allow_all_users / test_recent_image_resend |

### 验收结果（全绿）
- 核心钉钉 4 文件：**124/124 通过**
- 广度回归（progress/stream/interim/authz/message 等 47 文件）：**482/482 通过**
- 全部模块 import OK
- 排除项零混入确认：watchdog=0、agent_room/hosted_room/source_agent_binding 新增行=0、Agent Room 注释=0

### 执行中发现的隐藏跨文件依赖（原计划未列，已补齐）
1. `base.py` 的 `MessageEvent.media_errors` 字段——adapter 富媒体路径需要。
2. `authz_mixin.py` 的 `allow_all_users` config 回退——adapter 鉴权需要。
3. 图片重发调用点：fork 原本调了一个 **fork 里也无定义的 `self._send_reply`**（等于该错误路径 fork 从未被测），改用官方原生 `_adapter_for_source().send(...)`，行为等价且有测试覆盖。

### 已知无关脏项
- `contributors/emails/agent@Agents-Mac-mini.local` 显示 dirty——官方仓库**固有**的大小写冲突文件对（`Agents` vs `agents`），macOS 大小写不敏感 FS 物理只能存一个，git 必然显示其一 dirty。**与本移植无关，提交时忽略。**

---

## 本次发现但**未移植**的 fork 钉钉相关项（供后续独立决策）

| 项 | 内容 | 依赖 / 评估 |
|---|---|---|
| **A. dashboard 配置页中文化 + 钉钉 12 项 label** | `configLabels.ts`（新建1036行）+ ConfigPage 渲染架构改造 + 15 语言 i18n 文件 | 与 Room **无关**，但属整套 dashboard 本地化大改造（1121行前端+i18n），**超出钉钉运行时移植范畴**，应作独立专项。官方 dashboard 已能连钉钉（AppKey/Secret），细粒度项也可经 config.yaml 生效，仅缺 web 中文卡片。 |
| **B. 删 profile 时通知钉钉群** | `web_routers/profiles.py` 删除路由 + `_send_dingtalk_session_webhook` | 逻辑本身纯钉钉（**与 Room 无关**），但依赖 fork 新增的 `SourceAgentBindingStore` 存储层——须连带搬 C。 |
| **C. source_agent_binding + `/agent use` 群内绑定 profile** | `gateway/source_agent_binding.py`（fork新建）+ slash_commands + run.py 绑定路由 | **官方基线完全没有这整套**（非"官方已有基础、fork加Room层"）。fork 里与 `agent_room_store.py` 同源交织。大功能簇，需单独评估耦合深度后决策。 |

> 更正记录：早前方案笔记写 "KEEP `_binding_profile_for_source`, EXCLUDE Room" 的前提有误——官方**整套 source_agent_binding 都不存在**，是 fork 新增。B/C 的"能否与 Room 干净切开"需重新核 `source_agent_binding.py` 与 `agent_room_store.py` 的实际耦合。
