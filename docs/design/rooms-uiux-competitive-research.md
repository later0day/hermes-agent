# Hermes Rooms UI/UX 竞品调研与设计决策

> 调研日期：2026-09-06
> 范围：多智能体 IDE、工作流运行台、任务系统、执行诊断产品。
> 目的：验证并修订 `rooms-uiux-prd-prototype.html` 的产品假设；不是品牌视觉临摹。

## 1. 执行摘要

现有 Rooms 页面的问题，不是“缺少一个更漂亮的拓扑图”，而是把 Task、Conversation、Event、Pending Action、Peer、Replication、Observer 等不同层级的信息同时平铺。

竞品的共同规律是：

1. **列表先回答哪里需要关注**：Temporal、n8n、Linear 都先提供可过滤的运行/对象列表，而不是直接打开原始日志。
2. **对象详情再分层**：Temporal 将 History、Workers、Relationships、Pending Activities、Metadata 分开；GitHub Actions 使用 Run → Job → Log 层级。
3. **Graph 和 Chat 服务不同用户**：LangSmith Studio明确拆成 Graph mode 与 Chat mode；Dify 也明确区分一次性 Workflow 与会话型 Chatflow。
4. **图表达依赖，列表承载操作**：GitHub 的图节点显示状态、连线显示依赖，点击节点进入日志；图不是所有管理操作的容器。
5. **原始执行历史是诊断层**：Temporal 将 Event History 作为一个详情面，而不是默认首页；n8n 通过 Executions 列表进入具体失败执行和 Debug。
6. **人工介入必须贴近阻塞对象**：Temporal 暴露 Pending Activities 和 Workflow Actions；Agent 产品需要把 permission/retry 直接关联 Task 和 attempt。
7. **线程必须有稳定关联键**：LangSmith 要求父子 runs 都携带 thread_id，否则过滤、成本和聚合都会失真。这与 Hermes 的 task_id/thread_id/generation 精确关联完全一致。

因此建议保持：**Room Inbox + 默认 Tasks + Conversation + Activity + Context Inspector + Action Center**。但原型需要补强：运行历史入口、保存视图、List/Graph 切换、明确的 attempt 层、Conversation/Activity 的交叉定位，以及“运行态与配置态”分离。

## 2. 研究样本与证据

### 2.1 LangSmith Studio

官方定义 Studio 为可视化、交互和调试 agentic systems 的专用 IDE，并明确提供两种模式：

- **Graph mode**：显示节点遍历、中间状态、完整执行细节；面向开发和调试。
- **Chat mode**：更简单，面向业务用户测试整体 agent 行为。
- 还包括 thread、assistant、prompt、experiment 和 time-travel state debugging。

**可借鉴**：不要把图、聊天和调试塞进同一信息平面；用户目标不同，应提供明确模式切换。

**不照搬**：Hermes Rooms 是运行中的团队工作台，不是 agent graph IDE；默认页不应是静态架构画布。

来源：[LangSmith Studio](https://docs.langchain.com/langsmith/studio)

### 2.2 LangSmith Threads

LangSmith 使用 session_id/thread_id 将 traces 归入会话，并特别要求所有 child runs 都传播 thread metadata，否则 thread 过滤、token 计算和成本聚合都会不完整。

**可借鉴**：Conversation、Activity、Task Inspector 必须共享 task/thread/turn 标识；不能仅按 actor 或时间猜测关联。

**对 Hermes 的要求**：任何 UI 投影都必须来自服务端真实 task_id、thread_id、discussion_event_id、member_id、execution_generation，不得生成前端“近似关联”。

来源：[Configure threads](https://docs.langchain.com/langsmith/threads)

### 2.3 AutoGen Studio

AutoGen Studio 当前提供四个主要界面：Team Builder、Playground、Gallery、Deployment。Playground 支持：

- agent 间消息实时流；
- control transition graph；
- pause/stop run；
- interactive sessions。

其官方也明确说明产品属于快速原型工具，而非 production-ready app。

**可借鉴**：成员消息流和控制转换图适合实时观察；Pause/Stop 是运行级操作。

**不照搬**：Hermes 必须保留真实权限、fencing、跨进程决策和安全反馈，不能采用原型产品“点了就算成功”的交互。

来源：[AutoGen Studio](https://microsoft.github.io/autogen/stable/user-guide/autogenstudio-user-guide/index.html)

### 2.4 CrewAI Tracing

CrewAI 将 agent decisions、task execution timeline、tool usage 和 LLM calls 作为 trace 的核心维度。

**可借鉴**：Task 详情内应提供可折叠的 attempt/turn/tool/LLM 层级，而不是把所有 tool event 混入主 Conversation。

**不照搬**：Rooms 的默认用户任务是管理团队工作，不是纯 observability；trace 应位于 Activity/Inspector 深层。

来源：[CrewAI Tracing](https://docs.crewai.com/en/observability/tracing)

### 2.5 Temporal Web UI

Temporal 的首页是可查询的 Workflow Executions 列表，支持按 status、workflow ID/type、时间、自定义属性过滤。选中一次执行后，详情拆分为：

- History；
- Workers；
- Relationships；
- Pending Activities；
- Queries；
- Metadata；
- Workflow Actions。

Temporal 还支持 Saved Views，使复杂筛选可复用。

**可借鉴**：

- Room Inbox 应是可筛选的运行收件箱；
- Needs Action/Failed/Running 应成为默认视图或保存视图；
- Pending Action 是执行详情的一级状态；
- History、Workers、Relationships、Metadata 不应平铺在同一个长页面；
- 时间支持 relative/local/UTC，Rooms 默认相对时间，Inspector 显示完整时间。

来源：[Temporal Web UI](https://docs.temporal.io/web-ui)

### 2.6 GitHub Actions

每次 workflow run 生成实时可视化图：

- job 左侧图标表达状态；
- job 之间连线表达依赖；
- 点击 job 查看日志；
- 日志可搜索与下载；
- 运行摘要、Job、Step、Log 分层。

**可借鉴**：Graph 的职责是理解依赖和状态；点击节点进入 Task/Attempt Inspector。List 仍应是默认操作面。

**不照搬**：Hermes Task 不只是静态 CI job，还包含 owner、conversation、approval、retry generation、peer route，需要比 GitHub 节点更丰富的 Inspector。

来源：[Using the visualization graph](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-the-visualization-graph)、[Monitor workflows](https://docs.github.com/en/actions/how-tos/monitor-workflows)

### 2.7 n8n

n8n 将 execution 定义为一次 workflow run，分为 workflow-level executions 与 all executions。失败执行可以进入 Debug in editor，加载过去执行数据后修复并重跑。它还强调 execution data redaction：保留状态、耗时、节点名，但隐藏输入输出敏感数据。

**可借鉴**：

- Room 和 Task 都需要 attempt history；
- failed/deferred 应可从历史 attempt 进入安全 Retry；
- Raw payload 和 tool output 应提供敏感信息折叠/复制边界；
- “调试历史运行”与“编辑当前定义”要明确区分。

来源：[Understand executions](https://docs.n8n.io/build/understand-workflows/understand-executions)、[Debug executions](https://docs.n8n.io/build/understand-workflows/understand-executions/debug-executions)

### 2.8 Dify Workflow / Chatflow

Dify 使用共享画布和节点系统，但把交互模型拆成：

- Workflow：一次输入到最终输出；
- Chatflow：每条用户消息触发有结构的流程。

**可借鉴**：Rooms 同时包含任务执行和对话协作，UI 必须明确 Task 与 Conversation 是两个投影，而不是混成一条万能时间线。

**不照搬**：Rooms 的 DAG 是运行时、服务端拥有的任务 DAG，不是任意拖拽的设计时流程；V1 不应把静态 flow builder 当核心。

来源：[Workflow & Chatflow](https://docs.dify.ai/en/cloud/use-dify/build/workflow-chatflow)

### 2.9 Linear Views

Linear Custom Views 提供可保存、共享、收藏的筛选视图；List/Board 过滤结果可以保存。右侧 sidebar 用于解释当前 View 的内容和常见过滤属性；分组标题可折叠，URL 可携带临时过滤条件。

**可借鉴**：

- Room Inbox 提供 Needs Action、Running、Failed、Idle 预设视图；
- 用户过滤状态写入 URL；
- Task 状态组可折叠；
- 右侧 Inspector 只解释当前选择，而不是承载所有 Room 元数据；
- 高密度操作优先列表而非卡片瀑布。

来源：[Linear Custom Views](https://linear.app/docs/custom-views)

## 3. 横向对比矩阵

| 产品 | 默认入口 | 依赖表达 | 对话 | 执行历史 | 人工介入 | 详情模式 | 对 Rooms 的价值 |
|---|---|---|---|---|---|---|---|
| LangSmith Studio | Graph/Chat 模式 | Graph | Chat mode | Runs/state | Interrupt | 多模式 | Tasks/Conversation/Activity 分离 |
| AutoGen Studio | Playground | Transition graph | 实时消息 | Run control | Pause/Stop | Playground | 成员通信与运行控制 |
| CrewAI | Trace | Timeline/flow | 非核心 | Agent/task/tool/LLM | HITL 相关 | Trace | Attempt/Tool 深层诊断 |
| Temporal | Execution list | Relationships | 无 | Event History | Workflow Actions/Pending | 多 Tab | Room Inbox 与 Diagnostics 分层 |
| GitHub Actions | Run list | Job graph | 无 | Job/step logs | Rerun/cancel | 层级下钻 | List→Graph→Log 层级 |
| n8n | Execution list/editor | Canvas | 无 | Executions | Debug/rerun | Editor/Execution | Attempt 历史和失败重跑 |
| Dify | Workflow/Chatflow | Canvas | Chatflow | Logs | 节点调试 | 双产品模式 | Task 与 Conversation 分开 |
| Linear | Filtered list | issue relations | comments | activity | triage/actions | Peek/sidebar | 高密度列表、保存视图、URL 状态 |

## 4. 对当前 Hermes 原型的评审

### 4.1 已被竞品证据支持的部分

1. **三层结构成立**：Room Inbox → Workspace → Inspector，符合 Temporal、Linear、GitHub 的列表到详情模式。
2. **Tasks 默认成立**：运行管理产品首先展示执行对象，不首先展示原始日志。
3. **Conversation 与 Activity 分离成立**：LangSmith/Dify 都区分业务交互与完整执行细节。
4. **Action Center 成立**：Pending/Interrupt/Workflow Actions 都是阻塞执行的独立对象。
5. **Graph 作为辅助视图成立**：GitHub 用图表达依赖并点击进入日志，而非用图替代全部列表操作。
6. **Inspector 上下文化成立**：Linear sidebar 和多种 execution UI 都使用选择对象详情，而非所有 metadata 常驻。
7. **Raw Events 下沉成立**：Temporal History、n8n executions 都属于下钻诊断层。

### 4.2 当前原型必须修改

#### M1：新增 Room Saved Views/Presets

左栏从简单 chips 升级为：

- Needs action；
- Failed；
- Running；
- Idle；
- All；
- 用户本地保存的过滤视图（后续）。

V1 至少 URL 持久化筛选；V2 支持保存。

#### M2：Tasks 顶部增加 List / Graph / Attempts

推荐：

`[List] [Graph] [Attempts]`

- List：默认管理视图；
- Graph：依赖理解；
- Attempts：按时间查看当前 Room 的 task execution attempts。

不要把 Attempts 塞入 Raw Event。

#### M3：Task Inspector 增加 Attempt 层

Task → Attempt → Turn/Tools → Raw events。

必须展示：

- attempt number；
- execution generation；
- cancel generation；
- started/settled time；
- current tool；
- terminal reason；
- accepted/rejected member result；
- previous attempts。

#### M4：Conversation 增加稳定定位与范围提示

顶部显示：

`Conversation · Entire room` 或 `Conversation · T3`

Task 过滤来自真实 thread_id/task_id。消息卡提供：

- Open task；
- Open attempt；
- Show tool calls；
- Copy message link。

#### M5：Activity 增加运行级层次

Activity 不只是扁平时间线，而应支持：

- Room events；
- Task events；
- Attempt events；
- Infrastructure events。

默认仍显示人类可读 timeline；选择一条后 Inspector 展示完整因果链。

#### M6：Action 贴近 Task，同时保留全局 Inbox

Action 同时出现于：

- Room Inbox 计数；
- Room Header banner；
- Task Row；
- Task Inspector；
- Action Center。

它们必须引用同一个 action ID，不维护多份前端状态。

#### M7：增加日志敏感信息设计

Raw payload、terminal output、tool arguments：

- 默认折叠；
- 长输出截断；
- 可显式复制；
- 支持服务端已 redacted 标识；
- 不因被折叠就丢失状态、耗时、工具名。

#### M8：明确配置态与运行态

- 当前 Room 页面是运行态 Workspace；
- roster/profile/tool policy 的配置入口独立；
- Inspector 的 Allowed Tools 默认只读；
- 不在运行页伪造 Team Builder。

## 5. 修订后的页面模型

```
Room Inbox
  ├─ Preset views / URL filters
  ├─ Needs-action-first sorting
  └─ Room summary rows

Room Workspace
  ├─ Header: progress / members / health / run controls
  ├─ Tasks
  │   ├─ List (default)
  │   ├─ Graph
  │   └─ Attempts
  ├─ Conversation
  │   └─ room scope or exact task/thread scope
  └─ Activity
      ├─ human-readable timeline
      └─ raw event debug mode

Context Inspector
  ├─ Task
  ├─ Attempt
  ├─ Member
  ├─ Action
  ├─ Event
  └─ Diagnostics
```

## 6. 三个候选设计方向

### 方案 A：Operations Desk（推荐）

- 默认 List；
- Action、failed、blocked 置顶；
- Inspector 展示 Task/Attempt；
- Graph/Conversation/Activity 为一级切换；
- 适合真实生产控制。

### 方案 B：Agent Theater

- 默认 Conversation；
- 成员消息、presence、transition graph 更突出；
- Task 作为右栏摘要；
- 适合演示和观看，但复杂 DAG 管理较弱。

### 方案 C：Trace Lab

- 默认 Attempts/Activity；
- 节点、tool、LLM、raw event 为中心；
- 适合开发调试，但普通任务操作者认知成本高。

**最终推荐：A 为默认，B/C 作为视图能力融入，不做三个彼此割裂的产品。**

## 7. 可验证的设计原则

1. 用户 3 秒内能找到待行动 Room。
2. 用户 5 秒内能判断 Task 进度与阻塞原因。
3. 正常任务无需打开 Raw Events。
4. 任一消息可定位真实 Task/Thread/Attempt。
5. 任一 Action 可定位受阻 Task，并只提交一次真实决策。
6. Graph 节点与 List row 指向同一个 Task Inspector。
7. 失败任务可以看到完整 attempt 历史，但默认不泄露敏感 payload。
8. Peer/Replication/Authority 健康时不占主工作区。
9. 不存在只改变视觉、没有后端效果的控件。
10. 页面刷新后保留 Room、Tab、Task、filter 上下文。

## 8. 下一版 HTML 原型变更清单

- [ ] 左栏加入更明确的 preset view 导航与 filter count。
- [ ] Tasks 增加 List / Graph / Attempts 二级切换。
- [ ] Task Inspector 增加 attempt history。
- [ ] Conversation 显示当前 scope 与稳定链接入口。
- [ ] Activity 支持 Task/Attempt/Infrastructure 分层。
- [ ] Action Center 增加 conflict/stale/delivered 状态图。
- [ ] Raw Event 增加 redacted/复制安全状态。
- [ ] 增加 Peer reauthorization 和 provider failure 页面状态。
- [ ] 增加 mobile 的 Room list、Task detail、Action sheet 三个画板。
- [ ] 将三个候选方向做成 HTML 内可切换的 design rationale 面板。
