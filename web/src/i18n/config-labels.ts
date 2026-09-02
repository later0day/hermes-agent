// Config field localization overlay.
//
// ConfigPage/AutoField render 786 schema fields whose labels are humanized
// straight from the dot-path key (e.g. "agent.max_turns" → "Max Turns") and
// whose descriptions come from the backend schema (English). This overlay lets
// non-English locales localize a *curated subset* of those fields — the ones
// that carry hand-written, user-facing descriptions in the backend schema —
// without churning every locale file or fighting the ever-changing official
// config schema.
//
// Design:
//   - Keyed by config dot-path (the same `schemaKey` AutoField receives), so it
//     tracks the *current* schema. Keys that no longer exist in the schema are
//     simply never consulted; missing keys fall back to the humanized label +
//     English schema.description. Nothing here can "overwrite old files."
//   - Only locales that actually translate a field appear. English is the
//     implicit fallback and is intentionally NOT listed.
//   - `label` overrides the auto-humanized label; `description` overrides the
//     schema.description. Either may be omitted independently.
import type { Locale } from "./types";

export interface ConfigFieldLabel {
  label?: string;
  description?: string;
}

type ConfigLabelOverlay = Partial<Record<Locale, Record<string, ConfigFieldLabel>>>;

const CONFIG_FIELD_LABELS: ConfigLabelOverlay = {
  zh: {
    model: {
      label: "模型",
      description: "默认模型（例如 anthropic/claude-sonnet-4.6）",
    },
    model_context_length: {
      label: "上下文长度",
      description: "上下文窗口覆盖值（0 = 根据模型元数据自动检测）",
    },
    timezone: {
      label: "时区",
      description: "IANA 时区（例如 America/New_York）。留空则使用系统时区。",
    },
    "agent.service_tier": {
      label: "服务层级",
      description: "API 服务层级（OpenAI/Anthropic）",
    },
    "agent.max_turns": { label: "最大轮次" },
    "terminal.backend": {
      label: "终端后端",
      description: "终端执行后端",
    },
    "terminal.modal_mode": {
      label: "Modal 模式",
      description: "Modal 沙箱模式",
    },
    "terminal.vercel_runtime": {
      label: "Vercel 运行时",
      description: "Vercel Sandbox 运行时",
    },
    "browser.headed": {
      label: "有界面模式",
      description:
        "以有界面模式（可见窗口）运行本地浏览器。窗口也会在多轮之间保持打开；空闲会话仍会在 browser.inactivity_timeout 后被回收。",
    },
    "display.resume_display": {
      label: "恢复显示方式",
      description: "恢复的会话如何显示历史记录",
    },
    "display.busy_input_mode": {
      label: "忙碌输入模式",
      description: "代理运行时的输入行为",
    },
    "display.skin": {
      label: "CLI 主题",
      description: "CLI 视觉主题",
    },
    "dashboard.theme": {
      label: "仪表盘主题",
      description: "Web 仪表盘视觉主题",
    },
    "tts.provider": {
      label: "语音合成提供方",
      description: "文字转语音提供方",
    },
    "human_delay.mode": {
      label: "拟人延迟模式",
      description: "模拟打字延迟模式",
    },
    "context.engine": {
      label: "上下文引擎",
      description: "上下文管理引擎",
    },
    "memory.provider": {
      label: "记忆提供方",
      description: "记忆提供方插件",
    },
    "delegation.reasoning_effort": {
      label: "推理强度",
      description: "委派子代理的推理强度",
    },
    "approvals.mode": {
      label: "审批模式",
      description: "危险命令审批模式",
    },
    "logging.level": {
      label: "日志级别",
      description: "agent.log 的日志级别",
    },
    "plugins.hook_callback_timeout": {
      label: "钩子回调超时",
      description:
        "受超时约束的进程内 Python 插件钩子回调（热路径观察者 + pre_tool_call）的挂钟上限（秒）。超时的 pre_tool_call 会以失败关闭处理。0 表示禁用上限；超过 600 的值会被钳制。像 subagent_stop 这类调用方线程钩子永远不会移到超时工作线程。",
    },
    "updates.non_interactive_local_changes": {
      label: "非交互式本地改动处理",
      description:
        "当聊天应用/网关更新 Hermes（无终端提示）时，如何处理未提交的本地源码改动。'stash' 会保留它们并在更新后重新应用；'discard' 会丢弃它们。终端更新无论此设置如何都会询问。",
    },
    "updates.refresh_cua_driver": {
      label: "刷新 CUA 驱动",
      description:
        "在 hermes 更新期间刷新已安装的 cua-driver。在 /Applications 不可写的非管理员 macOS 账户上请禁用此项。",
    },
    "proxy.enabled": {
      label: "启用出口代理",
      description:
        "仅限 Docker 的出口凭据防火墙。需要 `hermes egress setup` 和 `hermes egress start`；Modal/SSH/Daytona 尚未接入。",
    },
    "proxy.credential_source": {
      label: "凭据来源",
      description: "iron-proxy 启动时从何处加载真实的上游密钥",
    },
    "proxy.enforce_on_docker": {
      label: "对 Docker 强制执行",
      description: "当已启用出口但未配置/未运行时，拒绝 Docker 沙箱",
    },
  },
};

/**
 * Look up a localized label/description for a config field. Returns `undefined`
 * when the current locale has no override for this key (caller falls back to the
 * humanized label + English schema.description). English always returns
 * `undefined` by design.
 */
export function getConfigFieldLabel(
  locale: Locale,
  schemaKey: string,
): ConfigFieldLabel | undefined {
  return CONFIG_FIELD_LABELS[locale]?.[schemaKey];
}
