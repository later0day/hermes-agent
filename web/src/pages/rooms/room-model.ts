import type {
  ReplicationHealthResponse,
  RoomActionResponse,
  RoomEvent,
  RoomRoleKind,
  RoomSummary,
} from "@/lib/api";

export function requireRoomActionSuccess(response: RoomActionResponse): void {
  if (!response.ok) throw new Error(response.message || "Room action failed");
}

export function filterEvents(
  events: RoomEvent[],
  actorFilter: string,
  kindFilter: string,
  memberRoles: ReadonlyMap<string, RoomRoleKind> = new Map(),
): RoomEvent[] {
  return events.filter((event) => {
    const actorKind = String(event.actor?.kind ?? "");
    const actorId = String(event.actor?.id ?? "");
    const role = actorKind === "member" ? memberRoles.get(actorId) : undefined;
    if (actorFilter === "coordinator" && role !== "coordinator" && role !== "team_lead") return false;
    if (actorFilter === "teammate" && role !== "teammate") return false;
    if (actorFilter === "observer" && role !== "observer") return false;
    if (actorFilter === "user" && actorKind !== "user") return false;
    if (actorFilter === "system" && actorKind !== "system" && actorKind !== "gateway") return false;
    if (kindFilter === "message" && !event.kind.startsWith("message.")) return false;
    if (kindFilter === "turn" && !event.kind.startsWith("turn.")) return false;
    if (kindFilter === "protocol" && !/^(coordinator|teammate|observer|heartbeat)./.test(event.kind)) return false;
    return true;
  });
}

export type VisualTaskState = "blocked" | "working" | "queued" | "completed" | "idle";
export type HealthLevel = "healthy" | "degraded" | "unhealthy" | "unknown";

export interface WorkspaceTaskLike {
  status?: string | null;
  blocked?: boolean | null;
  current_task?: string | null;
  current_task_id?: string | null;
  completed_at?: number | null;
}

export interface ActivityProjection {
  actor: string;
  summary: string;
  timestamp: number;
  kind: string;
}

export interface ConversationGroup {
  key: string;
  actor: string;
  startedAt: number;
  endedAt: number;
  events: ActivityProjection[];
}

export interface RoomHealth {
  level: HealthLevel;
  healthy: boolean | null;
  issues: string[];
  readyPeers: number;
  totalPeers: number;
}

function recency(room: RoomSummary): number {
  return room.updated_at ?? room.created_at ?? 0;
}

const ROOM_PRIORITY: Record<string, number> = {
  needs_action: 0,
  failed: 1,
  running: 2,
  idle: 3,
};

/** Action/failure priority first, then lifecycle, recency, and stable identity. */
export function compareRoomPriority(a: RoomSummary, b: RoomSummary): number {
  const lifecycle = Number(Boolean(a.disbanded_at)) - Number(Boolean(b.disbanded_at));
  if (lifecycle !== 0) return lifecycle;
  const priority = (ROOM_PRIORITY[a.workspace?.state ?? "idle"] ?? 4)
    - (ROOM_PRIORITY[b.workspace?.state ?? "idle"] ?? 4);
  if (priority !== 0) return priority;
  const recent = recency(b) - recency(a);
  if (recent !== 0) return recent;
  const sequence = b.latest_seq - a.latest_seq;
  if (sequence !== 0) return sequence;
  return a.room_id.localeCompare(b.room_id);
}

export function sortRoomsByPriority(rooms: readonly RoomSummary[]): RoomSummary[] {
  return [...rooms].sort(compareRoomPriority);
}

/** Maps loose workspace/task DTO states into the small visual vocabulary used by cards. */
export function visualTaskState(task: WorkspaceTaskLike | null | undefined): VisualTaskState {
  if (!task) return "idle";
  const status = String(task.status ?? "").toLowerCase();
  if (task.blocked || ["blocked", "failed", "error", "needs_revision"].includes(status)) return "blocked";
  if (task.completed_at || ["completed", "complete", "done", "succeeded", "success"].includes(status)) return "completed";
  if (["in_progress", "running", "working", "active"].includes(status)) return "working";
  if (["pending", "queued", "ready", "waiting"].includes(status)) return "queued";
  if (task.current_task || task.current_task_id) return "working";
  return "idle";
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function projectRoomActivity(event: RoomEvent): ActivityProjection {
  const payload = event.payload ?? {};
  const actor = text(event.actor?.handle) ?? text(event.actor?.id) ?? text(event.actor?.kind) ?? "system";
  const direct = text(payload.text) ?? text(payload.summary) ?? text(payload.description) ?? text(payload.reason);
  let summary = direct;
  if (!summary && event.kind === "coordinator.task_assign") {
    const target = text(payload.target_handle) ?? "unknown";
    summary = `Assigned task to @${target}`;
  }
  if (!summary && event.kind === "turn.requested") summary = "Requested a member turn";
  if (!summary && event.kind === "turn.settled") summary = payload.passed ? "Turn completed successfully" : "Turn completed";
  if (!summary) summary = event.kind.split(".").map((part) => part.replaceAll("_", " ")).join(" · ");
  return { actor, summary, timestamp: event.created_at, kind: event.kind };
}

/** Groups adjacent activity from the same actor within a bounded conversational pause. */
export function groupRoomConversations(events: readonly RoomEvent[], gapSeconds = 300): ConversationGroup[] {
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  const groups: ConversationGroup[] = [];
  for (const event of ordered) {
    const item = projectRoomActivity(event);
    const previous = groups.at(-1);
    if (previous && previous.actor === item.actor && item.timestamp - previous.endedAt <= gapSeconds) {
      previous.events.push(item);
      previous.endedAt = item.timestamp;
    } else {
      groups.push({ key: `${event.room_id}:${event.seq}`, actor: item.actor, startedAt: item.timestamp, endedAt: item.timestamp, events: [item] });
    }
  }
  return groups;
}

export function aggregateRoomHealth(
  replication: ReplicationHealthResponse | null | undefined,
  options: { blockedTasks?: number; pendingActions?: number; observerViolations?: number } = {},
): RoomHealth {
  const issues: string[] = [];
  if (replication) {
    if (!replication.healthy) issues.push("Replication is unhealthy");
    if (replication.unavailable > 0) issues.push(`${replication.unavailable} peer(s) unavailable`);
    if (replication.needs_reauthorization > 0) issues.push(`${replication.needs_reauthorization} peer(s) need reauthorization`);
  }
  if ((options.blockedTasks ?? 0) > 0) issues.push(`${options.blockedTasks} blocked task(s)`);
  if ((options.pendingActions ?? 0) > 0) issues.push(`${options.pendingActions} pending action(s)`);
  if ((options.observerViolations ?? 0) > 0) issues.push(`${options.observerViolations} observer violation(s)`);

  const severe = Boolean(replication && !replication.healthy)
    || (options.blockedTasks ?? 0) > 0
    || (options.observerViolations ?? 0) > 0;
  const level: HealthLevel = !replication && issues.length === 0 ? "unknown" : severe ? "unhealthy" : issues.length ? "degraded" : "healthy";
  return { level, healthy: level === "unknown" ? null : level === "healthy", issues, readyPeers: replication?.ready ?? 0, totalPeers: replication?.total_peers ?? 0 };
}
