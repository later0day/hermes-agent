import type { StatusResponse } from "@/lib/api";
import { ApiError } from "@/lib/api-error";
import { prefersZh } from "@/lib/doc-locale";

/** Profiles a gateway restart would blip when the managed profile is carried by the shared
 *  multiplexer (default first). `null` for a standalone gateway or an older backend without
 *  `gateway_shared_with`; those keep the plain restart copy. */
export function sharedGatewayProfiles(
  status: Pick<StatusResponse, "gateway_shared_with"> | null | undefined,
): string[] | null {
  const shared = status?.gateway_shared_with;
  if (!Array.isArray(shared)) return null;
  const names = [...new Set(shared.map((n) => String(n).trim()).filter(Boolean))];
  if (names.length < 2) return null;
  return names.sort((a, b) =>
    a === "default" ? -1 : b === "default" ? 1 : a.localeCompare(b),
  );
}

export function sharedGatewayRestartDescription(profiles: string[]): string {
  return prefersZh()
    ? `本设备上的所有机器人都会重连：${profiles.join("、")}`
    : `All bots on this device reconnect: ${profiles.join(", ")}`;
}

export function sharedGatewayRestartedMessage(count: number): string {
  return prefersZh() ? `共享网关已重启（${count} 个机器人）` : `Shared gateway restarted (${count} ${count === 1 ? "bot" : "bots"})`;
}

/** Raw `gateway_state` values (gateway/status.py) → what they mean for the user, per dashboard language.
 *  `exited`: a `degraded` record of a dead process is a watchdog exit, not a live gateway with channels down. */
const GATEWAY_STATE_COPY: Record<"en" | "zh", Record<string, string>> = {
  en: {
    running: "Running — messaging channels are online",
    starting: "Starting up",
    degraded: "Running with some channels offline — see Logs",
    stopped: "Stopped — messaging channels are offline",
    startup_failed: "Failed to start — see Logs",
    exited: "Exited: a watchdog stopped a wedged gateway — see Logs",
  },
  zh: {
    running: "运行中 —— 消息频道在线",
    starting: "正在启动",
    degraded: "运行中，部分频道离线 —— 请查看日志",
    stopped: "已停止 —— 消息频道离线",
    startup_failed: "启动失败 —— 请查看日志",
    exited: "已退出：看门狗停止了卡住的网关 —— 请查看日志",
  },
};

/** Plain description of the gateway's state; null/unknown falls back to running/stopped. */
export function gatewayStateDescription(
  state: string | null | undefined,
  running: boolean | undefined,
): string {
  const copy = GATEWAY_STATE_COPY[prefersZh() ? "zh" : "en"];
  if (state === "degraded" && running === false) return copy.exited;
  if (state && state !== "exited" && copy[state]) return copy[state];
  return running ? copy.running : copy.stopped;
}

/** True when the state points at Logs as the next step. */
export function gatewayStateNeedsLogs(state: string | null | undefined): boolean {
  return state === "startup_failed" || state === "degraded";
}

const GATEWAY_VERB_COPY: Record<"start" | "stop" | "restart", string> = {
  start: "Could not start the gateway",
  stop: "Could not stop the gateway",
  restart: "Could not restart the gateway",
};

/** Toast for a failed Start/Stop/Restart: lead with the outcome, then the detail, then the fix.
 *  `error` (the caught value) decides whether the Logs pointer makes sense: when the dashboard
 *  itself is unreachable (ApiError.status 0) the Logs page cannot load either, so it is omitted. */
export function gatewayActionFailedMessage(
  verb: "start" | "stop" | "restart",
  detail: string,
  error?: unknown,
): string {
  const trimmed = detail.trim().replace(/\.+$/, "");
  const ended = /[!?]$/.test(trimmed);
  const head = trimmed
    ? `${GATEWAY_VERB_COPY[verb]}: ${trimmed}${ended ? "" : "."}`
    : `${GATEWAY_VERB_COPY[verb]}.`;
  const unreachable = error instanceof ApiError && error.status === 0;
  return unreachable ? head : `${head} Open Logs for details.`;
}

/** A 409 on gateway start/stop for a served profile carries the multiplexer explanation in
 *  `detail`, which `fetchJSON` already lifts into `ApiError.message`. Return it, else null. */
export function servedProfileRefusal(error: unknown): string | null {
  if (error instanceof ApiError) {
    return error.status === 409 ? error.message : null;
  }
  // Pre-ApiError shape (`"409: {...}"`) from callers that still hand-roll fetch.
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (!text.startsWith("409")) return null;
  const detail = text.match(/"detail"\s*:\s*"([^"]+)"/)?.[1];
  return detail ?? text.replace(/^409:\s*/, "");
}
