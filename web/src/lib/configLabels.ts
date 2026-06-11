import type { Locale, Translations } from "@/i18n";

const ACRONYMS = new Set([
  "api",
  "cdp",
  "cli",
  "cpu",
  "cwd",
  "id",
  "json",
  "llm",
  "mb",
  "mcp",
  "ms",
  "oauth",
  "pii",
  "stt",
  "tts",
  "tui",
  "url",
  "urls",
  "yaml",
]);

const ZH_SECTION_LABELS: Record<string, string> = {
  agent: "代理",
  auxiliary: "辅助模型",
  browser: "浏览器",
  compression: "上下文压缩",
  dashboard: "Dashboard",
  delegation: "子代理委托",
  dingtalk: "钉钉",
  discord: "Discord",
  display: "显示",
  gateway: "网关",
  general: "通用",
  human_delay: "模拟人类延迟",
  logging: "日志",
  memory: "记忆",
  model: "模型",
  security: "安全",
  stt: "语音转文字",
  terminal: "终端",
  tts: "文字转语音",
  voice: "语音",
};

const ZH_FIELD_LABELS: Record<string, string> = {
  model: "默认模型",
  model_context_length: "模型上下文长度",
  "agent.max_turns": "最大轮次",
  "agent.gateway_timeout": "网关超时",
  "agent.native_image_max_base64_bytes": "原生图片压缩阈值",
  "agent.restart_drain_timeout": "重启等待时间",
  "agent.service_tier": "服务等级",
  "dashboard.theme": "Dashboard 主题",
  "dashboard.show_token_analytics": "显示 Token 统计",
  "delegation.reasoning_effort": "子代理推理强度",
  "display.busy_input_mode": "忙碌时输入模式",
  "display.resume_display": "恢复会话显示方式",
  "display.skin": "CLI 皮肤",
  "dingtalk.agent_id": "钉钉 AgentId",
  "dingtalk.allow_all_users": "允许所有钉钉用户",
  "dingtalk.allowed_chats": "允许的钉钉群",
  "dingtalk.allowed_users": "允许的钉钉用户",
  "dingtalk.app_code": "钉钉 appCode",
  "dingtalk.card_content_key": "AI Card 内容字段",
  "dingtalk.card_template_id": "AI Card 模板 ID",
  "dingtalk.corp_id": "钉钉 CorpId",
  "dingtalk.free_response_chats": "免 @ 回复群",
  "dingtalk.reply_at_sender": "回复时 @ 发送者",
  "dingtalk.require_mention": "需要 @ 才回复",
  "logging.level": "日志级别",
  "memory.provider": "记忆提供方",
  "terminal.backend": "终端后端",
  "terminal.cwd": "工作目录",
  "terminal.timeout": "命令超时",
  "terminal.vercel_runtime": "Vercel 运行时",
};

const ZH_FIELD_WORDS: Record<string, string> = {
  allow: "允许",
  allowed: "允许的",
  analytics: "统计",
  api: "API",
  auto: "自动",
  backend: "后端",
  browser: "浏览器",
  card: "Card",
  chats: "群聊",
  command: "命令",
  config: "配置",
  context: "上下文",
  cwd: "工作目录",
  dashboard: "Dashboard",
  default: "默认",
  delay: "延迟",
  disabled: "禁用",
  display: "显示",
  enabled: "启用",
  free: "免",
  gateway: "网关",
  id: "ID",
  input: "输入",
  interval: "间隔",
  length: "长度",
  level: "级别",
  max: "最大",
  memory: "记忆",
  mention: "@",
  mode: "模式",
  model: "模型",
  output: "输出",
  provider: "提供方",
  reasoning: "推理",
  require: "需要",
  response: "回复",
  resume: "恢复",
  runtime: "运行时",
  service: "服务",
  show: "显示",
  skin: "皮肤",
  template: "模板",
  theme: "主题",
  timeout: "超时",
  token: "Token",
  tool: "工具",
  users: "用户",
};

const ZH_DESCRIPTIONS: Record<string, string> = {
  model: "默认使用的模型，例如 anthropic/claude-sonnet-4.6。",
  model_context_length: "手动覆盖上下文窗口；0 表示根据模型元数据自动判断。",
  "agent.max_turns": "单次任务允许的最大工具调用/模型循环轮次。",
  "agent.gateway_timeout": "网关中代理完全空闲多久后视为超时，单位秒。",
  "agent.native_image_max_base64_bytes": "原生图片传给主模型前的主动压缩阈值，单位为 base64 字节；0 表示关闭主动压缩。",
  "agent.restart_drain_timeout": "网关重启时等待正在运行任务收尾的最长时间，单位秒。",
  "dashboard.theme": "Web Dashboard 的视觉主题。",
  "dashboard.show_token_analytics": "显示本地估算的 token/cost 统计；该数值不是账单口径。",
  "dingtalk.agent_id": "钉钉企业内部应用 AgentId；当前仅保存并透传，后续接工作通知等 API 时使用。",
  "dingtalk.allow_all_users": "允许所有钉钉用户通过网关鉴权；关闭时应配置用户 allowlist。",
  "dingtalk.allowed_chats": "仅允许这些钉钉群触发机器人，多个 ID 用逗号分隔。",
  "dingtalk.allowed_users": "允许的钉钉 staff_id 或 sender_id，多个值用逗号分隔；* 表示任意用户。",
  "dingtalk.app_code": "钉钉应用 appCode；当前仅保存并透传，现有 Stream 机器人收发不读取它。",
  "dingtalk.card_content_key": "钉钉 AI Card 模板中用于承载回复内容的变量名；留空时使用 msgContent。",
  "dingtalk.card_template_id": "钉钉 AI Card 模板 ID；留空时使用钉钉 SDK 默认 Markdown 卡片。",
  "dingtalk.corp_id": "钉钉企业 CorpId；当前仅保存并透传，后续接企业级开放接口时使用。",
  "dingtalk.free_response_chats": "这些钉钉群里无需 @ 机器人也会响应，多个 ID 用逗号分隔。",
  "dingtalk.reply_at_sender": "开启后，群聊最终回复会 @ 触发这轮对话的发送者。",
  "dingtalk.require_mention": "开启后，钉钉群聊里只有 @ 机器人时才响应。",
  "terminal.backend": "工具执行使用的终端后端，例如 local、docker、ssh。",
  "terminal.cwd": "网关和工具执行的默认工作目录。",
};

function isChinese(locale: Locale): boolean {
  return locale === "zh";
}

function fallbackSegmentLabel(segment: string): string {
  return segment
    .split("_")
    .filter(Boolean)
    .map((word) => {
      const lower = word.toLowerCase();
      if (ACRONYMS.has(lower)) return lower.toUpperCase();
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

function fallbackPathLabel(schemaKey: string): string {
  return schemaKey
    .replace(/\./g, " → ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function translateSegment(segment: string, locale: Locale, kind: "field" | "section"): string {
  if (!isChinese(locale)) return fallbackSegmentLabel(segment);
  if (kind === "section" && ZH_SECTION_LABELS[segment]) return ZH_SECTION_LABELS[segment];
  if (kind === "field" && ZH_FIELD_LABELS[segment]) return ZH_FIELD_LABELS[segment];

  const words = segment
    .split("_")
    .filter(Boolean)
    .map((word) => ZH_FIELD_WORDS[word.toLowerCase()] ?? "");
  if (words.length > 0 && words.every(Boolean)) return words.join("");
  return fallbackSegmentLabel(segment);
}

export function formatConfigCategoryName(
  category: string,
  t: Translations,
  locale: Locale,
): string {
  const knownCategory = (t.config.categories as Record<string, string>)[category];
  if (isChinese(locale) && ZH_SECTION_LABELS[category]) return ZH_SECTION_LABELS[category];
  if (knownCategory) return knownCategory;
  return translateSegment(category, locale, "section");
}

export function formatConfigSectionName(section: string, locale: Locale): string {
  return translateSegment(section, locale, "section");
}

export function formatConfigFieldLabel(schemaKey: string, locale: Locale): string {
  if (isChinese(locale) && ZH_FIELD_LABELS[schemaKey]) return ZH_FIELD_LABELS[schemaKey];
  const rawLabel = schemaKey.split(".").pop() ?? schemaKey;
  return translateSegment(rawLabel, locale, "field");
}

export function formatConfigPathLabel(schemaKey: string, locale: Locale): string {
  return schemaKey
    .split(".")
    .map((part, idx, parts) =>
      idx === parts.length - 1
        ? formatConfigFieldLabel(schemaKey, locale)
        : formatConfigSectionName(part, locale),
    )
    .join(" → ");
}

export function formatConfigDescription(
  schemaKey: string,
  schema: Record<string, unknown>,
  locale: Locale,
): string {
  if (isChinese(locale) && ZH_DESCRIPTIONS[schemaKey]) return ZH_DESCRIPTIONS[schemaKey];

  const description = schema.description ? String(schema.description) : "";
  if (!description) return "";
  if (isChinese(locale) && description === fallbackPathLabel(schemaKey)) {
    return formatConfigPathLabel(schemaKey, locale);
  }
  return description;
}
