# AgentProxy 系统评估报告

> 日期: 2026-07-04
> 评估环境: Cloud `47.242.209.41` (Ubuntu 24.04, 48C/185GB)

## 一、系统概述

AgentProxy 是自研的分布式 AI Agent 编排平台，采用 Cloud-Daemon 架构。

### 技术栈

| 组件 | 技术 | 二进制大小 |
|------|------|-----------|
| Cloud | Go (静态编译) + 内嵌 Flutter Web | 32MB |
| Daemon | Go (静态编译) | 15-23MB |
| Client | Go (静态编译) | 12MB |
| Dashboard | Flutter Web (CanvasKit) | 嵌入 Cloud |
| 传输协议 | gRPC (50051) + QUIC (4433) + HTTPS (8080) |  |
| IDL | Protobuf (`agentproxy.v1`) | |

### 架构图

```
                  ┌───────────────────────────────┐
                  │        Cloud (中控)            │
                  │  gRPC:50051 QUIC:4433 HTTP:8080│
                  └─────────┬─────────────────────┘
                            │ QUIC (mTLS)
          ┌─────────┬───────┼───────┬──────────┐
          ▼         ▼       ▼       ▼          ▼
       [devix]  [devix-  [home]  [android-  [devix-ggm
       claude    build]  codex   pixel7]     -agent]
                shell            shell       proxy→devix
```

## 二、功能矩阵

| 模块 | 状态 | API 端点数 | 说明 |
|------|------|-----------|------|
| Agent 管理 | ✅ | 6 | list/detail/metadata/disconnect/trigger-update/restart/destroy |
| 任务执行 | ✅ | 3 | run(SSE 流式)/delegate(A2A)/broadcast(多 Agent 并发) |
| A2A 协议 | ✅ | 2 | discover + delegate，最大委托深度 5 层 |
| LLM Relay | ✅ | 1 | Anthropic API 中继，日志含请求/响应审计 |
| Enrollment | ✅ | 4 | Token 邀请注册 + 自动证书签发 |
| Workspaces | ✅ | 3 | git clone / 目录挂载 |
| Screen Control | ✅ | 3 | 截图/输入/H.264 视频流 (WebSocket) |
| Terminal | ✅ | 1 | 完整 PTY shell (WebSocket) |
| Port Forward | ✅ | 1 | HTTP 反向代理到 Agent 本地端口 |
| Wiki | ✅ | 4 | 知识库 list/get/search/enable |
| OTA 更新 | ✅ | 6 | binary/sha256/sig/install.sh/update.sh |
| Dashboard | ✅ | 10 页 | agents/llm-traffic/run-task/delegate/deploy/terminal/wiki/screen/port-forward/settings |

## 三、安全体系

| 层级 | 实现 | 评价 |
|------|------|------|
| 传输层 | QUIC + mTLS (P-256 ECDSA) | ✅ |
| 认证 | 自签 CA → Agent 证书 + CN 严格校验 | ✅ |
| 授权 | gRPC Token + Dashboard Token 分离 | ✅ |
| Enrollment | 一次性 Token + 有效期 + 自动签发 | ✅ |
| CRL | 已配置空 CRL 文件 | ✅ (本次新增) |
| 防火墙 | 未配置 | ⚠️ 待加固 |

## 四、运行指标 (2026-07-04 实测)

| 指标 | Cloud | Daemon |
|------|-------|--------|
| RSS 内存 | 115MB | 99MB |
| CPU | 0.3% | 0.0% |
| 线程数 | 26 | 26 |
| Uptime | 持续运行 (systemd) | 10+ 天 |
| 任务延迟 (Claude Opus) | — | 7.2s |
| LLM Relay 延迟 | 5-10s | — |
| Shell 命令延迟 | — | 2-78ms |

## 五、已发现问题及处理

### 已修复 ✅

| 问题 | 方案 | 状态 |
|------|------|------|
| 15 个僵尸进程 | `kill -9` 父进程 1913820 | ✅ 已清理 (0 remaining) |
| CRL 未配置 | 生成空 CRL + cloud.yaml 引用 | ✅ 已配置 |

### 待修复

| 问题 | 优先级 | 推荐方案 |
|------|--------|---------|
| codex-home identity mismatch | 🔴 P0 | 修正远端 agent_id 匹配证书 CN |
| Client gRPC API 不兼容 | 🔴 P0 | 重新编译 client 对齐 Cloud proto |
| 防火墙全开 | 🟡 P1 | 阿里云安全组限制入站 IP (暂缓) |
| Cloud 单点故障 | 🟡 P1 | 状态文件异地备份 + DNS 切换预案 (暂缓) |
| Token 不自动清理 | 🟡 P1 | Cloud 定时清理过期 token |
| 无 /healthz 端点 | 🟡 P1 | 添加健康检查端点 |
| 无 README | 🟡 P1 | 编写项目文档 |
| 无 Prometheus metrics | 🟢 P2 | 暴露 /metrics |
| 无审计日志 | 🟢 P2 | 独立 audit.jsonl |
| Dashboard 嵌入 Cloud | 🟢 P2 | 分离到 Nginx/CDN |
| 证书 2027-06 到期 | 🟢 P2 | 自动续期机制 |
| systemd 以 root 运行 | 🟢 P2 | 创建专用用户 + 权限收窄 |
| 状态文件无备份 | 🟢 P2 | cron 定时 rsync |

## 六、快捷工具

已部署 `/opt/agentproxy/run` 脚本，可对任意 Agent 下发 shell 命令:

```bash
./run                              # 查看在线 Agent
./run home "ls ~/Desktop"          # 对 home 执行命令
./run devix-build "docker ps"      # 对构建机执行
./run android-pixel7 "whoami"      # 对 Android 执行
```

自动根据 Agent 的 `shell_type` (bash/sh/zsh) 选择执行模式。

## 七、总评

**评分: 75/100** — 架构精良、功能极其丰富的自研平台。

**核心优势**: QUIC+mTLS 安全传输、A2A 多 Agent 协作、异构设备支持 (Mac/Linux/Android)、完整 Dashboard。

**主要差距**: 运维成熟度 (监控/审计/HA) 需提升，从 "能用" 到 "可靠生产系统" 还需要打磨。
