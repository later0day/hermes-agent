# Agent Room · M1 完整技术规格

M1 = 手动创建 room + 手动绑定 IM 群 + 观察者单成员路由 + 异步回复。

不在 M1 范围内：自动规划 room（M2）、完整上下文投影（M3）、kanban 拆分路径（M4）。

## 用户拍板决策

见 `README.md` 决策一览表。

## 1. 数据模型

### 1.1 新表 `agent_rooms`

存储位置：`~/.hermes/gateway_agent_rooms.sqlite`（与 `gateway_source_agent_bindings.sqlite` 同级，均为运行时数据，不入 profile 目录）。

```sql
CREATE TABLE agent_rooms (
    room_id           TEXT PRIMARY KEY,        -- uuid，形如 "room_ab12cd34"
    room_name         TEXT NOT NULL UNIQUE,    -- 用户可读名，形如 "客户支持组"
    description       TEXT NOT NULL DEFAULT '',
    observer_profile  TEXT NOT NULL,           -- 观察者 profile 名，形如 "room_customer_support_observer"
    members_json      TEXT NOT NULL DEFAULT '[]',  -- ["客服", "财务", "技术"]
    default_member    TEXT NOT NULL DEFAULT '',    -- 兜底路由目标，为空则用 members[0]
    created_at        INTEGER NOT NULL,
    created_by        TEXT NOT NULL DEFAULT '',
    updated_at        INTEGER NOT NULL,
    updated_by        TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX idx_agent_rooms_name ON agent_rooms(room_name);
```

**设计要点**：
- `observer_profile` 是一个真正落在 `~/.hermes/profiles/` 下的普通 profile 目录，只是它的用途是观察者。跟其他 profile 完全同构，`hermes profile list` 能看到，可以被删除。
- `members_json` 是 profile 名字符串数组。不建外键关联（profile 是文件系统目录，无法建 SQL 外键），运行时校验存在性。
- N3 选项1：`members_json` 长度 hard-limit 5，超过报错。

### 1.2 扩展 `SourceAgentBindingStore`（不改表结构）

复用现有的 `fallback_extra` JSON 字段，加一个可选键：

```json
{
    "session_webhook": "...",
    "session_webhook_expired_time": 12345,
    "room_id": "room_ab12cd34"    ← 新增，可选
}
```

`gateway/run.py::_resolve_profile_home_for_source`（约 24690 行）现有的绑定查询逻辑加一个分支：

```
binding = source_agent_binding_store.get_binding(source_binding_key)
if binding:
    room_id = (binding.fallback_extra or {}).get("room_id")
    if room_id:
        → 进入 room 路由流程（§3）
    else:
        → 使用 binding.profile_name（现有单 profile 分支，不变）
```

**为什么不新建"群→room"表**：`SourceAgentBindingStore` 已经在消息入口路径上被查询、已经处理了 DingTalk 特有的 session_webhook 存储。复用它避免在同一个入口做两次 lookup。

## 2. 观察者 Profile 的构造

### 2.1 目录结构

`~/.hermes/profiles/<observer_profile_name>/`：跟普通 profile 完全同构。

**M1 关键区别**：
- `SOUL.md` 由代码自动生成（模板见 §2.2），每次 room 成员变动时重写
- `config.yaml` 里 `memory.enabled: false`（禁用长期记忆，只保留 session history）
- `config.yaml` 里 `toolsets: ["room_observer"]`（**仅**这一个工具集，锁死行为边界）
- `profile.yaml` 里 `description: "Room observer for <room_name>"`, `description_auto: true`
- 一个内部标记文件 `.observer` 存在，标识这个 profile 是自动生成的观察者

标记文件用途：`/room delete` 时安全地一并删除观察者 profile；`/agent list` 或 dashboard 展示时可以打上"observer"徽章。人工创建的普通 profile 不会有这个文件。

### 2.2 SOUL.md 模板

```markdown
# Observer Agent for Room: {room_name}

You are the observer for a group of specialized agents. Your job is to read
incoming messages and decide which member should respond.

## Room description
{room.description}

## Members
- **{member_1}**: {member_1.description}
- **{member_2}**: {member_2.description}
- **{member_3}**: {member_3.description}
{...每个成员一行}

## Your workflow
1. Read the latest message (see conversation history).
2. Determine whether this is a NEW topic or a continuation of the ongoing one.
   - If continuation: keep routing to the same member as last time.
   - If new topic: pick the best-matching member by their descriptions.
3. Emit your routing decision by calling the `route_to_member` tool with:
   - `member`: the profile name from the roster above
   - `reason`: 1-sentence explanation
   - `is_new_topic`: true if you consider this a new topic

If you cannot decide (message is ambiguous or off-topic), route to
`{default_member}` with reason "fallback".

Do NOT write any user-facing reply yourself. Your only output is the
`route_to_member` tool call.
```

**每次 room 成员变动 → 重生成 SOUL.md**（因为名册变了系统提示要跟着变）。

### 2.3 新增内部工具 `route_to_member`

新文件 `tools/room_router_tool.py`：

```python
def route_to_member(member: str, reason: str, is_new_topic: bool = False) -> dict:
    """Emit a routing decision. Called by the observer agent only."""
    return {
        "action": "route_to_member",
        "member": member,
        "reason": reason,
        "is_new_topic": is_new_topic,
    }
```

- 注册到 `toolsets.py` 一个新的 toolset `room_observer`
- 观察者 profile 的 `toolsets` 只包含 `room_observer` 这一个
- 用 hermes-agent 现有 tool calling 基础设施，模型天然结构化输出（不用 JSON 文本解析，避免格式错误）

### 2.4 N4：轻量分类先行（观察者调用时机优化）

观察者跑完整 agent turn 有成本。N4 选项3 的实现：

1. 每个 room 在内存里缓存 `last_routed_member`（不落库，进程重启后失效）
2. 消息进来 → **先跑一次 aux LLM 调用**（复用 `agent/auxiliary_client.py`）
   - 输入：`最近5条群消息 + last_routed_member`
   - 输出：`{"is_new_topic": bool, "confidence": float}`
3. 如果 `is_new_topic == False && confidence > 0.7 && last_routed_member 存在`：
   - **跳过观察者 turn**，直接沿用 `last_routed_member`
4. 否则：跑完整观察者 turn，通过 `route_to_member` 工具决策

**关键选择**：`last_routed_member` 内存缓存而非落库——重启后第一条消息触发完整观察者判断是正常代价，避免额外 IO；且 fence 时天然失效。

## 3. 运行时路由

### 3.1 消息入口分支

`gateway/run.py` 里在 `_run_agent_inner` 入口（或更早的 `process_message`）前置一个分支：

```python
def _resolve_room_for_source(self, source) -> Optional[RoomBinding]:
    """Check if this source is bound to an agent room (not a single profile)."""
    binding = self._source_agent_binding_store.get_binding(
        build_source_binding_key(source)
    )
    if not binding:
        return None
    room_id = (binding.fallback_extra or {}).get("room_id")
    if not room_id:
        return None
    return self._agent_room_store.get_room(room_id)

# 在 process_message 主分支里：
room = self._resolve_room_for_source(source)
if room:
    return await self._process_message_via_room(message, source, room)
# 否则走现有单 profile 流程（不变）
```

### 3.2 Room 路由完整流程

按 A3 选项1（异步）:

```
Step 1: 立刻在群里发一句 "已收到，正在为你选择处理人..."
        通过 DingTalk session_webhook，不进入任何 agent turn

Step 2: 轻量分类 aux LLM 调用（§2.4）
        输入：最近5条历史 + last_routed_member
        输出：is_new_topic + confidence

Step 3a（沿用路由）: target_member = last_routed_member
Step 3b（完整观察者）:
        - 加载 observer_profile 的 HERMES_HOME
        - session_id = f"room_observer:{room_id}"
        - 触发 hermes-agent 现有的 agent turn 流程
        - 观察者产出 route_to_member 工具调用
        - 提取 target_member = tool_call.arguments["member"]
        - 校验 target_member 存在于 members_json（不存在 → default_member 或 members[0]）
        - 更新 last_routed_member 内存缓存

Step 4: 更新群里 Step 1 那条消息为 "转交给 {target_member} 处理..."
        用 DingTalk adapter 的 edit_message 能力（fork 已实现）

Step 5: 派发给 target_member 执行
        - 加载 target_member profile 的 HERMES_HOME
        - session_id = f"room_member:{room_id}:{target_member}"
                       ← 每个 (room × member) 一个独立 session
                       ← 保证成员能看到"这个 room 里我之前跟用户聊过什么"
        - 走 hermes-agent 现有完整 agent turn 流程
        - 输出通过 session_webhook 发回原群
```

### 3.3 Session ID 命名约定

| Session 用途 | 命名 | 生命周期 |
|---|---|---|
| 观察者 session | `room_observer:{room_id}` | Room 生命周期内持续（历史很小，因为观察者只调 route_to_member 工具，不做长文本对话） |
| 成员 session | `room_member:{room_id}:{member_profile}` | Room 生命周期内持续 |
| 群外单 profile 绑定（现有） | 不变，走 `build_session_key` | 不变 |

**关键**：观察者和成员用**同一个 `source`（原群）**但**不同 profile 的 HERMES_HOME**。session id 通过 room_id 强命名，避免与非 room 场景的 session id 冲撞。

### 3.4 理解 X 的 M1 简化取舍

**理解 X 的完整定义**：所有成员都能读到 room 完整历史，但一次只有观察者选中的成员会跑 turn 并回复。

**M1 的简化**：成员 session 里只包含"这个成员被路由到过的消息"，**不包含**"路由到别的成员时的对话"。也就是说，成员 profile 看不到 room 里"另外那位同事说了什么"。

**为什么这个简化在 M1 可接受**：
- 90% 的实际场景里，客服问题分给客服、财务问题分给财务，两者本来就不需要看对方的对话
- 观察者本身知道全局路由历史（`route_to_member` 调用的 session 里有完整决策链），有跨成员协调需求时观察者可以在系统提示里给成员摘要
- 完整 context-projection（每个成员看到全 room 历史 + 单人称改写）是 M3 的核心工作，跟 M1 分开做，避免 M1 卡在这个复杂度上

**这个取舍如果被否决**：M1 工作量从 ~2500 行翻到 4000-5000 行，需要前置实现 Studio `context-projection.ts` 等价物，M1 交付时间翻倍。

### 3.5 Session 围栏（Fence）机制

借鉴 Studio Group Chat 的 fence 设计，简化版：

- `AgentRoomStore` 内存里维护 `fenced_sessions: set[str]`
- 触发 fence 的操作：
  - `DELETE /room/{name}`（删除 room）
  - `/room unbind`（解绑群）
  - `/room members ... add/remove`（成员名册变动）
- 触发时机：把当前所有该 room 相关的活跃 session id（观察者 + 所有成员）加入 fence 集合
- 检查时机：**每次要往群回消息之前**，检查这个 session_id 是否被 fenced，是则丢弃输出（不写入历史、不发到 IM 群）

M1 只在**改变 room 结构**时 fence，不做 Studio 那种"每条消息细粒度 fence"。此简化对 M1 场景够用。

## 4. Slash Commands

在 `gateway/slash_commands.py` 新增 `_handle_room_command`（跟 `_handle_agent_command` 平级）：

| 命令 | 作用 |
|---|---|
| `/room create <name> --members <p1,p2,p3> [--description "..."] [--default-member <p>]` | 创建 room，自动生成观察者 profile |
| `/room list` | 列出所有 room（本机可见） |
| `/room info <name>` | 查看 room 详情：成员、观察者、绑定的群列表 |
| `/room bind <name>` | 把**当前 IM 会话（群）**绑定到该 room（A6：如群已有绑定，报错要求先 unbind） |
| `/room unbind` | 解绑当前群 |
| `/room delete <name>` | 删除：先解绑所有关联群（fence 进行中 session）→ 删观察者 profile → 删 room 记录 |
| `/room members <name> add <profile>` | 加成员（长度校验 ≤5），触发 SOUL.md 重生成 + fence 当前 session |
| `/room members <name> remove <profile>` | 删成员（同上） |
| `/room set-default-member <name> <profile>` | 设置观察者兜底路由 |

`/room create` 内部逻辑：
1. 校验所有成员 profile 存在，不存在 → 报错让用户先 `/agent create <profile>`
2. 生成观察者 profile 名 `room_<slugified_name>_observer`
3. 调用现有的 `create_profile(name, clone_from="default", clone_env=False, clone_skills=False)`
4. 写 `SOUL.md`（观察者模板，§2.2）
5. 写 `config.yaml`：`toolsets: [room_observer]`, `memory: {enabled: false}`
6. 写 `profile.yaml`：description + description_auto: true
7. 触碰 `.observer` 标记文件
8. 插入 `agent_rooms` 表

## 5. Dashboard REST API + UI

A5 选项3：Slash command 和 Dashboard 双入口。

### 5.1 新增 REST 端点（`hermes_cli/web_server.py`）

- `GET /api/rooms` — 列表
- `POST /api/rooms` — 创建（body: name/description/members/default_member）
- `GET /api/rooms/{room_id}` — 详情
- `PATCH /api/rooms/{room_id}` — 修改成员/描述/默认成员（自动 fence + SOUL 重生成）
- `DELETE /api/rooms/{room_id}` — 删除
- `POST /api/rooms/{room_id}/bind` — body: `{source_binding_key}` 绑定到指定 IM 会话
- `POST /api/rooms/{room_id}/unbind` — body: `{source_binding_key}`

### 5.2 前端页面（简版）

M1 只做能用的最小页面：
- Room 列表 + CRUD
- 每 room 详情页显示：成员列表、绑定的群列表、观察者 profile 链接
- 绑定管理：从 SourceBinding 页面挑一个"群"，选一个"Room"，点绑定

M1 不追求好看，能操作就行。M2 会大改。

## 6. 组件文件清单

| 文件 | 类型 | 估计行数 | 用途 |
|---|---|---|---|
| `gateway/agent_room_store.py` | 新 | ~250 | SQLite 数据模型 + CRUD + fence 集合管理 |
| `gateway/agent_room_router.py` | 新 | ~350 | 路由主流程（轻量分类 + 观察者调用 + 派发成员） |
| `gateway/agent_room_bootstrapper.py` | 新 | ~200 | 创建 room 时生成观察者 profile 目录、SOUL.md、config.yaml |
| `tools/room_router_tool.py` | 新 | ~80 | `route_to_member` 工具 |
| `toolsets.py` | 改 | ~10 | 注册 `room_observer` toolset |
| `gateway/slash_commands.py` | 改 | ~300 | `_handle_room_command` + 子命令派发 |
| `gateway/run.py` | 改 | ~50 | `_resolve_room_for_source` + `_process_message_via_room` 前置分支 |
| `hermes_cli/web_server.py` | 改 | ~250 | 7 个 REST 端点 |
| Dashboard 前端（`hermes_cli/web_dist/` 源工程） | 新 | ~400 | Rooms 管理页面 |
| 单元 + 集成测试 | 新 | ~600 | store CRUD、bootstrapper、路由分支、fence 行为 |
| **合计** | | **~2500** | |

## 7. 验收标准

### 7.1 单元测试
- `AgentRoomStore` CRUD + 并发读写 + 损坏数据容错
- 观察者 SOUL.md 生成：成员名册注入准确、成员被 add/remove 后重生成正确
- Fence 集合：删除/改成员时正确加入，回消息前正确检查

### 7.2 集成测试
- 完整链路：创建 room → 绑定（模拟 DingTalk source） → 发消息 → 观察者路由 → 成员回复 → 回落原群
- 沿用路由（N4）：连续 3 条消息，只有第 1 条跑完整观察者，后 2 条走轻量分类沿用
- 兜底：观察者返回不存在的成员 → 走 default_member/members[0]
- Fence：进行中 session 被 fenced 时输出被丢弃

### 7.3 线上真实环境测试
- 本机创建 room（成员用 `default` + 一个新建的 `test_profile`）
- 用真实 DingTalk 群发消息
- 验证观察者被真实调用、路由决策合理、成员真回复、消息真回群

## 8. M1 交付后必然要面对的问题（M2 起点）

- **成员看不到跨成员上下文**（M1 简化）→ M3 前置需求
- **只支持手动创建 room**（M1 不做规划）→ M2 主题
- **观察者只能路由到单成员**（M1 不拆任务）→ M4 主题
- **消息乱序**（N2 简化）→ M3 一起做
- **Room 权限模型**（谁能创建/绑定/改）→ M2 或独立小任务

M1 每一个简化都有对应的后期落地路线，不会成为死胡同。

## 9. 开工前的最后 3 个疑问

（这些不影响 M1 整体设计，但影响细节实现，等你回复后再动手）

- **Q9.1**：观察者的 aux LLM（轻量分类那个）用哪个 model/provider？走 `auxiliary` 配置（现有 aux 客户端默认）还是让 room 单独指定？
- **Q9.2**：观察者本身跑完整 turn 时，模型/provider 从哪拿？跟观察者 profile 的 `config.yaml` 里定义的走（合理）还是继承发起群的原 profile？我倾向前者。
- **Q9.3**：`/room create` 时如果观察者 profile 名冲突（比如已经有个手工建的 `room_customer_support_observer`）怎么处理？报错要求换 room name（保守）还是加数字后缀（激进）？我倾向报错。
