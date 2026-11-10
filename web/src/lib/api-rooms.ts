import { fetchJSON } from "@/lib/api";

export interface RoomMember {
  member_id?: string;
  profile?: string;
  handle?: string;
  display_name?: string;
  role?: "worker" | "decider";
  target?: { kind?: string; profile?: string } | null;
}

export interface RoomCreateRequest {
  room_id: string;
  name: string;
  members: Array<{
    profile: string;
    handle: string;
    display_name?: string;
    role: "worker" | "decider";
  }>;
}

export interface RoomMessageRequest {
  text: string;
  recipient: string;
  event_id: string;
  thread_id: string;
}

export interface RoomSummary {
  room_id: string;
  name: string;
  members: RoomMember[];
  revision: number;
  latest_seq: number;
  authority_epoch: number;
  authority_gateway_id?: string | null;
  created_at?: number | null;
  updated_at?: number | null;
  disbanded_at?: number | null;
  workspace?: RoomWorkspaceSummary | null;
}

export interface RoomWorkspaceSummary {
  state: "needs_action" | "failed" | "running" | "idle";
  health: "healthy" | "warning" | "critical";
  task_counts: Record<string, number> & { total: number; completed: number };
  pending_action_count: number;
  active_member_count: number;
  current_task: { task_id: string; subject: string; assignee?: string | null } | null;
  last_activity_at: number;
  driver_running: boolean;
}

export interface RoomListResponse {
  rooms: RoomSummary[];
}

export interface RoomDriverStatus {
  running?: boolean;
  working?: boolean;
  blocked?: boolean;
  counts?: Record<string, number>;
  pending_actions?: Array<Record<string, unknown>>;
  peer_routes?: unknown;
}

export interface RoomDetailResponse {
  room: RoomSummary & {
    authority_claim?: Record<string, unknown>;
  };
  driver_status: RoomDriverStatus | null;
}

export interface RoomEvent {
  room_id: string;
  seq: number;
  event_id: string;
  kind: string;
  actor: { kind?: string; id?: string } & Record<string, unknown>;
  authority_epoch: number | null;
  payload: Record<string, unknown>;
  created_at: number;
}

export interface RoomLogResponse {
  events: RoomEvent[];
  cursor: number;
  latest_seq: number;
  has_more: boolean;
  authority: { gateway_id?: string | null; epoch?: number | null };
}

export interface RoomWorkspaceTask {
  task_id: string;
  subject: string;
  description?: string;
  status: string;
  visual_state: string;
  owner?: string | null;
  blockedBy?: string[];
  blocks?: string[];
  dispatch_thread_id?: string | null;
  pending_actions: PendingAction[];
  latest_attempt?: RoomTaskAttempt | null;
  driver_only?: boolean;
}

export interface RoomTaskAttempt {
  identity: { room_id: string; task_id: string; thread_id: string; turn_id: string };
  status: string;
  execution_generation: number;
  cancel_generation: number;
  created_at: number;
  updated_at: number;
  started_at?: number | null;
  terminal_at?: number | null;
  indeterminate_at?: number | null;
  settlement_status?: string | null;
  terminal_reason?: string | null;
  error?: string | null;
  redacted?: boolean;
}

export interface RoomConversationItem {
  event_id: string;
  kind: string;
  actor_id: string;
  actor_kind: string;
  text: string;
  task_id?: string | null;
  thread_id?: string | null;
  turn_id?: string | null;
  terminal_kind?: string | null;
  created_at: number;
}

export interface RoomActivityItem {
  event_id: string;
  seq: number;
  kind: string;
  category: string;
  title: string;
  summary: string;
  task_id?: string | null;
  thread_id?: string | null;
  member_id?: string | null;
  created_at: number;
  raw_event: Pick<RoomEvent, "room_id" | "seq" | "event_id" | "kind" | "actor" | "authority_epoch" | "created_at"> & { redacted: true };
}

export interface RoomWorkspaceResponse {
  room: RoomSummary;
  tasks: RoomWorkspaceTask[];
  attempts: RoomTaskAttempt[];
  pending_actions: PendingAction[];
  conversation: RoomConversationItem[];
  activity: RoomActivityItem[];
  log: RoomLogResponse;
}

export type RoomRoleKind = "coordinator" | "teammate" | "observer" | "team_lead";

export type ObserverState =
  | "armed"
  | "delivering"
  | "denied"
  | "retired"
  | "stopped"
  | "blocked";

export interface RoomMemberRole {
  member_id: string;
  handle: string;
  profile: string;
  role: RoomRoleKind;
  /** Observer state-machine state; null for non-observer roles. */
  observer_state?: ObserverState | null;
  /** Activity level 0-100; null for observers. */
  activity_level?: number | null;
  /** Current task subject (from C3 task DAG where owner == handle). */
  current_task?: string | null;
  /** Current task ID. */
  current_task_id?: string | null;
}

export interface RoomTopologyResponse {
  room_id: string;
  members: RoomMemberRole[];
  coordinator_id: string | null;
  team_lead_id: string | null;
  current_turn?: number | null;
  current_round?: number | null;
  max_rounds?: number | null;
}

export type PendingActionKind = "permission" | "plan_approval" | "shutdown" | "retry";

export interface PendingAction {
  room_id: string;
  action_id: string;
  kind: PendingActionKind;
  description: string;
  from_handle: string;
  detail: Record<string, unknown>;
  created_at: number;
  task_id?: string | null;
  redacted?: boolean | Record<string, unknown>;
}

export interface PendingActionsResponse {
  room_id: string;
  actions: PendingAction[];
}

export interface RoomActionResponse {
  ok: boolean;
  message?: string;
}

export interface RoomLogFilter {
  actors?: string[];
  kinds?: string[];
  since_seq?: number;
  limit?: number;
}

export interface PeerGrant {
  room_id: string;
  member_id: string;
  status: string;
  target_profile?: string | null;
  target_install_id?: string | null;
  capability_digest?: string | null;
  execution_policy_digest?: string | null;
  execution_policy?: Record<string, unknown> | null;
}

export interface PeerGrantsResponse {
  room_id: string;
  peer_grants: PeerGrant[];
}

export interface ReplicationHealthResponse {
  room_id: string;
  healthy: boolean;
  total_peers: number;
  ready: number;
  unavailable: number;
  needs_reauthorization: number;
  peers: PeerGrant[];
}

export interface PolicyTraceEvent {
  seq?: number;
  kind?: string;
  actor?: Record<string, unknown>;
  created_at?: number;
}

export interface PolicyTraceResponse {
  room_id: string;
  through_seq: number;
  stopped_through_seq: number;
  event_count: number;
  events: PolicyTraceEvent[];
  watermarks?: Record<string, number>;
  error?: string;
}

export const roomsApi = {
  listRooms: (all = false) =>
    fetchJSON<RoomListResponse>(`/api/rooms${all ? "?all=true" : ""}`),
  getRoom: (roomId: string) =>
    fetchJSON<RoomDetailResponse>(`/api/rooms/${encodeURIComponent(roomId)}`),
  getRoomLog: (roomId: string, sinceSeq = 0, limit = 100) =>
    fetchJSON<RoomLogResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/log?since_seq=${sinceSeq}&limit=${limit}`,
    ),
  getRoomWorkspace: (roomId: string) =>
    fetchJSON<RoomWorkspaceResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/workspace`,
    ),
  createRoom: (body: RoomCreateRequest) =>
    fetchJSON<{ room: RoomSummary }>("/api/rooms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  sendRoomMessage: (roomId: string, body: RoomMessageRequest) =>
    fetchJSON<{ event: RoomEvent; accepted: boolean }>(
      `/api/rooms/${encodeURIComponent(roomId)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getRoomTopology: (roomId: string) =>
    fetchJSON<RoomTopologyResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/topology`,
    ),
  getRoomPendingActions: (roomId: string) =>
    fetchJSON<PendingActionsResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/pending-actions`,
    ),
  approveRoomAction: (roomId: string, actionId: string) =>
    fetchJSON<RoomActionResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/pending-actions/${encodeURIComponent(actionId)}/approve`,
      { method: "POST" },
    ),
  denyRoomAction: (roomId: string, actionId: string) =>
    fetchJSON<RoomActionResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/pending-actions/${encodeURIComponent(actionId)}/deny`,
      { method: "POST" },
    ),
  getRoomPeerGrants: (roomId: string) =>
    fetchJSON<PeerGrantsResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/peer-grants`,
    ),
  getRoomReplicationHealth: (roomId: string) =>
    fetchJSON<ReplicationHealthResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/replication-health`,
    ),
  getRoomPolicyTrace: (roomId: string) =>
    fetchJSON<PolicyTraceResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/policy-trace`,
    ),
  getRoomLogFiltered: (roomId: string, filter: RoomLogFilter) => {
    const params = new URLSearchParams();
    if (filter.since_seq !== undefined) params.set("since_seq", String(filter.since_seq));
    if (filter.limit !== undefined) params.set("limit", String(filter.limit));
    filter.actors?.forEach((a) => params.append("actor", a));
    filter.kinds?.forEach((k) => params.append("kind", k));
    return fetchJSON<RoomLogResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}/log?${params.toString()}`,
    );
  },
};
