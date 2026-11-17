import type {
  ReplicationHealthResponse,
  RoomActionResponse,
  RoomEvent,
  RoomRoleKind,
  RoomSummary,
} from "@/lib/api-rooms";

export function roomErrorMessage(
  error: unknown,
  unreachable = "Couldn’t load teams. The Dashboard service did not respond.",
): string {
  const message = String(error);
  return message.includes("Failed to fetch") ? unreachable : message;
}

export function requireRoomActionSuccess(response: RoomActionResponse, fallback = "Room action failed"): void {
  if (!response.ok) throw new Error(response.message || fallback);
}

const ACTOR_FILTERS: Record<string, (actorKind: string, role: RoomRoleKind | undefined) => boolean> = {
  coordinator: (_actorKind, role) => role === "coordinator" || role === "team_lead",
  teammate: (_actorKind, role) => role === "teammate",
  observer: (_actorKind, role) => role === "observer",
  user: (actorKind) => actorKind === "user",
  system: (actorKind) => actorKind === "system" || actorKind === "gateway",
};

const KIND_FILTERS: Record<string, (kind: string) => boolean> = {
  message: (kind) => kind.startsWith("message."),
  turn: (kind) => kind.startsWith("turn."),
  protocol: (kind) => /^(coordinator|teammate|observer|heartbeat)./.test(kind),
};

function eventMatchesFilters(
  event: RoomEvent,
  actorFilter: string,
  kindFilter: string,
  memberRoles: ReadonlyMap<string, RoomRoleKind>,
): boolean {
  const actorKind = String(event.actor?.kind ?? "");
  const actorId = String(event.actor?.id ?? "");
  const role = actorKind === "member" ? memberRoles.get(actorId) : undefined;
  const matchesActor = ACTOR_FILTERS[actorFilter];
  if (matchesActor && !matchesActor(actorKind, role)) return false;
  const matchesKind = KIND_FILTERS[kindFilter];
  return !matchesKind || matchesKind(event.kind);
}

export function filterEvents(
  events: RoomEvent[],
  actorFilter: string,
  kindFilter: string,
  memberRoles: ReadonlyMap<string, RoomRoleKind> = new Map(),
): RoomEvent[] {
  return events.filter((event) => eventMatchesFilters(event, actorFilter, kindFilter, memberRoles));
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

export interface RoomHealthOptions {
  blockedTasks?: number;
  pendingActions?: number;
  observerViolations?: number;
}

function collectRoomHealthIssues(
  replication: ReplicationHealthResponse | null | undefined,
  options: RoomHealthOptions,
): string[] {
  const candidates = [
    { present: Boolean(replication && !replication.healthy), message: "Replication is unhealthy" },
    { present: Boolean(replication && replication.unavailable > 0), message: `${replication?.unavailable} peer(s) unavailable` },
    { present: Boolean(replication && replication.needs_reauthorization > 0), message: `${replication?.needs_reauthorization} peer(s) need reauthorization` },
    { present: (options.blockedTasks ?? 0) > 0, message: `${options.blockedTasks} blocked task(s)` },
    { present: (options.pendingActions ?? 0) > 0, message: `${options.pendingActions} pending action(s)` },
    { present: (options.observerViolations ?? 0) > 0, message: `${options.observerViolations} observer violation(s)` },
  ];
  return candidates.filter((candidate) => candidate.present).map((candidate) => candidate.message);
}

function roomHealthLevel(
  replication: ReplicationHealthResponse | null | undefined,
  issues: readonly string[],
  severe: boolean,
): HealthLevel {
  if (!replication && issues.length === 0) return "unknown";
  if (severe) return "unhealthy";
  if (issues.length > 0) return "degraded";
  return "healthy";
}

export function aggregateRoomHealth(
  replication: ReplicationHealthResponse | null | undefined,
  options: RoomHealthOptions = {},
): RoomHealth {
  const issues = collectRoomHealthIssues(replication, options);
  const severe = replication?.healthy === false
    || (options.blockedTasks ?? 0) > 0
    || (options.observerViolations ?? 0) > 0;
  const level = roomHealthLevel(replication, issues, severe);
  return { level, healthy: level === "unknown" ? null : level === "healthy", issues, readyPeers: replication?.ready ?? 0, totalPeers: replication?.total_peers ?? 0 };
}
