# Agent Room × Raft AX —— 完整吸收对照与改造路线

> 基准原文：Raft《Is Having Agents in the Room Meant to Be Chaotic?》(2026-05-21, Tenny)
> 权威来源：官方为 agent 提供的 `.../post.md`（已全文核对，6 大板块 + 2 子板块 + 6 图无遗漏）
> 对照对象：本仓库 `gateway/agent_room_*.py` + `tools/room_*.py`（**已演进到 m1-spec 之后**）
> 目的：不是照搬 Raft，而是用 AX 的四问框架**审计并补齐**我们中心化路由架构的短板。

---

## 0. 一句话结论

Raft 的核心洞见 —— **agent 是"回合制"的（读快照→推理→提交，中间什么都不跑），房间会在
"推理与提交之间"漂移** —— 我们的 `AgentRoomRouter` 其实**已经在多处无意识地实现了它的解法**
（Fence 三关卡、no-match 一等结果、@mention 确定性路由）。真正缺的只有三块：
**(1) 输出被丢弃时 agent 无知情/可恢复权**（对应 held-draft），
**(2) 线上 DingTalk session_webhook 过期导致回复彻底丢失**（缺陷 #2），
**(3) 成员 context 仍是全量 push、无 pull**（对应 agent inbox）。

---

## 1. Raft 的 AX 四问 × 我们代码的逐项审计

Raft 定义 AX = 对每个 agent 接触的界面问四个问题。用它审我们的 room：

| AX 四问 | 我们当前实现 | 评级 | 缺口 |
|---|---|---|---|
| ① 动作时**看到**什么 | `agent_room_projection.project_for_member()` 全量投影 + `_MAX_CONTENT_CHARS=4000` 硬截断 | ⚠️ push 全量 | 无按需 pull（改造 3） |
| ② 调用间**携带**什么状态 | 每 member 独立 session `room_member:{room_id}:{member}`；`_last_routed_member` 内存缓存 | ✅ 良好 | 重启丢缓存（可接受，M1-B11） |
| ③ 能从什么**恢复** | Fence 命中 / webhook 失效 → **静默丢弃**（`return {"reply": None}`） | ❌ 不可恢复 | held-draft 退回（改造 1）+ 缺陷 #2 |
| ④ 被**允许决定**什么 | observer 只能 route；member 只能回/@handoff | ⚠️ 部分 | 缺 stay-silent 显式出口（改造 2，**已有雏形**） |

---

## 2. 我们【已经】实现了 Raft 的哪些原则（不要重复造）

审计代码发现三处已与 Raft 主张同构，**改造时必须复用、不得另起炉灶**：

### 2.1 「no-match 是一等结果」≈ Raft 的 stay-silent 雏形
`agent_room_firsthop.FirstHopResult.matched` + `RoutingDecision.is_no_match`
（router.py L823-842）已经把"这条不属于任何人"当作**独立信号**，而非静默塞给
default。docstring 明确引用了 hermes-studio / AutoGen / OpenAI Agents SDK 三个
参考设计都把 "no match" 当 first-class outcome。
→ **这正是 Raft "Silence is a valid outcome" 的一半**。差别：我们 no-match 仍会
**兜底派发**（`_apply_no_match_prefix` 给成员加"你是兜底，请用判断"前缀），而 Raft
允许**真正沉默**。改造 2 只需补上"真沉默"这条腿。

### 2.2 「@mention 确定性路由」≈ Raft 反对的规则门控的**正确用法**
`agent_room_mentions.resolve_mention_targets()` 零 LLM 解析 `@member`。
Raft 批评 @mention **作为唯一入口**会让 agent 退化成工具——但我们只把它当
**first-hop 的优先短路**（router.py L790），未命中才走分类器。这规避了 Raft 的批评。

### 2.3 「Fence 三关卡」≈ Raft held-draft 的**新鲜度检查思想**（但结局相反）
router.py 在三处检查 `store.is_fenced()`：observer 后（L808）、member 派发前
（L998）、member 派发后（L1023）。**触发条件**是 room 结构变化（删/解绑/改成员，
`AgentRoomStore.fence_room()`）。这与 Raft held-draft 的"提交前比对房间版本"是**同一
思想**。
→ 唯一差别、也是最大差距：Raft 比对后**退回 agent 给四条路**；我们比对后
**静默 drop**（丢弃、不落库、不通知）。**这就是改造 1。**

---

## 3. 三项改造（按优先级）

### 改造 1（P0）· Fence/webhook 失败从"静默 drop"升级为"held-draft 退回" + 修缺陷 #2

**锚点**：router.py 所有 `return {..., "reply": None, "fenced_at": ...}` 分支
（L813, L923, L960, L1003, L1028）+ 线上 `[Dingtalk] No valid session_webhook`。

**设计（对齐 held draft 四条路）**：
1. member turn 产出 reply 后，不直接投递，构造
   `HeldReply{room_id, session_id, member, room_version, payload, held_reason}`。
   `room_version` 复用 `agent_room_messages_store` 已有的消息 sequence。
2. 投递前双检查：`is_fenced()` 命中 **或** DingTalk session_webhook 失效 → **hold**（不丢）。
3. hold 后给出四条路（一一映射 Raft）：
   - **Revise**：用当前 `project_for_member()` 重投影 + 重跑一轮；
   - **Send as-is**：改走 DingTalk **主动消息 API `robotSendToConversation`**
     （不依赖过期 webhook）—— **缺陷 #2 的正解**；
   - **Stay silent**：若该话题已被他人覆盖 → 记 log 合法丢弃（不再是黑洞）；
   - **Send anyway**：重试多次仍 hold → 显式旁路。
4. `HeldReply` **落库**（新表 `agent_room_held_replies`），gateway 重启后恢复投递
   —— 解决"重启丢回复"。注意：这与 `AgentRoomStore` fence 集合"进程内不持久"的
   设计（L108-113）并不冲突——fence 防的是进程内竞态，held-reply 落库防的是重启丢投递，
   两者正交。

**改动面**：`agent_room_store.py`(+held 表 CRUD)、`agent_room_router.py`(投递分支)、
`plugins/platforms/dingtalk/adapter.py`(+`robotSendToConversation`)。≈ +400 行 + 测试。

#### 实现进度（2026-08-16）

**已落地（第一纵切：hold，不再黑洞）**
- `gateway/agent_room_held_store.py`（新）：`HeldReply` + `AgentRoomHeldStore`
  （SQLite `agent_room_held_replies` 表，WAL/retry 同 messages_store）。
  `hold()` / `get()` / `list_held()` / `resolve(one-of-四路)` / `delete_room()`。
  **落库持久**，`test_held_survives_reopen` 证明重启后仍在。14 测试全绿。
- `agent_room_messages_store.max_sequence(room_id)`（新）：held-draft 的
  **room_version 快照锚点**（复用已有单调 sequence，删尾行会正确回退版本）。
- `agent_room_router.py`：新增可选注入 `held_store` + `room_version_provider`；
  helper `_current_room_version()` / `_hold_reply()`。**单成员 + 多成员的
  post-dispatch fence 分支**：命中时不再 `reply:None` 静默丢，而是
  `_hold_reply(...)` 落库并回传 `held_id` / `held_ids`。
  **向后兼容**：未注入 held_store 时逐字保持旧的静默丢弃（`held_id=None`）；
  hold 落库失败也降级为旧丢弃、绝不 crash 路由。5 新测试 + 原 64 全绿。
- `gateway/run.py`：懒初始化 `_agent_room_held_store`，把 held_store +
  `room_version_provider=lambda rid: messages_store.max_sequence(rid)`
  接进 `AgentRoomRouter`（线上已激活）。
- `gateway/slash_commands.py`：`/room delete` 级联清理 held-store（并补上
  一直缺的 messages_store 级联），best-effort 幂等。

**已落地（第二纵切：resolver + Send-as-is 投递，黑洞真正闭合）**
- `gateway/agent_room_held_resolver.py`（新）：`AgentRoomHeldResolver` +
  `ResolveOutcome`。四条路全部实现且带自动选路：
  - **自动策略**：`held.room_version == 当前` → Send-as-is；`当前 > held` → Revise。
  - **Send-as-is / Send-anyway**：把落库的 payload 原样通过
    **transport-independent** 的 `deliver`（线上 = `adapter.send`）投递；
    投递失败**不消费** held 行（留给下次 drain 重试）。
  - **Revise**：重跑 member 拿新 reply 再投；无 rerun 时**降级为 Send-as-is**
    （投稍旧的也胜过丢）；重跑空 → 合法 Stay-silent。
  - **Stay-silent**：不投递，仅 resolve + 记 log（可问责的丢弃，非黑洞）。
  - `resume_all(room_id?)`：按 created_at 顺序**排空 held backlog**，单行崩溃隔离。
    14 测试全绿。
- **Send-as-is 底座已 re-apply**：`stash@{0}` 的 DingTalk adapter webhook-independent
  fallback（AI Card / robot-native proactive）已应用回工作区，`adapter.send` 现在
  **不再依赖过期 webhook** —— 因 webhook 过期而被 hold 的 reply 现在能被 resolver 投出。
  84 dingtalk 测试全绿。
- `gateway/run.py`：构造 `AgentRoomHeldResolver`（`deliver` 包 `adapter.send`→bool）；
  **启动时 `resume_all()`** 恢复跨重启的 held reply；**每条消息处理后
  `resume_all(room.room_id)`** 机会式排空（新 inbound 通常意味 transport 复活，
  是投递 hold 的最佳时机）。

**已落地（第三纵切：version-based hold —— 纠偏，闭合文章最核心的 turn-based gap）**
- **背景（走偏自查）**：前两纵切把 held-draft **窄化**成了「fence / webhook 失败救援」，
  触发器是 `is_fenced()` / 传输失败，而**不是**文章的核心机制——
  「agent 对着房间**快照**推理，commit 前房间在**间隙**里动了」。这漏掉了
  文章反复举例的 **counting-game / 话题漂移** 场景：成员在 compose 期间
  用户又发了新一轮消息，旧 reply 投出去就成了 **答非所问的 non-sequitur**。
- **修法**：投递发生在 `run.py::_member_dispatcher` 内（`adapter.send` 是那里的
  side-effect），所以版本闸门必须落在**产出 reply 之后、append+投递之前**：
  - 进 dispatcher 时 `_snapshot_version = messages_store.max_sequence(room_id)`
    ——成员推理所对的房间快照。
  - 产出 reply 后，`list_messages(since_seq=_snapshot_version)` 里若出现**新的
    `user` 消息** → 判定「房间动了」→ **不 append、不投递**，改为
    `held_store.hold(held_reason=HELD_REASON_ROOM_MOVED)` 落库，返回 `""`
    （不触发 @mention handoff）。
  - **只数 `user` 消息**：并发多成员是我们**有意**的同轮广播，彼此的 `member`
    回复不算「房间移动」，否则会误伤多声道广播（且破坏既有并发测试）。
- **resolver 按 held_reason 分流降级**（关键正确性）：线上 `rerun_member=None`。
  - `room_moved` 是**可证明的陈旧**（新用户轮次已取代它）→ 无 rerun 时降级为
    **Stay-silent**（可问责的记录式丢弃），**绝不** Send-as-is 把 non-sequitur 再投出去；
  - 传输丢失类（`fenced`/`no_webhook`/`send_failed`）reply 本身仍有效 → 保持
    降级为 **Send-as-is**（投稍旧的胜过丢）。
  - 有 rerun 时 `room_moved` 走真正的 **Revise**（重跑拿对新问题的新答复）。
- `HELD_REASON_ROOM_MOVED` 新增进 `_VALID_HELD_REASONS`。store +1、resolver +3
  新测试；agent_room 全 15 文件 307 测试全绿，零回归。

**已落地（改造 1 收尾：线上真·Revise，2026-08-16）**
- **`RerunMember` 签名改为收整个 `HeldReply`**（原来是 `(room_id,member,session_id)`）——
  这样 rerun 实现拿得到 `chat_id` + `extra`（platform/chat_type/user/original_msg），
  可无歧义地重建一次成员轮次。
- **room_moved hold 现在 stash 传输元数据到 `extra`**：dispatcher 在 hold 时把
  platform / chat_type / user_id / user_name / thread_id / original_msg 一并落库。
- **`run.py::_agent_room_held_rerun(held)`（新）**：从 held 行重建 `SessionSource`
  → 对**当前**房间做 fresh windowed 投影（含那条把房间推进的新用户消息）→ 用
  「修订请求」framing 让成员看现状后决定**更新回复**还是**空内容=不再发言** →
  返回文本但**不投递、不 re-hold**（resolver 投到 held 行自己的 chat_id）。
  防御式:room 没了 / 成员已移除 / platform 未知 / 重跑异常 → 返回 ""，drain 不崩。
  已接进 resolver 构造(`rerun_member=self._agent_room_held_rerun`)。
- 效果:被 hold 的 "1... 2... 3!" 现在能被改成 "哦你说停了,那算了",而不是原样投出的答非所问。
  resolver 侧:room_moved + **有** rerun → 真 Revise;空重跑 → stay-silent。

**待做（后续）**
- **pre-dispatch / observer-fence** 分支仍是静默——那是**尚未产出 reply**
  的阶段（无 artifact 可 hold），本就不该 hold，保持不变是正确的。

### 改造 2（P1）· 给 no-match 补上"真沉默"出口（对齐 Silence is a valid outcome）

**锚点**：router.py L824 `is_no_match` 现在**总是**兜底派发。

**设计**：room 增加可选配置 `no_match_policy: fallback | silent`（默认 fallback 保持现状）。
当 `silent` 且 `is_no_match=True`：不派发任何成员，返回
`{"fenced_at": None, "target_member": None, "reply": None, "stayed_silent": True}`。
复用现有 `is_no_match` 信号，**零新数据模型**。≈ +60 行。

#### 实现进度（2026-08-16）· 已落地

- **零新 DB schema**：`no_match_policy` 做成 **router 构造参数 + gateway 配置驱动**
  （`agent_room.no_match_policy`），不是 per-room DB 列——避免迁移，可回滚。
  未知值一律降级 `fallback`（**绝不**意外静默）；大小写不敏感。
- `agent_room_router.py`：构造参数 `no_match_policy="fallback"`；`process_message`
  在 decision 定下后、派发前加闸门：`silent` 且 `decision.is_no_match` → **派发 NOBODY**，
  不更新 last_routed，返回 `stayed_silent=True` bundle。
  @mention / N4-reuse 从不置 `is_no_match`，因此永不会被静默;真实 domain match 也不受影响。
- `run.py`：从 `_load_gateway_config()["agent_room"]["no_match_policy"]` 读取并注入 router。
- 5 新测试（silent 静默 / fallback 仍派发 / 不静默真匹配 / 不静默 @mention / 未知值降级）。
- **已知取舍**：Step 1 的"已收到…"ack 在 silent 下会成为悬挂消息（重排 ack 顺序会给
  常见 fallback 路径加延迟）；silent 是 opt-in,可接受。默认 fallback 无此问题。

### 改造 3（P2，并入 M3）· 成员 context 从全量 push 加一层 pull（对齐 agent inbox）

**锚点**：`agent_room_projection.project_for_member()` 全量 + 4000 字截断。

**设计**：投影时只 push「摘要 + 最近 N 条」；新增内部工具
`room_fetch_context(query|range)`（放 `tools/`，注册进成员 toolset），让成员**按需 pull**
跨成员历史 / 附件全文。对齐 Raft"agent 决定什么值得进它的 context"。≈ +200 行，
建议与 M3 完整投影层一起做。

#### 实现进度（2026-08-16）

**已落地（第一纵切：push 反转 —— summary + recent N）**
- `agent_room_projection.project_for_member_windowed(messages, target, *, recent_n=12)`（新）：
  向后兼容的超集。房间 `<= recent_n` 行时**逐字等价** `project_for_member`（小房间零变化）；
  超出时把较早的尾部塌成**一条 digest 消息**（每行 `#<seq> [who] 短预览`，160 字上限、
  oldest-first、可被 `#seq` 寻址）+ 最后 N 条**逐字**正常投影。
  取代历史的「全量 push + 4000 字硬截断」（文章批评的"房间替 agent 决定 context"）。
  确定性 O(n)。`run.py::_member_dispatcher` 已切到 windowed 投影。9 新测试。

**已落地（第二纵切：pull 侧 —— room_fetch_context 工具）**
- `tools/room_fetch_context_tool.py`（新）：成员按需拉取工具，注册进 **`room_member`** toolset。
  - `query`（大小写不敏感子串）**或** `start_seq/end_seq`（含端点，对应 digest 的 `#seq`）
    二选一；返回 `{"ok",rows:[{seq,who,content}]}`（逐字、每行 2000 字上限）。
  - **房间作用域由 ContextVar 绑定**（`bind_room_context`，dispatcher 用 try/finally
    包住成员 run），**不是**工具入参 —— LLM 无法伪造 room_id 去读别的房间。
  - 防御式：未绑定 context / 读取失败 / query+range 同时给 → 结构化 error，绝不 raise。
  - 安全上限：range 50 行 / query 20 命中，防止一次 pull 把 context 又撑爆。
- `run.py::_run_agent_inner`：新增 `_extra_toolsets` kwarg —— **union**（非 replace，
  区别于 observer 的 `_toolsets_override`）进 source/profile 正常解析出的 toolset。
  成员 run 传 `_extra_toolsets=["room_member"]`，在不改任何 profile 配置的前提下
  额外授予 room_fetch_context。成员 prompt 补一句：开头可能是 room digest 摘要，
  需要较早消息全文时调 room_fetch_context。
- store scope-isolation 单测证明绑定 R 的成员**读不到** OTHER 房间。tool 14 测试全绿；
  agent_room 全 16 文件 330 测试零回归；prod venv 冒烟通过。

---

## 4. 明确【不采纳】Raft 的部分（架构边界）

| Raft 主张 | 我们的选择 | 理由 |
|---|---|---|
| **去掉编排器/中心调度**（"no orchestrator, nobody telling agents whose turn it is"） | **保留** first-hop 分类器 + observer 决策的中心路由 | IM 群场景中心路由更可控、天然无抢答碰撞；且已支撑 M4 `decompose_and_route` 任务拆分与 handoff 链。去掉等于推翻半个架构 |
| **每个 agent 靠感知面自决何时开口** | 部分吸收（no-match/silent），但**不做全员并发自决** | 全员自决在真实 IM 群未验证，风险高；我们用"单点触发 + @handoff 链"达到类似的多方协作，可控性更好 |

> 换言之：**吸收 Raft 的交互面设计（inbox/held-draft/silence），保留我们的中心化编排架构。**

---

## 5. 落地顺序

1. **改造 1**（held-draft 化 + 缺陷 #2）—— 线上正在丢回复，价值最高。
2. **改造 2**（silent 出口）—— 小、低风险、复用 `is_no_match`。
3. **改造 3**（inbox pull）—— 并入 M3 投影层。

每项动代码前：先补/改设计 → `scripts/run_tests.sh`（HERMES_PYTHON=/tmp/hermes-test-venv/bin/python）
→ 改到 DingTalk adapter 等线上路径后重启 xcx 网关并线上验证（重启前先确认无活跃会话被打断）。

---

## 6. 线上真实环境测试方案（改造 1）

> 目标：在真实 xcx 网关 + 真实 DingTalk 群里，验证「fenced/webhook 失效的 reply
> 不再被静默丢弃，而是被 hold → resolver 投出」，且**无回归**。

### 6.0 前置

- 单测已全绿（下方基线）：`held_store 14 + resolver 14 + router 69 + messages_store 21
  + dingtalk 84 + gateway_entry/slash/store/... `。
- 重启前**必须**确认 xcx 网关无活跃会话（`grep 'inbound message' 最近日志`，
  无进行中的 member turn）。
- 全程**不 echo/print** 任何 basic-auth / API key；不把凭据放进 URL。

### 6.1 重启并确认健康

```
# 确认无活跃会话后重启 xcx 网关（走既有 systemd/进程管理）
# 重启后看日志（不含任何密钥）：
tail -f /root/.hermes/profiles/xcx/logs/gateway.log
```
预期：3 platform connected、无 Traceback、
`held-draft startup drain` 若有 backlog 会打印 `drained N row(s)`。

### 6.2 用例 A · webhook 过期 → hold → 下条消息触发投递（缺陷 #2 正解）

1. 在绑定了 room 的 DingTalk 群里发一条会触发**长 member turn**的问题
   （让回复产出时 session_webhook 已过期），或人工构造 webhook 失效。
2. **预期（旧行为）**：回复彻底丢失。
   **预期（新行为）**：`adapter.send` 自动走 proactive/AI-Card 投出（多数情况直接送达）；
   若该刻 proactive 也不可用 → router 落一条 `HeldReply`
   （日志 `held reply from member ... reason=...`）。
3. 隔一会儿再发**任意一条**消息 → `post-message drain` 触发
   `resume_all(room_id)` → 日志 `held N: delivered via send_as_is`，
   之前 hold 的回复此刻送达群里。
4. 核验：`sqlite3 gateway_agent_room_held.sqlite
   "SELECT id,member,held_reason,status,resolution FROM agent_room_held_replies"`
   → 对应行 `status=resolved, resolution=send_as_is`。

### 6.3 用例 B · 结构变更 fence → hold（不再静默丢）

1. member turn 进行中时执行 `/room members <name> remove <profile>` 或 `/room unbind`
   （触发 `fence_room`）。
2. **预期**：turn 完成后命中 post-dispatch fence → 不再 `reply:None` 黑洞，
   而是落 `HeldReply(reason=fenced, room_version=<派发时快照>)`。
   日志 `fenced during dispatch; held (id=...)`。
3. 核验 held 行存在；因该 room 结构已变，可人工 `resolve` 为 stay-silent，
   或（room 仍在时）下条消息 drain 时按版本对比走 Send-as-is/Revise。

### 6.4 用例 C · 跨重启恢复（durable 承诺）

1. 制造一条 held 行（用例 A/B），**不**触发投递。
2. 重启 xcx 网关。
3. **预期**：启动 `resume_all()` 把该行投出（transport 已恢复）→
   `held N: delivered`；`status=resolved`。若 transport 仍不可用 → 行保持 held，
   下次 drain 再试（**不消费**）。

### 6.5 回归核验（必须无变化）

- 正常 room 消息路由/回复、@mention、多成员并发、handoff 链 —— 行为不变。
- 未绑定 room 的普通会话 —— 完全不走 held-draft 路径。
- `/room delete <name>` —— 级联清掉该 room 的 messages + held 行（无孤儿）。

### 6.6 回滚

- 代码回滚：held-draft 为**纯增量**——`held_store=None` 即恢复旧静默丢弃；
  最坏情况 revert 本批 commit。
- 数据：`gateway_agent_room_held.sqlite` 独立文件，删除不影响其它存储。
