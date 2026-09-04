import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Crown,
  Eye,
  Home,
  Key,
  Network,
  Pause,
  Play,
  Power,
  RotateCw,
  Shield,
  User as UserIcon,
  Users,
} from "lucide-react";
import {
  api,
  type MailboxMessage,
  type MailboxResponse,
  type ObserverState,
  type ObserverStatus,
  type PeerGrant,
  type PendingAction,
  type PendingActionKind,
  type PolicyTraceResponse,
  type ReplicationHealthResponse,
  type RoomDetailResponse,
  type RoomEvent,
  type RoomLogResponse,
  type RoomMemberRole,
  type RoomRoleKind,
  type RoomSummary,
  type RoomTopologyResponse,
} from "@/lib/api";
import { useI18n } from "@/i18n";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { FilterGroup, Segmented } from "@nous-research/ui/ui/components/segmented";

/* ------------------------------------------------------------------ */
/*  RoomsPage — read-only inspector for hosted rooms.                  */
/*                                                                     */
/*  Deliberately read-only: the dashboard reuses the TUI for chat, so  */
/*  there is no second Web composer here. Roster / authority / driver  */
/*  / append-only event log only. Mutations live on /room + groups.*.  */
/* ------------------------------------------------------------------ */

// English fallbacks so the page renders before/without translation.
const FALLBACK = {
  title: "Hosted Rooms",
  description:
    "Read-only inspector for multi-agent hosted rooms — roster, authority, driver status, and the append-only event log. Create or disband from a chat with /room.",
  empty: "No hosted rooms yet. Create one from a chat with /room create.",
  showDisbanded: "Show disbanded",
  members: "Members",
  revision: "Revision",
  latestSeq: "Log seq",
  authorityEpoch: "Authority epoch",
  authorityGateway: "Authority gateway",
  created: "Created",
  updated: "Updated",
  disbanded: "Disbanded",
  driver: "Driver",
  running: "Running",
  working: "Working",
  blocked: "Blocked",
  idle: "Idle",
  eventLog: "Event log",
  seq: "Seq",
  kind: "Kind",
  actor: "Actor",
  content: "Content",
  noEvents: "No events.",
  loadMore: "Load more",
  selectRoom: "Select a room to inspect its state and event log.",
  teamTopology: "Team Topology",
  roleCoordinator: "Coordinator",
  roleTeammate: "Teammate",
  roleObserver: "Observer",
  roleTeamLead: "Team Lead",
  noTopology: "No topology data available.",
  turnProgress: "Turn",
  roundProgress: "Round",
  pendingActions: "Pending Actions",
  noPendingActions: "No pending actions.",
  approve: "Approve",
  deny: "Deny",
  approveAll: "Approve All",
  permissionRequest: "Permission",
  planApproval: "Plan Approval",
  shutdownRequest: "Shutdown",
  scopeSession: "scope: session",
  from: "from",
  toolFiltering: "Tool Filtering",
  filterAll: "All",
  filterOrchestration: "Orchestration",
  filterExecution: "Execution",
  filterCustom: "Custom",
  noTools: "No tools available.",
  mailbox: "Mailbox",
  newMessages: "{count} new",
  noMessages: "No messages.",
  markAllRead: "Mark All Read",
  filterCoordinator: "Coordinator",
  filterTeammate: "Teammate",
  filterObserver: "Observer",
  filterUser: "User",
  filterSystem: "System",
  filterProtocol: "Protocol",
  filterMessage: "Messages",
  filterTurn: "Turns",
  noEventsMatchingFilter: "No events matching filter.",
  observerMonitor: "Observer Monitor",
  state: "State",
  rulesChecked: "Rules checked",
  violations: "Violations",
  heartbeat: "Heartbeat",
  lastDigest: "Last digest",
  pauseObserver: "Pause",
  resumeObserver: "Resume",
  secondsAgo: "{n}s ago",
  never: "never",
  noObserver: "No observer configured for this room.",
  observerError: "Error loading observer.",
  kindTaskAssign: "assign",
  kindTaskCancel: "cancel",
  kindPlanSubmit: "plan",
  kindResultSynthesize: "synth",
  kindResultReport: "report",
  kindStatusReport: "status",
  kindClarificationRequest: "clarify",
  kindActivityDigest: "digest",
  kindHeartbeat: "hb",
  kindRuleViolation: "violation",
  kindShutdown: "shutdown",
  kindPermission: "permission",
  kindPlan: "plan-req",
  peerGrants: "Peer Grants",
  noPeerGrants: "No peer routes configured.",
  grantStatus: "Status",
  grantReady: "Ready",
  grantUnavailable: "Unavailable",
  grantNeedsReauth: "Needs Reauthorization",
  replicationHealth: "Replication Health",
  healthy: "Healthy",
  unhealthy: "Unhealthy",
  peersReady: "{ready}/{total} peers ready",
  policyTrace: "Policy Trace",
  noPolicyTrace: "No policy trace available.",
  policyEvents: "Events",
  policyWatermarks: "Watermarks",
  policyThroughSeq: "Through seq",
  linkage: "Room Linkage",
  noLinkage: "No room links found.",
  targetProfile: "Target",
  linkStatus: "Status",
  linkReady: "Ready",
  linkUnavailable: "Unavailable",
  linkNeedsReauth: "Needs Reauthorization",
};

function fmtTs(value: number | null | undefined): string {
  if (!value) return "—";
  try {
    return new Date(value * 1000).toLocaleString();
  } catch {
    return String(value);
  }
}

function memberHandles(members: RoomSummary["members"]): string {
  if (!members || members.length === 0) return "—";
  return members
    .map((m) => {
      const handle = m.handle ? `@${m.handle}` : "";
      const profile = m.profile ? ` (${m.profile})` : "";
      return `${handle}${profile}`.trim() || m.member_id || "?";
    })
    .join(", ");
}

function truncateText(text: string, max = 200): string {
  if (!text) return "";
  if (text.length <= max) return text;
  return text.slice(0, max).trimEnd() + "…";
}

/* ------------------------------------------------------------------ */
/*  Event content extraction (extended for protocol messages)           */
/* ------------------------------------------------------------------ */

interface EventContentResult {
  text: string;
  classification: "protocol" | "message" | "turn" | "system" | "unknown";
  protocolSubtype?: string;
}

function eventContent(ev: RoomEvent): EventContentResult {
  const p = ev.payload || {};
  const text = typeof p.text === "string" ? p.text : "";
  const k = ev.kind;

  if (k === "coordinator.task_assign") {
    const target = typeof p.target_handle === "string" ? p.target_handle : "?";
    const summary = typeof p.task_summary === "string" ? p.task_summary : text;
    return { text: `→ @${target}: ${summary}`, classification: "protocol", protocolSubtype: "task_assign" };
  }
  if (k === "coordinator.task_cancel") {
    return { text: `✕ cancel task=${p.task_id ?? "?"}`, classification: "protocol", protocolSubtype: "task_cancel" };
  }
  if (k === "coordinator.plan_submit") {
    return { text: `[plan] ${p.plan_title ?? ""} (${p.step_count ?? 0} steps)`, classification: "protocol", protocolSubtype: "plan_submit" };
  }
  if (k === "coordinator.result_synthesize") {
    return { text: `[synthesis] ${text}`, classification: "protocol", protocolSubtype: "result_synthesize" };
  }
  if (k === "teammate.result_report") {
    const from = typeof p.from_handle === "string" ? p.from_handle : "?";
    return { text: `← @${from}: ${text}`, classification: "protocol", protocolSubtype: "result_report" };
  }
  if (k === "teammate.status_report") {
    return { text: `[status] ${p.status ?? ""}: ${text}`, classification: "protocol", protocolSubtype: "status_report" };
  }
  if (k === "teammate.clarification_request") {
    return { text: `[clarify] ${text}`, classification: "protocol", protocolSubtype: "clarification_request" };
  }
  if (k === "observer.activity_digest") {
    return { text: `[digest] turn=${p.turn_id ?? "?"} rules=${p.rules_checked ?? 0}`, classification: "protocol", protocolSubtype: "activity_digest" };
  }
  if (k === "observer.heartbeat") {
    return { text: `[hb] ack=${p.ack_turn ?? "?"}`, classification: "protocol", protocolSubtype: "heartbeat" };
  }
  if (k === "observer.rule_violation") {
    return { text: `[violation] ${p.rule_id ?? "?"}: ${p.description ?? ""}`, classification: "protocol", protocolSubtype: "rule_violation" };
  }
  if (k.startsWith("heartbeat.")) {
    return { text: `[hb] ${p.from ?? "?"}: ${p.status ?? "ok"}`, classification: "protocol", protocolSubtype: "heartbeat" };
  }
  if (k.startsWith("shutdown.")) {
    return { text: `[shutdown] ${p.reason ?? text}`, classification: "protocol", protocolSubtype: "shutdown" };
  }
  if (k.startsWith("permission.")) {
    return { text: `[permission] ${p.tool_name ?? ""}: ${p.decision ?? "pending"}`, classification: "protocol", protocolSubtype: "permission" };
  }
  if (k.startsWith("plan.")) {
    return { text: `[plan] ${p.decision ?? "pending"}: ${text}`, classification: "protocol", protocolSubtype: "plan" };
  }

  switch (k) {
    case "message.user":
      return { text, classification: "message" };
    case "message.member": {
      const member = typeof p.member_id === "string" ? p.member_id : "?";
      return { text: `@${member}: ${text}`, classification: "message" };
    }
    case "turn.requested":
      return { text: `[dispatch] member=${p.member_id ?? "?"} round=${p.round_index ?? "?"}`, classification: "turn" };
    case "turn.settled": {
      const passed = p.passed ? " (pass)" : "";
      return { text: `[settled] member=${p.member_id ?? "?"} round=${p.round_index ?? "?"}${passed}`, classification: "turn" };
    }
    case "turn.cancelled":
      return { text: `[cancelled] ${p.reason ?? ""}`, classification: "turn" };
    case "room.activity":
      return { text: `[${p.status ?? "activity"}] ${p.reason_code ?? ""}`, classification: "system" };
    default:
      return { text: `[${k}] ${text}`, classification: "unknown" };
  }
}

/* ------------------------------------------------------------------ */
/*  Event filtering                                                     */
/* ------------------------------------------------------------------ */

function filterEvents(events: RoomEvent[], actorFilter: string, kindFilter: string): RoomEvent[] {
  return events.filter((ev) => {
    if (actorFilter !== "all") {
      if (actorFilter === "coordinator" && !ev.kind.startsWith("coordinator.")) return false;
      if (actorFilter === "teammate" && !ev.kind.startsWith("teammate.")) return false;
      if (actorFilter === "observer" && !ev.kind.startsWith("observer.")) return false;
      if (actorFilter === "user" && ev.kind !== "message.user") return false;
      if (actorFilter === "system" && ev.kind !== "room.activity" && !ev.kind.startsWith("room.")
        && ev.actor?.kind !== "system" && ev.actor?.kind !== "gateway") return false;
    }
    if (kindFilter !== "all") {
      const c = eventContent(ev);
      if (kindFilter === "protocol" && c.classification !== "protocol") return false;
      if (kindFilter === "message" && c.classification !== "message") return false;
      if (kindFilter === "turn" && c.classification !== "turn") return false;
    }
    return true;
  });
}

/* ------------------------------------------------------------------ */
/*  Color mappings & helpers                                            */
/* ------------------------------------------------------------------ */

const EVENT_ROW_COLOR: Record<string, string> = {
  protocol: "border-l-[3px] border-l-[--color-primary] pl-2",
  message: "border-l-[3px] border-l-blue-600 dark:border-l-blue-400 pl-2",
  turn: "border-l-[3px] border-l-[--color-muted] pl-2",
  system: "border-l-[3px] border-l-[--color-muted] pl-2",
  unknown: "border-l-[3px] border-l-[--color-muted] pl-2",
};

const PROTOCOL_TEXT_COLOR: Record<string, string> = {
  task_assign: "text-[--color-primary]",
  task_cancel: "text-[--color-warning]",
  plan_submit: "text-[--color-accent]",
  result_synthesize: "text-[--color-primary]",
  result_report: "text-[--color-success]",
  status_report: "text-[--color-success]",
  clarification_request: "text-[--color-warning]",
  activity_digest: "text-[--color-warning]",
  heartbeat: "text-[--color-muted]",
  rule_violation: "text-[--color-destructive]",
  shutdown: "text-[--color-destructive]",
  permission: "text-[--color-warning]",
  plan: "text-[--color-accent]",
};

const ROLE_ICON: Record<RoomRoleKind, typeof Crown> = {
  coordinator: Crown, teammate: UserIcon, observer: Eye, team_lead: Shield,
};

const OBSERVER_STATE_TONE: Record<ObserverState, "success" | "default" | "destructive" | "warning" | "secondary"> = {
  armed: "success", delivering: "default", denied: "destructive", retired: "secondary", stopped: "secondary", blocked: "warning",
};

const OBSERVER_STATE_ORDER: ObserverState[] = ["armed", "delivering", "denied", "retired", "stopped", "blocked"];

function getProtocolKindColor(kind: string): string {
  if (kind.startsWith("coordinator.")) return "text-[--color-primary]";
  if (kind.startsWith("teammate.")) return "text-[--color-success]";
  if (kind.startsWith("observer.")) return "text-[--color-warning]";
  if (kind.startsWith("heartbeat.")) return "text-[--color-muted]";
  if (kind.startsWith("shutdown.")) return "text-[--color-destructive]";
  if (kind.startsWith("permission.")) return "text-[--color-warning]";
  if (kind.startsWith("plan.")) return "text-[--color-accent]";
  return "text-[--color-muted]";
}

function getProtocolKindLabel(kind: string, L: Record<string, string>): string {
  const m: Record<string, string> = {
    "coordinator.task_assign": L.kindTaskAssign, "coordinator.task_cancel": L.kindTaskCancel,
    "coordinator.plan_submit": L.kindPlanSubmit, "coordinator.result_synthesize": L.kindResultSynthesize,
    "teammate.result_report": L.kindResultReport, "teammate.status_report": L.kindStatusReport,
    "teammate.clarification_request": L.kindClarificationRequest,
    "observer.activity_digest": L.kindActivityDigest, "observer.heartbeat": L.kindHeartbeat,
    "observer.rule_violation": L.kindRuleViolation,
  };
  if (m[kind]) return m[kind];
  if (kind.startsWith("heartbeat.")) return L.kindHeartbeat;
  if (kind.startsWith("shutdown.")) return L.kindShutdown;
  if (kind.startsWith("permission.")) return L.kindPermission;
  if (kind.startsWith("plan.")) return L.kindPlan;
  return kind.split(".").pop() ?? kind;
}

function deriveStageLabel(topology: RoomTopologyResponse | null): string | null {
  if (!topology) return null;
  const coordinator = topology.members.find((m) => m.role === "coordinator");
  if (!coordinator) return null;
  const active = topology.members.filter((m) => m.role === "teammate" && m.current_task !== null);
  if (active.length === 0) return `@${coordinator.handle}`;
  return `@${coordinator.handle} → ${active.map((m) => `@${m.handle}`).join(", ")}`;
}

export default function RoomsPage() {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const { toast, showToast } = useToast();

  const [rooms, setRooms] = useState<RoomSummary[] | null>(null);
  const [showDisbanded, setShowDisbanded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RoomDetailResponse | null>(null);
  const [log, setLog] = useState<RoomLogResponse | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [topology, setTopology] = useState<RoomTopologyResponse | null>(null);
  const [topologyLoading, setTopologyLoading] = useState(false);
  const [topologyError, setTopologyError] = useState<string | null>(null);

  const [pendingActions, setPendingActions] = useState<PendingAction[] | null>(null);
  const [pendingActionsLoading, setPendingActionsLoading] = useState(false);
  const [pendingActionsError, setPendingActionsError] = useState<string | null>(null);
  const [busyActionId, setBusyActionId] = useState<string | null>(null);

  const [observerStatus, setObserverStatus] = useState<ObserverStatus | null>(null);
  const [observerLoading, setObserverLoading] = useState(false);
  const [observerError, setObserverError] = useState<string | null>(null);
  const [observerBusy, setObserverBusy] = useState(false);

  const [peerGrants, setPeerGrants] = useState<PeerGrant[] | null>(null);
  const [peerGrantsLoading, setPeerGrantsLoading] = useState(false);
  const [peerGrantsError, setPeerGrantsError] = useState<string | null>(null);

  const [replicationHealth, setReplicationHealth] = useState<ReplicationHealthResponse | null>(null);
  const [replicationHealthLoading, setReplicationHealthLoading] = useState(false);
  const [replicationHealthError, setReplicationHealthError] = useState<string | null>(null);

  const [policyTrace, setPolicyTrace] = useState<PolicyTraceResponse | null>(null);
  const [policyTraceLoading, setPolicyTraceLoading] = useState(false);
  const [policyTraceError, setPolicyTraceError] = useState<string | null>(null);

  const [expandedMemberId, setExpandedMemberId] = useState<string | null>(null);
  const [actorFilter, setActorFilter] = useState("all");
  const [kindFilter, setKindFilter] = useState("all");

  const loadList = useCallback(() => {
    let cancelled = false;
    setLoadingList(true);
    api.listRooms(showDisbanded)
      .then((res) => {
        if (cancelled) return;
        setRooms(res.rooms);
        setSelected((prev) => {
          if (prev && res.rooms.some((r) => r.room_id === prev)) return prev;
          return res.rooms.length > 0 ? res.rooms[0].room_id : null;
        });
      })
      .catch(() => !cancelled && showToast(t.common.loading, "error"))
      .finally(() => !cancelled && setLoadingList(false));
    return () => { cancelled = true; };
  }, [showDisbanded, showToast, t]);

  useEffect(() => loadList(), [loadList]);

  const loadDetail = useCallback((roomId: string) => {
    let cancelled = false;
    setLoadingDetail(true);
    setDetail(null);
    setLog(null);
    Promise.all([api.getRoom(roomId), api.getRoomLog(roomId, 0, 100)])
      .then(([d, lg]) => { if (!cancelled) { setDetail(d); setLog(lg); } })
      .catch(() => !cancelled && showToast(t.common.loading, "error"))
      .finally(() => !cancelled && setLoadingDetail(false));
    return () => { cancelled = true; };
  }, [showToast, t]);

  useEffect(() => {
    if (selected) return loadDetail(selected);
    setDetail(null);
    setLog(null);
  }, [selected, loadDetail]);

  const loadTopology = useCallback((roomId: string) => {
    let cancelled = false;
    setTopologyLoading(true);
    setTopologyError(null);
    api.getRoomTopology(roomId)
      .then((res) => { if (!cancelled) setTopology(res); })
      .catch((err) => { if (!cancelled) setTopologyError(String(err)); })
      .finally(() => { if (!cancelled) setTopologyLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const loadPendingActions = useCallback((roomId: string) => {
    let cancelled = false;
    setPendingActionsLoading(true);
    setPendingActionsError(null);
    api.getRoomPendingActions(roomId)
      .then((res) => { if (!cancelled) setPendingActions(res.actions); })
      .catch((err) => { if (!cancelled) setPendingActionsError(String(err)); })
      .finally(() => { if (!cancelled) setPendingActionsLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const loadObserverStatus = useCallback((roomId: string) => {
    let cancelled = false;
    setObserverLoading(true);
    setObserverError(null);
    api.getObserverStatus(roomId)
      .then((res) => { if (!cancelled) setObserverStatus(res); })
      .catch((err) => { if (!cancelled) setObserverError(String(err)); })
      .finally(() => { if (!cancelled) setObserverLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const loadPeerGrants = useCallback((roomId: string) => {
    let cancelled = false;
    setPeerGrantsLoading(true);
    setPeerGrantsError(null);
    api.getRoomPeerGrants(roomId)
      .then((res) => { if (!cancelled) setPeerGrants(res.peer_grants); })
      .catch((err) => { if (!cancelled) setPeerGrantsError(String(err)); })
      .finally(() => { if (!cancelled) setPeerGrantsLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const loadReplicationHealth = useCallback((roomId: string) => {
    let cancelled = false;
    setReplicationHealthLoading(true);
    setReplicationHealthError(null);
    api.getRoomReplicationHealth(roomId)
      .then((res) => { if (!cancelled) setReplicationHealth(res); })
      .catch((err) => { if (!cancelled) setReplicationHealthError(String(err)); })
      .finally(() => { if (!cancelled) setReplicationHealthLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const loadPolicyTrace = useCallback((roomId: string) => {
    let cancelled = false;
    setPolicyTraceLoading(true);
    setPolicyTraceError(null);
    api.getRoomPolicyTrace(roomId)
      .then((res) => { if (!cancelled) setPolicyTrace(res); })
      .catch((err) => { if (!cancelled) setPolicyTraceError(String(err)); })
      .finally(() => { if (!cancelled) setPolicyTraceLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selected) { setTopology(null); setPendingActions(null); setObserverStatus(null); setPeerGrants(null); setReplicationHealth(null); setPolicyTrace(null); return; }
    const c1 = loadTopology(selected);
    const c2 = loadPendingActions(selected);
    const c3 = loadObserverStatus(selected);
    const c4 = loadPeerGrants(selected);
    const c5 = loadReplicationHealth(selected);
    const c6 = loadPolicyTrace(selected);
    return () => { c1(); c2(); c3(); c4(); c5(); c6(); };
  }, [selected, loadTopology, loadPendingActions, loadObserverStatus, loadPeerGrants, loadReplicationHealth, loadPolicyTrace]);

  const loadMore = useCallback(() => {
    if (!selected || !log) return;
    api.getRoomLog(selected, log.cursor, 100)
      .then((more) => setLog((prev) => prev ? { ...more, events: [...prev.events, ...more.events] } : more))
      .catch(() => showToast(t.common.loading, "error"));
  }, [selected, log, showToast, t]);

  const handleApproveAction = useCallback(async (actionId: string) => {
    if (!selected) return;
    setBusyActionId(actionId);
    try {
      await api.approveRoomAction(selected, actionId);
      setPendingActions((prev) => prev ? prev.filter((a) => a.action_id !== actionId) : null);
      showToast("Action approved", "success");
    } catch (err) { showToast(String(err), "error"); }
    finally { setBusyActionId(null); }
  }, [selected, showToast]);

  const handleDenyAction = useCallback(async (actionId: string) => {
    if (!selected) return;
    setBusyActionId(actionId);
    try {
      await api.denyRoomAction(selected, actionId);
      setPendingActions((prev) => prev ? prev.filter((a) => a.action_id !== actionId) : null);
      showToast("Action denied", "success");
    } catch (err) { showToast(String(err), "error"); }
    finally { setBusyActionId(null); }
  }, [selected, showToast]);

  const handleApproveAll = useCallback(async () => {
    if (!selected || !pendingActions) return;
    setBusyActionId("__approve_all__");
    try {
      for (const action of pendingActions) { await api.approveRoomAction(selected, action.action_id); }
      setPendingActions([]);
      showToast("All actions approved", "success");
    } catch (err) { showToast(String(err), "error"); }
    finally { setBusyActionId(null); }
  }, [selected, pendingActions, showToast]);

  const handlePauseObserver = useCallback(async () => {
    if (!selected) return;
    setObserverBusy(true);
    try { await api.pauseObserver(selected); loadObserverStatus(selected); }
    catch (err) { showToast(String(err), "error"); }
    finally { setObserverBusy(false); }
  }, [selected, showToast, loadObserverStatus]);

  const handleResumeObserver = useCallback(async () => {
    if (!selected) return;
    setObserverBusy(true);
    try { await api.resumeObserver(selected); loadObserverStatus(selected); }
    catch (err) { showToast(String(err), "error"); }
    finally { setObserverBusy(false); }
  }, [selected, showToast, loadObserverStatus]);

  const driverLabel = useMemo(() => {
    const s = detail?.driver_status;
    if (!s) return L.idle;
    if (s.blocked) return L.blocked;
    if (s.working) return L.working;
    if (s.running) return L.running;
    return L.idle;
  }, [detail, L]);

  const filteredEvents = useMemo(
    () => (log ? filterEvents(log.events, actorFilter, kindFilter) : []),
    [log, actorFilter, kindFilter],
  );

  return (
    <div className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-4 p-4 lg:grid-cols-[320px_1fr]">
      {/* LeftPanel */}
      <div className="space-y-4">
        <Card className="rounded-none">
          <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Home className="h-4 w-4 text-muted-foreground" />
                {L.title}
              </CardTitle>
              <CardDescription className="text-xs">{L.description}</CardDescription>
            </div>
            <Button ghost size="xs" className="text-muted-foreground hover:text-foreground"
              onClick={() => loadList()} disabled={loadingList} aria-label={t.common.refresh}>
              <RotateCw />
            </Button>
          </CardHeader>
          <CardContent className="space-y-2">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={showDisbanded} onChange={(e) => setShowDisbanded(e.target.checked)} />
              {L.showDisbanded}
            </label>
            {loadingList ? (
              <div className="flex min-h-[120px] items-center justify-center"><Spinner /></div>
            ) : !rooms || rooms.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">{L.empty}</p>
            ) : (
              <ul className="space-y-1">
                {rooms.map((r) => (
                  <li key={r.room_id}>
                    <button type="button" onClick={() => setSelected(r.room_id)}
                      className={`w-full border px-3 py-2 text-left text-sm transition-colors ${
                        selected === r.room_id ? "border-ring bg-accent" : "border-input hover:bg-accent/50"
                      } ${r.disbanded_at ? "opacity-60" : ""}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium">{r.name}</span>
                        <span className="flex items-center gap-1 text-xs text-muted-foreground">
                          <Users className="h-3 w-3" />{r.members.length}
                        </span>
                      </div>
                      <div className="truncate font-mono text-xs text-muted-foreground">{r.room_id}</div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {selected && (
          <TeamTopologyCard topology={topology} loading={topologyLoading}
            error={topologyError} onRetry={() => loadTopology(selected)} />
        )}
        {selected && (
          <PendingActionsCard actions={pendingActions} loading={pendingActionsLoading}
            error={pendingActionsError} onApprove={handleApproveAction} onDeny={handleDenyAction}
            onApproveAll={handleApproveAll} onRetry={() => loadPendingActions(selected)}
            busyActionId={busyActionId} />
        )}
      </div>

      {/* RightPanel */}
      <div className="space-y-4">
        {!selected ? (
          <Card className="rounded-none">
            <CardContent className="py-12 text-center text-sm text-muted-foreground">{L.selectRoom}</CardContent>
          </Card>
        ) : loadingDetail ? (
          <Card className="rounded-none">
            <CardContent className="flex min-h-[200px] items-center justify-center"><Spinner /></CardContent>
          </Card>
        ) : detail ? (
          <>
            <Card className="rounded-none">
              <CardHeader className="space-y-0">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Home className="h-4 w-4 text-muted-foreground" />
                  {detail.room.name}
                  <span className="font-mono text-xs font-normal text-muted-foreground">{detail.room.room_id}</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <TurnProgressBar currentTurn={topology?.current_turn ?? null}
                  currentRound={topology?.current_round ?? null}
                  maxRounds={topology?.max_rounds ?? null}
                  stageLabel={deriveStageLabel(topology)} />
                <div>
                  <span className="text-xs uppercase text-muted-foreground">{L.members} ({detail.room.members.length})</span>
                  <p className="mt-1">{memberHandles(detail.room.members)}</p>
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
                  <MetaCell label={L.driver} value={driverLabel} />
                  <MetaCell label={L.revision} value={String(detail.room.revision)} />
                  <MetaCell label={L.latestSeq} value={String(detail.room.latest_seq)} />
                  <MetaCell label={L.authorityEpoch} value={String(detail.room.authority_epoch)} />
                  <MetaCell label={L.created} value={fmtTs(detail.room.created_at)} />
                  <MetaCell label={L.updated} value={fmtTs(detail.room.updated_at)} />
                  {detail.room.disbanded_at ? <MetaCell label={L.disbanded} value={fmtTs(detail.room.disbanded_at)} /> : null}
                  <MetaCell label={L.authorityGateway} value={detail.room.authority_gateway_id ?? "—"} />
                </dl>
                {topology?.members.map((member) => (
                  <MemberDetailPanel key={member.member_id} roomId={selected} member={member}
                    expanded={expandedMemberId === member.member_id}
                    onToggle={() => setExpandedMemberId((prev) => prev === member.member_id ? null : member.member_id)} />
                ))}
              </CardContent>
            </Card>

            <Card className="rounded-none">
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <CardTitle className="text-sm">{L.eventLog}</CardTitle>
                <Button ghost size="xs" className="text-muted-foreground hover:text-foreground"
                  onClick={() => selected && loadDetail(selected)} aria-label={t.common.refresh}>
                  <RotateCw />
                </Button>
              </CardHeader>
              <EventLogFilterBar actorFilter={actorFilter} onActorFilterChange={setActorFilter}
                kindFilter={kindFilter} onKindFilterChange={setKindFilter} />
              <CardContent>
                {!log || filteredEvents.length === 0 ? (
                  <p className="py-6 text-center text-sm text-muted-foreground">
                    {log && log.events.length > 0 && filteredEvents.length === 0 ? L.noEventsMatchingFilter : L.noEvents}
                  </p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-muted-foreground">
                          <th className="py-1 pr-3 font-medium whitespace-nowrap">{L.seq}</th>
                          <th className="py-1 pr-3 font-medium whitespace-nowrap">{L.kind}</th>
                          <th className="py-1 pr-3 font-medium">{L.content}</th>
                          <th className="py-1 pr-3 font-medium whitespace-nowrap">{L.created}</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {filteredEvents.map((ev) => {
                          const content = eventContent(ev);
                          const lineColor = EVENT_ROW_COLOR[content.classification] ?? EVENT_ROW_COLOR.unknown;
                          const textColor = content.protocolSubtype
                            ? PROTOCOL_TEXT_COLOR[content.protocolSubtype] ?? ""
                            : content.classification === "message" ? "text-blue-600 dark:text-blue-400"
                            : content.classification === "protocol" ? "text-[--color-primary]"
                            : "text-muted-foreground";
                          return (
                            <tr key={ev.seq} className={`border-t border-input/50 align-top ${lineColor}`}>
                              <td className="py-1 pr-3 tabular-nums whitespace-nowrap">{ev.seq}</td>
                              <td className="py-1 pr-3 whitespace-nowrap">
                                {content.protocolSubtype
                                  ? <Badge tone="outline" className="text-[10px] font-mono">{content.protocolSubtype}</Badge>
                                  : <span className="text-muted-foreground">{ev.kind}</span>}
                              </td>
                              <td className={`py-1 pr-3 max-w-[400px] ${textColor}`}>
                                {truncateText(content.text, 300) || "—"}
                              </td>
                              <td className="py-1 pr-3 text-muted-foreground whitespace-nowrap">{fmtTs(ev.created_at)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    {log.has_more && (
                      <div className="mt-3 flex justify-center">
                        <Button ghost size="xs" onClick={loadMore}>{L.loadMore}</Button>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>

            <LiveObserverCard observerStatus={observerStatus} loading={observerLoading}
              error={observerError} onPause={handlePauseObserver} onResume={handleResumeObserver}
              onRetry={() => loadObserverStatus(selected)} busy={observerBusy} />

            <PeerGrantsCard peerGrants={peerGrants} loading={peerGrantsLoading}
              error={peerGrantsError} onRetry={() => loadPeerGrants(selected)} />
            <LinkageCard peerGrants={peerGrants} loading={peerGrantsLoading}
              error={peerGrantsError} onRetry={() => loadPeerGrants(selected)} />
            <ReplicationHealthCard health={replicationHealth} loading={replicationHealthLoading}
              error={replicationHealthError} onRetry={() => loadReplicationHealth(selected)} />
            <PolicyTraceCard policyTrace={policyTrace} loading={policyTraceLoading}
              error={policyTraceError} onRetry={() => loadPolicyTrace(selected)} />
          </>
        ) : null}
      </div>
      <Toast toast={toast} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Sub-components                                                      */
/* ------------------------------------------------------------------ */

function MetaCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="uppercase text-muted-foreground">{label}</dt>
      <dd className="truncate">{value}</dd>
    </div>
  );
}

function TurnProgressBar({ currentTurn, currentRound, maxRounds, stageLabel }: {
  currentTurn: number | null; currentRound: number | null;
  maxRounds: number | null; stageLabel: string | null;
}) {
  if (currentTurn === null && currentRound === null) return null;
  const pct = maxRounds && currentRound !== null ? Math.min(100, Math.max(0, (currentRound / maxRounds) * 100)) : 0;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        {currentTurn !== null && <span>Turn {currentTurn}</span>}
        {currentRound !== null && maxRounds !== null && <span>Round {currentRound}/{maxRounds}</span>}
        {stageLabel && <span className="font-mono text-[10px] truncate">[{stageLabel}]</span>}
      </div>
      {maxRounds !== null && maxRounds > 0 && (
        <div className="h-1.5 rounded-full bg-[--color-muted]/40 overflow-hidden">
          <div className="h-full rounded-full bg-[--color-primary] transition-[width] duration-500 ease-out"
            style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

function TeamTopologyCard({ topology, loading, error, onRetry }: {
  topology: RoomTopologyResponse | null; loading: boolean; error: string | null; onRetry: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const sorted = topology?.members ? [...topology.members].sort((a, b) => {
    const order: Record<RoomRoleKind, number> = { coordinator: 0, team_lead: 1, teammate: 2, observer: 3 };
    return (order[a.role] ?? 99) - (order[b.role] ?? 99);
  }) : [];

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Network className="h-4 w-4 text-muted-foreground" />{L.teamTopology}
          </CardTitle>
          <CardDescription className="text-xs">
            {topology && topology.members.length > 0 ? `${topology.members.length} member${topology.members.length !== 1 ? "s" : ""}` : ""}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-1">
        {loading && <div className="flex min-h-[80px] items-center justify-center"><Spinner /></div>}
        {!loading && error && (
          <div className="space-y-2 py-2 text-center">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && sorted.length === 0 && (
          <p className="py-6 text-center text-xs text-muted-foreground">{L.noTopology}</p>
        )}
        {!loading && !error && sorted.length > 0 && (
          <div className="space-y-0.5">
            {sorted.map((member, idx) => {
              const Icon = ROLE_ICON[member.role] ?? UserIcon;
              const isLast = idx === sorted.length - 1;
              const isCoordinator = member.role === "coordinator";
              return (
                <div key={member.member_id}>
                  <div className={`flex items-center gap-1.5 py-1 px-1.5 ${isCoordinator ? "bg-[--color-primary]/5 border-l-[3px] border-l-[--color-primary]" : ""}`}>
                    {!isCoordinator && <span className="text-muted-foreground/40 font-mono text-xs select-none w-4 text-center">{isLast ? "└" : "├"}</span>}
                    <Icon className={`h-3.5 w-3.5 shrink-0 ${
                      member.role === "coordinator" ? "text-[--color-primary]" : member.role === "teammate" ? "text-[--color-success]"
                      : member.role === "observer" ? "text-[--color-warning]" : "text-[--color-accent]"}`} />
                    <span className="text-xs font-medium truncate">
                      {member.role === "coordinator" ? L.roleCoordinator : member.role === "teammate" ? L.roleTeammate
                      : member.role === "observer" ? L.roleObserver : L.roleTeamLead}
                    </span>
                    <span className="font-mono text-xs text-muted-foreground truncate">@{member.handle}</span>
                    {member.role === "observer" && member.observer_state && (
                      <Badge tone={OBSERVER_STATE_TONE[member.observer_state] ?? "outline"} className="text-[10px] ml-auto shrink-0">
                        {member.observer_state}
                      </Badge>
                    )}
                  </div>
                  {member.role === "teammate" && member.current_task && (
                    <div className="ml-6 pl-1.5 pb-0.5">
                      <span className="text-[10px] text-muted-foreground font-mono truncate block">
                        ↳ {member.current_task.slice(0, 40)}{member.current_task.length > 40 ? "…" : ""}
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatActionDetail(action: PendingAction, L: Record<string, string>): string[] {
  const d = action.detail;
  const lines: string[] = [];
  if (action.kind === "permission") {
    lines.push(`tool: ${typeof d.tool_name === "string" ? d.tool_name : "?"}`);
    lines.push(`${L.scopeSession}: ${typeof d.scope === "string" ? d.scope : "?"}`);
  } else if (action.kind === "plan_approval") {
    const title = typeof d.task_title === "string" ? d.task_title : "";
    if (title) lines.push(`task: ${title}`);
    const steps = typeof d.step_count === "number" ? d.step_count : null;
    if (steps !== null) lines.push(`steps: ${steps}`);
  } else if (action.kind === "shutdown") {
    const reason = typeof d.reason === "string" ? d.reason : "";
    if (reason) lines.push(`reason: ${reason}`);
  }
  return lines;
}

function PendingActionsCard({ actions, loading, error, onApprove, onDeny, onApproveAll, onRetry, busyActionId }: {
  actions: PendingAction[] | null; loading: boolean; error: string | null;
  onApprove: (id: string) => void; onDeny: (id: string) => void; onApproveAll: () => void;
  onRetry: () => void; busyActionId: string | null;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const ACTION_ICON: Record<PendingActionKind, typeof Key> = { permission: Key, plan_approval: ClipboardList, shutdown: Power };
  const ACTION_LABEL: Record<PendingActionKind, string> = { permission: L.permissionRequest, plan_approval: L.planApproval, shutdown: L.shutdownRequest };
  const count = actions?.length ?? 0;

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Key className="h-4 w-4 text-muted-foreground" />{L.pendingActions}
          {count > 0 && <Badge tone="warning" className="text-[10px]">{count}</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {loading && <div className="flex min-h-[60px] items-center justify-center"><Spinner /></div>}
        {!loading && error && (
          <div className="space-y-2 py-2 text-center">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && count === 0 && (
          <p className="py-4 text-center text-xs text-muted-foreground">{L.noPendingActions}</p>
        )}
        {!loading && !error && actions && actions.length > 0 && (
          <>
            <div className="divide-y divide-[--color-border]">
              {actions.map((action) => {
                const Icon = ACTION_ICON[action.kind] ?? Key;
                const isBusy = busyActionId === action.action_id;
                const detailLines = formatActionDetail(action, L);
                return (
                  <div key={action.action_id} className="py-2 space-y-1.5">
                    <div className="flex items-center gap-1.5">
                      <Icon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                      <span className="text-xs font-medium">{ACTION_LABEL[action.kind] ?? action.kind}</span>
                      <span className="text-[10px] text-muted-foreground font-mono">{L.from} @{action.from_handle}</span>
                      <span className="text-[10px] text-muted-foreground ml-auto">{fmtTs(action.created_at)}</span>
                    </div>
                    <p className="text-xs text-muted-foreground pl-5">{action.description}</p>
                    {detailLines.length > 0 && (
                      <div className="pl-5 space-y-0.5">
                        {detailLines.map((line, i) => (
                          <span key={i} className="text-[10px] text-muted-foreground/70 font-mono block">{line}</span>
                        ))}
                      </div>
                    )}
                    <div className="flex items-center gap-1.5 pl-5">
                      <Button ghost size="xs" className="text-[--color-success] hover:bg-[--color-success]/10 h-6 text-[11px]"
                        disabled={isBusy} onClick={() => onApprove(action.action_id)}
                        prefix={isBusy ? <Spinner className="h-3 w-3" /> : undefined}>{L.approve}</Button>
                      <Button ghost size="xs" className="text-[--color-destructive] hover:bg-[--color-destructive]/10 h-6 text-[11px]"
                        disabled={isBusy} onClick={() => onDeny(action.action_id)}>{L.deny}</Button>
                    </div>
                  </div>
                );
              })}
            </div>
            {actions.length > 1 && (
              <div className="pt-1">
                <Button ghost size="xs" className="text-[--color-primary] w-full"
                  disabled={busyActionId !== null} onClick={onApproveAll}>{L.approveAll} ({actions.length})</Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function MailboxMessageRow({ message, L }: { message: MailboxMessage; L: Record<string, string> }) {
  const kindColor = getProtocolKindColor(message.kind);
  return (
    <div className={`py-1 px-1.5 text-[10px] ${!message.read ? "bg-[--color-primary]/5" : ""}`}>
      <div className="flex items-center gap-1">
        {!message.read && <span className="w-1.5 h-1.5 rounded-full bg-[--color-primary] shrink-0" />}
        <span className={`font-mono ${kindColor} shrink-0`}>{getProtocolKindLabel(message.kind, L)}</span>
        <span className="text-muted-foreground">@{message.from_handle}</span>
        <span className="text-muted-foreground/50 ml-auto shrink-0">{fmtTs(message.created_at)}</span>
      </div>
      <p className="truncate mt-0.5">{message.summary}</p>
    </div>
  );
}

const ALL_TOOLS = [
  { name: "Bash", layer: "execution" as const }, { name: "Read", layer: "execution" as const },
  { name: "Write", layer: "execution" as const }, { name: "Grep", layer: "execution" as const },
  { name: "WebSearch", layer: "execution" as const }, { name: "Browser", layer: "execution" as const },
  { name: "bot_room", layer: "orchestration" as const }, { name: "delegation", layer: "orchestration" as const },
  { name: "todo", layer: "orchestration" as const }, { name: "clarify", layer: "orchestration" as const },
];

function ToolCheckboxList({ filterLayer, L }: { filterLayer: string; L: Record<string, string> }) {
  const filtered = filterLayer === "all" ? ALL_TOOLS : ALL_TOOLS.filter((t) => t.layer === filterLayer);
  if (filtered.length === 0) return <p className="text-[10px] text-muted-foreground py-1">{L.noTools}</p>;
  return (
    <div className="max-h-28 overflow-y-auto border border-[--color-border] bg-[--color-muted]/5 p-1">
      {filtered.map((tool) => (
        <label key={tool.name} className="flex cursor-pointer items-center gap-1.5 px-1.5 py-0.5 text-[10px] hover:bg-[--color-muted]/20">
          <input type="checkbox" className="accent-[--color-primary] h-3 w-3" defaultChecked={true} />
          <span className="font-mono truncate">{tool.name}</span>
        </label>
      ))}
    </div>
  );
}

const TOOL_FILTER_OPTIONS = [
  { value: "all", label: "All" }, { value: "orchestration", label: "Orchestration" },
  { value: "execution", label: "Execution" }, { value: "custom", label: "Custom" },
];

function MemberDetailPanel({ roomId, member, expanded, onToggle }: {
  roomId: string; member: RoomMemberRole; expanded: boolean; onToggle: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const [toolFilter, setToolFilter] = useState<string>("all");
  const [mailbox, setMailbox] = useState<MailboxResponse | null>(null);
  const [mailboxLoading, setMailboxLoading] = useState(false);
  const Icon = ROLE_ICON[member.role] ?? UserIcon;

  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    setMailboxLoading(true);
    api.getMemberMailbox(roomId, member.member_id)
      .then((res) => { if (!cancelled) setMailbox(res); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setMailboxLoading(false); });
    return () => { cancelled = true; };
  }, [expanded, roomId, member.member_id]);

  return (
    <div className="border-t border-[--color-border]">
      <button type="button" onClick={onToggle}
        className="flex items-center gap-1.5 w-full py-2 px-2 hover:bg-[--color-muted]/20 transition-colors">
        {expanded ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
          : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />}
        <Icon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        <span className="text-xs font-mono">@{member.handle}</span>
        <span className="text-[10px] text-muted-foreground">
          {member.role === "coordinator" ? L.roleCoordinator : member.role === "teammate" ? L.roleTeammate
          : member.role === "observer" ? L.roleObserver : L.roleTeamLead}
        </span>
        {member.observer_state && (
          <Badge tone={OBSERVER_STATE_TONE[member.observer_state]} className="text-[10px] ml-auto">{member.observer_state}</Badge>
        )}
      </button>
      {expanded && (
        <div className="px-2 pb-2 space-y-3">
          <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
            <span className="text-muted-foreground">Profile</span>
            <span className="font-mono">{member.profile}</span>
            {member.current_task && (
              <><span className="text-muted-foreground">Task</span><span className="font-mono truncate">{member.current_task}</span></>
            )}
          </div>
          <div className="space-y-1.5">
            <span className="text-[10px] uppercase text-muted-foreground">{L.toolFiltering}</span>
            <Segmented options={TOOL_FILTER_OPTIONS} value={toolFilter} onChange={(v) => setToolFilter(v as string)}
              className="w-fit max-w-full flex-wrap justify-start self-start" />
            <ToolCheckboxList filterLayer={toolFilter} L={L} />
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[10px] uppercase text-muted-foreground">
                {L.mailbox}
                {mailbox && mailbox.unread_count > 0 && (
                  <Badge tone="warning" className="text-[10px] ml-1">{L.newMessages.replace("{count}", String(mailbox.unread_count))}</Badge>
                )}
              </span>
              {mailbox && mailbox.messages.length > 0 && (
                <Button ghost size="xs" className="text-[10px] h-5" onClick={() => {
                  api.markMailboxRead(roomId, member.member_id).then(() => {
                    if (mailbox) setMailbox({ ...mailbox, messages: mailbox.messages.map((m) => ({ ...m, read: true })), unread_count: 0 });
                  }).catch(() => {});
                }}>{L.markAllRead}</Button>
              )}
            </div>
            {mailboxLoading ? <div className="flex justify-center py-2"><Spinner /></div>
            : !mailbox || mailbox.messages.length === 0 ? <p className="text-[10px] text-muted-foreground py-1">{L.noMessages}</p>
            : (
              <div className="max-h-[200px] overflow-y-auto space-y-1 border border-[--color-border] p-1">
                {mailbox.messages.map((msg) => <MailboxMessageRow key={msg.message_id} message={msg} L={L} />)}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const ACTOR_FILTER_OPTIONS = [
  { value: "all", label: "All" }, { value: "coordinator", label: "Coordinator" },
  { value: "teammate", label: "Teammate" }, { value: "observer", label: "Observer" },
  { value: "user", label: "User" }, { value: "system", label: "System" },
];

const KIND_FILTER_OPTIONS = [
  { value: "all", label: "All" }, { value: "protocol", label: "Protocol" },
  { value: "message", label: "Messages" }, { value: "turn", label: "Turns" },
];

function EventLogFilterBar({ actorFilter, onActorFilterChange, kindFilter, onKindFilterChange }: {
  actorFilter: string; onActorFilterChange: (v: string) => void;
  kindFilter: string; onKindFilterChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2 border-b border-[--color-border]">
      <FilterGroup label="Actor" className="flex items-center gap-1.5">
        <Segmented className="w-fit max-w-full flex-wrap justify-start self-start"
          value={actorFilter} onChange={onActorFilterChange} options={ACTOR_FILTER_OPTIONS} />
      </FilterGroup>
      <FilterGroup label="Kind" className="flex items-center gap-1.5">
        <Segmented className="w-fit max-w-full flex-wrap justify-start self-start"
          value={kindFilter} onChange={onKindFilterChange} options={KIND_FILTER_OPTIONS} />
      </FilterGroup>
    </div>
  );
}

function LiveObserverCard({ observerStatus, loading, error, onPause, onResume, onRetry, busy }: {
  observerStatus: ObserverStatus | null; loading: boolean; error: string | null;
  onPause: () => void; onResume: () => void; onRetry: () => void; busy: boolean;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const [expanded, setExpanded] = useState(false);

  if (!expanded) {
    return (
      <Card className="rounded-none">
        <button type="button" onClick={() => setExpanded(true)} className="w-full">
          <CardHeader className="flex flex-row items-center gap-2 space-y-0 py-3">
            <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
            <Eye className="h-4 w-4 text-muted-foreground shrink-0" />
            <CardTitle className="text-sm">{L.observerMonitor}</CardTitle>
            {loading ? <Spinner className="h-3 w-3 ml-auto" />
            : error ? <span className="text-xs text-destructive ml-auto">{error}</span>
            : observerStatus ? (
              <div className="flex items-center gap-2 ml-auto text-xs text-muted-foreground">
                <span className="flex items-center gap-1">
                  <span className={`w-2 h-2 rounded-full ${
                    observerStatus.state === "armed" ? "bg-[--color-success]" : observerStatus.state === "delivering" ? "bg-[--color-primary]"
                    : observerStatus.state === "blocked" ? "bg-[--color-warning]" : observerStatus.state === "denied" ? "bg-[--color-destructive]"
                    : "bg-[--color-muted]"}`} />
                  {observerStatus.state}
                </span>
                <span>Turn {observerStatus.current_turn}</span>
                <span>{L.rulesChecked}: {observerStatus.rules_checked}</span>
              </div>
            ) : <span className="text-xs text-muted-foreground ml-auto">{L.noObserver}</span>}
          </CardHeader>
        </button>
      </Card>
    );
  }

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => setExpanded(false)} className="text-muted-foreground hover:text-foreground">
            <ChevronDown className="h-4 w-4" />
          </button>
          <Eye className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-sm">{L.observerMonitor}</CardTitle>
        </div>
        <div className="flex items-center gap-1">
          <Button ghost size="xs" disabled={busy || observerStatus?.state === "stopped"} onClick={onPause}
            prefix={<Pause className="h-3 w-3" />}>{L.pauseObserver}</Button>
          <Button ghost size="xs" disabled={busy || observerStatus?.state !== "stopped"} onClick={onResume}
            prefix={<Play className="h-3 w-3" />}>{L.resumeObserver}</Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading && <div className="flex justify-center py-4"><Spinner /></div>}
        {!loading && error && (
          <div className="text-center space-y-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && !observerStatus && (
          <p className="text-xs text-muted-foreground text-center py-4">{L.noObserver}</p>
        )}
        {!loading && !error && observerStatus && (
          <>
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">{L.state}:</span>
                <Badge tone={OBSERVER_STATE_TONE[observerStatus.state] ?? "outline"} className="text-xs">{observerStatus.state}</Badge>
              </div>
              <div className="flex items-center gap-0.5">
                {OBSERVER_STATE_ORDER.map((s) => {
                  const isActive = s === observerStatus.state;
                  const isPast = OBSERVER_STATE_ORDER.indexOf(s) < OBSERVER_STATE_ORDER.indexOf(observerStatus.state);
                  return <div key={s} className={`h-1.5 flex-1 rounded-full ${
                    isActive ? "bg-[--color-primary]" : isPast ? "bg-[--color-muted]" : "bg-[--color-muted]/20"}`} title={s} />;
                })}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <div><span className="text-muted-foreground">Turn / Round</span>
                <p className="font-mono">{observerStatus.current_turn} / {observerStatus.current_round}</p></div>
              <div><span className="text-muted-foreground">{L.rulesChecked}</span>
                <p className="font-mono">{observerStatus.rules_checked}</p></div>
              <div><span className="text-muted-foreground">{L.violations}</span>
                <p className={`font-mono ${observerStatus.violations > 0 ? "text-[--color-destructive]" : ""}`}>{observerStatus.violations}</p></div>
              <div><span className="text-muted-foreground">{L.heartbeat}</span>
                <p className="font-mono">{observerStatus.last_heartbeat_at
                  ? L.secondsAgo.replace("{n}", String(Math.floor(Date.now() / 1000 - observerStatus.last_heartbeat_at)))
                  : L.never}</p></div>
            </div>
            {observerStatus.last_digest && (
              <div className="space-y-1">
                <span className="text-xs text-muted-foreground">{L.lastDigest}</span>
                <p className="text-xs font-mono bg-[--color-muted]/10 p-2 border border-[--color-border] max-h-20 overflow-y-auto whitespace-pre-wrap break-words">
                  {observerStatus.last_digest}</p>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Peer Grants card                                                    */
/* ------------------------------------------------------------------ */

const PEER_STATUS_COLOR: Record<string, string> = {
  ready: "text-[--color-success]",
  unavailable: "text-[--color-destructive]",
  needs_reauthorization: "text-[--color-warning]",
};

function PeerGrantsCard({ peerGrants, loading, error, onRetry }: {
  peerGrants: PeerGrant[] | null; loading: boolean; error: string | null; onRetry: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const count = peerGrants?.length ?? 0;

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Network className="h-4 w-4 text-muted-foreground" />{L.peerGrants}
          {count > 0 && <span className="text-xs text-muted-foreground font-normal">({count})</span>}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading && <div className="flex justify-center py-4"><Spinner /></div>}
        {!loading && error && (
          <div className="text-center space-y-2 py-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && count === 0 && (
          <p className="py-4 text-center text-xs text-muted-foreground">{L.noPeerGrants}</p>
        )}
        {!loading && !error && peerGrants && peerGrants.length > 0 && (
          <div className="divide-y divide-[--color-border]">
            {peerGrants.map((g) => (
              <div key={`${g.room_id}-${g.member_id}`} className="flex items-center gap-2 py-1.5 text-xs">
                <span className="font-mono text-muted-foreground truncate flex-1">{g.member_id}</span>
                <span className="text-muted-foreground/50">{L.grantStatus}:</span>
                <span className={`font-medium ${PEER_STATUS_COLOR[g.status] ?? ""}`}>
                  {g.status === "ready" ? L.grantReady : g.status === "unavailable" ? L.grantUnavailable : g.status === "needs_reauthorization" ? L.grantNeedsReauth : g.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Replication Health card                                            */
/* ------------------------------------------------------------------ */

function ReplicationHealthCard({ health, loading, error, onRetry }: {
  health: ReplicationHealthResponse | null; loading: boolean; error: string | null; onRetry: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Shield className="h-4 w-4 text-muted-foreground" />{L.replicationHealth}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading && <div className="flex justify-center py-4"><Spinner /></div>}
        {!loading && error && (
          <div className="text-center space-y-2 py-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && !health && (
          <p className="py-4 text-center text-xs text-muted-foreground">—</p>
        )}
        {!loading && !error && health && (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <span className={`w-3 h-3 rounded-full ${health.healthy ? "bg-[--color-success]" : "bg-[--color-destructive]"}`} />
              <span className="text-xs font-medium">{health.healthy ? L.healthy : L.unhealthy}</span>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <div className="text-center p-2 bg-[--color-muted]/10 border border-[--color-border]">
                <p className="text-lg font-mono font-bold text-[--color-success]">{health.ready}</p>
                <p className="text-muted-foreground">{L.grantReady}</p>
              </div>
              <div className="text-center p-2 bg-[--color-muted]/10 border border-[--color-border]">
                <p className="text-lg font-mono font-bold text-[--color-destructive]">{health.unavailable}</p>
                <p className="text-muted-foreground">{L.grantUnavailable}</p>
              </div>
              <div className="text-center p-2 bg-[--color-muted]/10 border border-[--color-border]">
                <p className="text-lg font-mono font-bold text-[--color-warning]">{health.needs_reauthorization}</p>
                <p className="text-muted-foreground">{L.grantNeedsReauth}</p>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{L.peersReady.replace("{ready}", String(health.ready)).replace("{total}", String(health.total_peers))}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Policy Trace card                                                  */
/* ------------------------------------------------------------------ */

function PolicyTraceCard({ policyTrace, loading, error, onRetry }: {
  policyTrace: PolicyTraceResponse | null; loading: boolean; error: string | null; onRetry: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const [expanded, setExpanded] = useState(false);

  if (!expanded) {
    return (
      <Card className="rounded-none">
        <button type="button" onClick={() => setExpanded(true)} className="w-full">
          <CardHeader className="flex flex-row items-center gap-2 space-y-0 py-3">
            <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
            <ClipboardList className="h-4 w-4 text-muted-foreground shrink-0" />
            <CardTitle className="text-sm">{L.policyTrace}</CardTitle>
            {loading ? <Spinner className="h-3 w-3 ml-auto" />
            : error ? <span className="text-xs text-destructive ml-auto">{error}</span>
            : policyTrace ? (
              <span className="text-xs text-muted-foreground ml-auto">
                {policyTrace.event_count} {L.policyEvents}
              </span>
            ) : <span className="text-xs text-muted-foreground ml-auto">{L.noPolicyTrace}</span>}
          </CardHeader>
        </button>
      </Card>
    );
  }

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => setExpanded(false)} className="text-muted-foreground hover:text-foreground">
            <ChevronDown className="h-4 w-4" />
          </button>
          <ClipboardList className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-sm">{L.policyTrace}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading && <div className="flex justify-center py-4"><Spinner /></div>}
        {!loading && error && (
          <div className="text-center space-y-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && !policyTrace && (
          <p className="text-xs text-muted-foreground text-center py-4">{L.noPolicyTrace}</p>
        )}
        {!loading && !error && policyTrace && !policyTrace.error && (
          <>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <div><span className="text-muted-foreground">{L.policyThroughSeq}</span>
                <p className="font-mono">{policyTrace.through_seq} / {policyTrace.stopped_through_seq}</p></div>
              <div><span className="text-muted-foreground">{L.policyEvents}</span>
                <p className="font-mono">{policyTrace.event_count}</p></div>
            </div>
            {policyTrace.watermarks && Object.keys(policyTrace.watermarks).length > 0 && (
              <div className="space-y-1">
                <span className="text-xs text-muted-foreground">{L.policyWatermarks}</span>
                <div className="max-h-[120px] overflow-y-auto border border-[--color-border] p-1">
                  {Object.entries(policyTrace.watermarks).map(([key, val]) => (
                    <div key={key} className="flex justify-between text-[10px] font-mono py-0.5">
                      <span className="text-muted-foreground truncate mr-2">{key}</span>
                      <span className="tabular-nums">{val}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {policyTrace.events.length > 0 && (
              <div className="space-y-1">
                <span className="text-xs text-muted-foreground">{L.policyEvents} ({policyTrace.events.length})</span>
                <div className="max-h-[200px] overflow-y-auto border border-[--color-border]">
                  <table className="w-full text-[10px] font-mono">
                    <thead>
                      <tr className="text-left text-muted-foreground bg-[--color-muted]/10">
                        <th className="py-0.5 px-1">Seq</th>
                        <th className="py-0.5 px-1">Kind</th>
                        <th className="py-0.5 px-1">Time</th>
                      </tr>
                    </thead>
                    <tbody>
                      {policyTrace.events.map((ev, i) => (
                        <tr key={i} className="border-t border-[--color-border]/50">
                          <td className="py-0.5 px-1 tabular-nums">{ev.seq ?? "—"}</td>
                          <td className="py-0.5 px-1">{ev.kind ?? "—"}</td>
                          <td className="py-0.5 px-1 text-muted-foreground">{ev.created_at ? fmtTs(ev.created_at) : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
        {!loading && !error && policyTrace?.error && (
          <p className="text-xs text-muted-foreground text-center py-4">{policyTrace.error}</p>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Linkage card (Room/Kanban linkage)                                  */
/* ------------------------------------------------------------------ */

function LinkageCard({ peerGrants, loading, error, onRetry }: {
  peerGrants: PeerGrant[] | null; loading: boolean; error: string | null; onRetry: () => void;
}) {
  const { t } = useI18n();
  const L = { ...FALLBACK, ...(t.rooms ?? {}) };
  const links = peerGrants?.filter((g) => g.target_profile) ?? [];
  const count = links.length;

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Network className="h-4 w-4 text-muted-foreground" />{L.linkage}
          {count > 0 && <span className="text-xs text-muted-foreground font-normal">({count})</span>}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading && <div className="flex justify-center py-4"><Spinner /></div>}
        {!loading && error && (
          <div className="text-center space-y-2 py-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button ghost size="xs" onClick={onRetry}>Retry</Button>
          </div>
        )}
        {!loading && !error && count === 0 && (
          <p className="py-4 text-center text-xs text-muted-foreground">{L.noLinkage}</p>
        )}
        {!loading && !error && links.length > 0 && (
          <div className="divide-y divide-[--color-border]">
            {links.map((g) => (
              <div key={`${g.room_id}-${g.member_id}`} className="py-2 space-y-1 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-muted-foreground truncate flex-1">{g.member_id}</span>
                  <span className={`font-medium ${PEER_STATUS_COLOR[g.status] ?? ""}`}>
                    {g.status === "ready" ? L.linkReady : g.status === "unavailable" ? L.linkUnavailable : g.status === "needs_reauthorization" ? L.linkNeedsReauth : g.status}
                  </span>
                </div>
                {g.target_profile && (
                  <div className="flex items-center gap-2 pl-1">
                    <span className="text-muted-foreground">{L.targetProfile}:</span>
                    <span className="font-mono">{g.target_profile}</span>
                  </div>
                )}
                {g.capability_digest && (
                  <div className="flex items-center gap-2 pl-1">
                    <span className="text-muted-foreground">Capability:</span>
                    <span className="font-mono text-[10px] text-muted-foreground/70 truncate">{g.capability_digest.slice(0, 16)}…</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
