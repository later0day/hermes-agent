# AgentProxy Shell 快捷执行工具

> 日期: 2026-08-20 (updated)
> 位置: `/opt/agentproxy/run`
> Alias: `ap` (已配置在 `~/.bashrc`)

> **认证**: Dashboard token **自动**从环境变量 `$DASHBOARD_TOKEN` 读取，
> 若无则回退到 `/opt/agentproxy/.env`（cloud 服务的权威来源）。
> **无需再手动更新脚本里的 token** —— cloud 轮换 token 后脚本会自动跟随，
> 不会再出现 401 导致 Agent 列表为空的问题。

## 用法

```bash
ap                                   # 查看在线 Agent 列表
ap <agent_id> <command>              # 对指定 Agent 执行 shell 命令
```

也可以用完整路径 `./run` 或 `/opt/agentproxy/run`。

## 示例

```bash
ap home "ls ~/Desktop"               # macOS (zsh)
ap devix-build "docker ps"           # Linux (bash)
ap android-pixel7 "getprop ro.product.model"  # Android (sh)
ap home 'df -h / | awk NR==2{print\ $4}'     # 含单引号/特殊字符
```

## 特性

- 自动检测 Agent 的 `shell_type` (bash/sh/zsh)，`sh` 类型不加 `mode:shell` 避免报错
- 通过 `sys.argv` 传参做 JSON 编码，正确处理单引号、双引号、反斜杠等特殊字符
- Agent 不存在或不在线时给出红色错误提示
- Token 自动从 `$DASHBOARD_TOKEN` / `.env` 读取，缺失时红字报错退出
- API 返回错误（如 401 unauthorized）时明确打印原因，不再静默返回空列表
- SSE 流式输出，实时显示结果
- 完成后显示耗时和状态 (✓/✗)
- 无参数时列出所有在线 Agent

## 测试覆盖

| 场景 | 状态 |
|------|------|
| 基础命令 | ✅ |
| 管道 | ✅ |
| 单引号 / 双引号 / 嵌套引号 | ✅ |
| 变量 $HOME / 子命令 $(cmd) / 反引号 | ✅ |
| awk 花括号 + $ | ✅ |
| 分号 / && / \|\| | ✅ |
| 通配符 * / 重定向 > < | ✅ |
| 反斜杠 / 换行 / Tab | ✅ |
| 中文 | ✅ |
| 失败命令（非零退出） | ✅ |
| 无输出命令 | ✅ |
| Agent 不存在 | ✅ |
| Android sh 类型 | ✅ |

## 实现

调用 Dashboard API `POST /api/tasks/run`，参数:

```json
{"agent_id": "<id>", "prompt": "<command>", "mode": "shell"}
```

- 命令通过 `python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$*"` 编码，避免 shell 引号问题
- Android 等 `shell_type=sh` 的 Agent 不传 `mode` 字段（走 Agent 默认 shell 执行）
- Agent 存在性检查在发送任务前完成，不存在则提前报错退出
- **认证 token 不再硬编码**：按 `$DASHBOARD_TOKEN` → `/opt/agentproxy/.env` 顺序解析，
  跟随 cloud 的 `DASHBOARD_TOKEN` 轮换自动生效（cloud 配置 `configs/cloud.yaml`
  中 `dashboard.auth_token: "${DASHBOARD_TOKEN}"` 由 systemd EnvironmentFile 注入）
