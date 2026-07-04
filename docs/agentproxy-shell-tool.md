# AgentProxy Shell 快捷执行工具

> 日期: 2026-07-04 (updated)
> 位置: `/opt/agentproxy/run`
> Alias: `ap` (已配置在 `~/.bashrc`)

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
