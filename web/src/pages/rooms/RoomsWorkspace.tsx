import { useMemo, useState } from "react";
import { Markdown } from "@/components/Markdown";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  MessageSquareText,
  Plus,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import type {
  PeerGrant,
  PendingAction,
  PolicyTraceResponse,
  ProfileInfo,
  RoomCreateRequest,
  ReplicationHealthResponse,
  RoomActivityItem,
  RoomMemberRole,
  RoomSummary,
  RoomTopologyResponse,
  RoomWorkspaceResponse,
  RoomWorkspaceTask,
} from "@/lib/api";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";
import type { Translations } from "@/i18n/types";
import { filterRoomInbox, taskProgress } from "./workspace-helpers";
import { CreateTeamDialog, WorkComposer } from "./TeamOperations";

const compactButtonClass = "inline-flex h-8 w-auto flex-none self-start items-center justify-center whitespace-nowrap rounded-md border border-border bg-background px-3 text-xs font-semibold text-text-primary shadow-sm transition-colors hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:pointer-events-none disabled:opacity-50";
const compactPrimaryButtonClass = `${compactButtonClass} border-primary bg-primary text-primary-foreground hover:bg-primary/90`;
const wrapContentClass = "min-w-0 max-w-full break-words [overflow-wrap:anywhere]";

export type RoomPreset = "all" | "needs_action" | "failed" | "running" | "idle";
export type RoomWorkspaceTab = "tasks" | "conversation" | "activity";
export type RoomTaskMode = "list" | "graph" | "attempts";
export type RoomInspector =
  | { kind: "room" }
  | { kind: "task"; taskId: string }
  | { kind: "attempt"; attemptIndex: number }
  | { kind: "event"; eventId: string };

export interface RoomsWorkspaceProps {
  rooms: readonly RoomSummary[];
  selectedRoomId: string | null;
  workspace: RoomWorkspaceResponse | null;
  topology?: RoomTopologyResponse | null;
  peerGrants?: readonly PeerGrant[] | null;
  replicationHealth?: ReplicationHealthResponse | null;
  policyTrace?: PolicyTraceResponse | null;
  search: string;
  preset: RoomPreset;
  tab: RoomWorkspaceTab;
  taskMode: RoomTaskMode;
  inspector: RoomInspector;
  actionCenterAction?: PendingAction | null;
  profiles?: readonly ProfileInfo[];
  teamBusy?: boolean;
  onCreateTeam?(request: RoomCreateRequest): Promise<boolean>;
  onDelegateWork?(recipient: string, text: string): Promise<boolean>;
  onSearchChange(value: string): void;
  onPresetChange(value: RoomPreset): void;
  onSelectRoom(id: string): void;
  onTabChange(value: RoomWorkspaceTab): void;
  onTaskModeChange(value: RoomTaskMode): void;
  onInspectorChange(value: RoomInspector): void;
  onOpenActionCenter(action: PendingAction): void;
  onCloseActionCenter(): void;
  onApproveAction(action: PendingAction): void;
  onDenyAction(action: PendingAction): void;
  actionBusy?: boolean;
  className?: string;
}

const stamp = (value?: number | null) => value
  ? new Date(value * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  : "—";
const tone = (value: string) => /fail|block|denied|cancel/i.test(value)
  ? "destructive" as const
  : /complete|success|healthy|ready|armed/i.test(value)
    ? "success" as const
    : /run|progress|work/i.test(value)
      ? "default" as const
      : "outline" as const;
const humanize = (value: string) => value.replaceAll("_", " ").replaceAll(".", " · ");
type Copy = NonNullable<Translations["roomsWorkspace"]>;
type CopyKey = keyof Copy;
/** Known backend vocabulary gets a translated label; anything else falls back to the humanized raw value. */
const vocabulary = (entries: Record<string, CopyKey>) => {
  const keys = new Map(Object.entries(entries));
  return (s: Copy, value: string) => {
    const key = keys.get(value);
    return key ? s[key] : humanize(value);
  };
};
const stateLabel = vocabulary({
  pending: "statusPending", in_progress: "statusInProgress", completed: "statusCompleted", failed: "statusFailed",
  blocked: "statusBlocked", cancelled: "statusCancelled", claimed: "statusClaimed", deferred: "statusDeferred",
  queued: "statusQueued", running: "statusRunning", settled: "statusSettled", indeterminate: "statusIndeterminate",
  waiting: "statusWaiting", armed: "statusArmed", delivering: "statusDelivering", denied: "statusDenied",
  retired: "statusRetired", stopped: "statusStopped",
});
const actorLabel = vocabulary({ user: "actorUser", member: "actorMember", gateway: "actorGateway", system: "actorSystem" });
const actionKindLabel = vocabulary({
  permission: "actionKindPermission", plan_approval: "actionKindPlanApproval", shutdown: "actionKindShutdown", retry: "actionKindRetry",
});
const detailLabel = vocabulary({
  tool_name: "detailToolName", scope: "detailScope", status: "detailStatus",
  execution_generation: "detailExecutionGeneration", step_count: "detailStepCount", reason: "detailReason",
});
const fill = (template: string, values: Record<string, string | number>) =>
  template.replace(/\{(\w+)\}/g, (match, name: string) => Object.hasOwn(values, name) ? String(values[name]) : match);
const actionCount = (s: Copy, count: number) => fill(count === 1 ? s.actionCountOne : s.actionCountOther, { count });
const displayMember = (s: Copy, member: RoomMemberRole) => member.handle || member.profile || s.teamMember;
const roleLabel = (s: Copy, role: RoomMemberRole["role"]) => role === "team_lead"
  ? s.roleTeamLead
  : role === "coordinator"
    ? s.roleCoordinator
    : role === "observer"
      ? s.roleObserver
      : s.roleTeammate;

function Choices<T extends string>({ value, values, labels, onChange, name }: {
  value: T;
  values: readonly T[];
  labels: Record<T, string>;
  onChange(value: T): void;
  name: string;
}) {
  return <div
    role="group"
    aria-label={name}
    className="inline-flex min-w-max items-center rounded-md bg-surface/60 p-0.5"
  >
    {values.map((item) => <button
      key={item}
      type="button"
      aria-pressed={item === value}
      onClick={() => onChange(item)}
      className={`rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 ${item === value ? "bg-background text-text-primary shadow-sm" : "text-text-tertiary hover:bg-background/50 hover:text-text-secondary"}`}
    >{labels[item]}</button>)}
  </div>;
}

function Inbox({ p, s, rooms, onNewTeam }: { p: RoomsWorkspaceProps; s: Copy; rooms: RoomSummary[]; onNewTeam(): void }) {
  return <aside aria-label="Room inbox" className={`min-h-0 flex-col border-border bg-background/40 md:flex md:border-r ${p.selectedRoomId ? "hidden" : "flex"}`}>
    <div className="border-b border-border p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">{s.teamInbox}</p>
      <div className="mt-1 flex items-center justify-between gap-3"><h2 className="text-2xl font-bold">{s.roomsTitle}</h2>{p.onCreateTeam ? <button type="button" className={compactPrimaryButtonClass} onClick={onNewTeam}><Plus className="mr-1 h-3.5 w-3.5" /> {s.newTeam}</button> : null}</div>
      <p className="mt-1 text-sm text-text-secondary">{s.inboxSubtitle}</p>
      <label className="relative mt-4 block">
        <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-text-tertiary" />
        <Input aria-label="Search rooms" value={p.search} onChange={(event) => p.onSearchChange(event.target.value)} className="pl-9" placeholder={s.searchPlaceholder} />
      </label>
      <div className="mt-3 overflow-x-auto"><Choices value={p.preset} values={["all", "needs_action", "failed", "running", "idle"]} labels={{ all: s.presetAll, needs_action: s.presetNeedsAction, failed: s.presetFailed, running: s.presetRunning, idle: s.presetIdle }} onChange={p.onPresetChange} name="Room presets" /></div>
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto p-2">
      {rooms.map((room) => {
        const selected = p.selectedRoomId === room.room_id;
        const actions = room.workspace?.pending_action_count ?? 0;
        return <button
          key={room.room_id}
          type="button"
          aria-current={selected ? "true" : undefined}
          onClick={() => p.onSelectRoom(room.room_id)}
          className={`mb-2 w-full rounded-lg border p-3 text-left transition-colors hover:border-primary/50 hover:bg-surface ${selected ? "border-primary bg-primary/10" : "border-border bg-background"}`}
        >
          <span className="flex items-start justify-between gap-3">
            <span className="min-w-0">
              <strong className="block truncate">{room.name}</strong>
              <span className="mt-0.5 block truncate text-xs text-text-secondary">{room.workspace?.current_task?.subject ?? s.waitingForWork}</span>
            </span>
            <small className="shrink-0 text-text-tertiary">{stamp(room.workspace?.last_activity_at)}</small>
          </span>
          {room.workspace?.task_counts.total ? <>
            <span className="mt-3 block h-1.5 overflow-hidden rounded-full bg-border"><span className="block h-full rounded-full bg-primary" style={{ width: `${taskProgress(room)}%` }} /></span>
            <span className="mt-2 flex items-center justify-between text-xs text-text-secondary">
              <span>{fill(s.progressComplete, { completed: room.workspace.task_counts.completed, total: room.workspace.task_counts.total })}</span>
              {actions ? <Badge tone="warning">{actionCount(s, actions)}</Badge> : null}
            </span>
          </> : actions ? <Badge tone="warning" className="mt-3">{actionCount(s, actions)}</Badge> : null}
        </button>;
      })}
      {!rooms.length ? <div className="p-8 text-center"><MessageSquareText className="mx-auto mb-2 h-6 w-6 text-text-tertiary" /><p>{s.noRoomsMatch}</p><p className="text-xs text-text-secondary">{s.noRoomsHint}</p></div> : null}
    </div>
  </aside>;
}

function Header({ p, s, room }: { p: RoomsWorkspaceProps; s: Copy; room: RoomSummary }) {
  const counts = room.workspace?.task_counts;
  return <header className="border-b border-border bg-background/90 px-4 pt-3 backdrop-blur">
    <button type="button" onClick={() => p.onSelectRoom("")} className="mb-2 inline-flex items-center gap-1 text-sm md:hidden"><ChevronLeft className="h-4 w-4" /> {s.backToInbox}</button>
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">{s.teamWorkspace}</p>
        <h1 className="truncate text-2xl font-bold">{room.name}</h1>
        <p className={`mt-1 text-sm text-text-secondary ${wrapContentClass}`}>{room.workspace?.current_task?.subject ?? s.readyForNext}</p>
      </div>
      <Badge tone={room.disbanded_at ? "outline" : "success"}>{room.disbanded_at ? s.statusClosed : s.statusLive}</Badge>
    </div>
    <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border text-center text-sm sm:grid-cols-4">
      {[
        [counts ? `${counts.completed} / ${counts.total}` : "—", s.statTasks],
        [room.workspace?.active_member_count ?? 0, s.statWorkingNow],
        [counts?.failed ?? counts?.blocked ?? 0, s.statBlocked],
        [room.workspace?.pending_action_count ?? 0, s.statNeedsYou],
      ].map(([value, label]) => <span key={label} className="bg-background px-2 py-2.5"><b className="block text-base">{value}</b><small className="text-text-tertiary">{label}</small></span>)}
    </div>
    <div role="tablist" aria-label="Room views" className="mt-2 flex overflow-x-auto">
      {(["tasks", "conversation", "activity"] as const).map((view) => <button
        key={view}
        type="button"
        role="tab"
        aria-selected={p.tab === view}
        onClick={() => p.onTabChange(view)}
        className={`shrink-0 border-b-2 px-4 py-3 text-sm capitalize ${p.tab === view ? "border-primary font-semibold text-text-primary" : "border-transparent text-text-secondary"}`}
      >{{ tasks: s.tabTasks, conversation: s.tabConversation, activity: s.tabTimeline }[view]}</button>)}
    </div>
  </header>;
}

function MemberRoster({ s, topology, fallback }: { s: Copy; topology?: RoomTopologyResponse | null; fallback: RoomSummary["members"] }) {
  if (!topology?.members.length) return fallback.length ? <div className="flex flex-wrap gap-2">{fallback.map((member, index) => <Badge key={member.member_id ?? member.handle ?? index} tone="outline">{member.display_name || member.handle || member.profile || s.teamMember}</Badge>)}</div> : null;
  return <section aria-labelledby="room-team-heading" className="rounded-lg border border-border bg-surface/40 p-3">
    <div className="mb-3 flex items-center gap-2"><Users className="h-4 w-4 text-primary" /><h3 id="room-team-heading" className="font-semibold">{s.teamHeading}</h3></div>
    <div className="grid gap-2 sm:grid-cols-2">
      {topology.members.map((member) => <div key={member.member_id} className="flex items-center justify-between gap-3 rounded-md bg-background p-2.5">
        <span className="min-w-0"><b className="block truncate">{displayMember(s, member)}</b><small className="text-text-secondary">{roleLabel(s, member.role)}</small></span>
        <span className={`text-right text-xs text-text-secondary ${wrapContentClass}`}>{member.current_task ? <span className={`block ${wrapContentClass}`}>{member.current_task}</span> : null}{member.role === "observer" ? stateLabel(s, member.observer_state ?? "waiting") : member.activity_level != null ? fill(s.percentActive, { percent: member.activity_level }) : s.available}</span>
      </div>)}
    </div>
  </section>;
}

function dependencyNames(s: Copy, ids: readonly string[] | undefined, tasks: readonly RoomWorkspaceTask[]) {
  return (ids ?? []).map((id) => tasks.find((task) => task.task_id === id)?.subject ?? s.anotherTask);
}

function orderedTasks(tasks: readonly RoomWorkspaceTask[]) {
  const sourceIndex = new Map(tasks.map((task, index) => [task.task_id, index]));
  const taskById = new Map(tasks.map((task) => [task.task_id, task]));
  const dependencies = new Map(tasks.map((task) => [
    task.task_id,
    (task.blockedBy ?? []).filter((id) => taskById.has(id)),
  ]));
  const outgoing = new Map(tasks.map((task) => [task.task_id, [] as string[]]));
  for (const [taskId, prerequisites] of dependencies) {
    for (const prerequisite of prerequisites) outgoing.get(prerequisite)?.push(taskId);
  }
  const incoming = new Map([...dependencies].map(([taskId, prerequisites]) => [taskId, prerequisites.length]));
  const ready = tasks.filter((task) => incoming.get(task.task_id) === 0);
  const ordered: RoomWorkspaceTask[] = [];
  const seen = new Set<string>();

  while (ready.length) {
    ready.sort((left, right) => (sourceIndex.get(left.task_id) ?? 0) - (sourceIndex.get(right.task_id) ?? 0));
    const task = ready.shift()!;
    if (seen.has(task.task_id)) continue;
    seen.add(task.task_id);
    ordered.push(task);
    for (const blockedId of outgoing.get(task.task_id) ?? []) {
      if (!incoming.has(blockedId)) continue;
      const remaining = (incoming.get(blockedId) ?? 1) - 1;
      incoming.set(blockedId, remaining);
      if (remaining === 0) ready.push(taskById.get(blockedId)!);
    }
  }

  return [...ordered, ...tasks.filter((task) => !seen.has(task.task_id))];
}

function Tasks({ p, s, room }: { p: RoomsWorkspaceProps; s: Copy; room: RoomSummary }) {
  const workspace = p.workspace!;
  const content = p.taskMode === "attempts"
    ? <details open className="rounded-lg border border-border bg-surface/30 p-3">
      <summary className="cursor-pointer font-semibold">{s.executionDiagnostics}</summary>
      <p className="mb-2 mt-1 text-xs text-text-secondary">{s.attemptsHint}</p>
      <div>{workspace.attempts.map((attempt, index) => {
        const subject = workspace.tasks.find((task) => task.task_id === attempt.identity.task_id)?.subject ?? s.taskAttempt;
        return <button key={attempt.identity.turn_id} type="button" onClick={() => p.onInspectorChange({ kind: "attempt", attemptIndex: index })} className="flex w-full min-w-0 max-w-full items-center justify-between gap-3 border-t border-border py-3 text-left"><span className={wrapContentClass}>{subject}<small className="block text-text-tertiary">{fill(s.generation, { number: attempt.execution_generation })}</small></span><Badge tone={tone(attempt.status)}>{stateLabel(s, attempt.status)}</Badge></button>;
      })}</div>
    </details>
    : p.taskMode === "graph"
      ? <div className="rounded-lg bg-surface/20 px-3 py-2">
        <p className="mb-2 text-xs text-text-secondary">{s.executionOrder}</p>
        <ol className="divide-y divide-border/60">
          {orderedTasks(workspace.tasks).map((task, index) => {
            const before = dependencyNames(s, task.blockedBy, workspace.tasks);
            const explicitNext = workspace.tasks.filter((candidate) => candidate.blockedBy?.includes(task.task_id)).map((candidate) => candidate.subject);
            return <li key={task.task_id} className="relative py-2.5 pl-7 first:pt-1 last:pb-1">
              <span aria-hidden="true" className="absolute left-1 top-3.5 grid h-4 w-4 place-items-center rounded-full bg-primary/10 text-[9px] font-semibold text-primary">{index + 1}</span>
              <button type="button" onClick={() => p.onInspectorChange({ kind: "task", taskId: task.task_id })} className="grid w-full gap-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3">
                <span className="min-w-0">
                  <span className={`block text-sm font-semibold ${wrapContentClass}`}>{task.subject}</span>
                  <span className={`block text-xs text-text-tertiary ${wrapContentClass}`}>
                    {task.owner ? <>@{task.owner}</> : s.unassigned}
                    {` · ${before.length ? fill(s.afterTasks, { tasks: before.join(", ") }) : s.startsIndependently}`}
                  </span>
                </span>
                <span className="flex min-w-0 max-w-full items-center gap-2 text-xs text-text-secondary">
                  <Badge tone={tone(task.status)}>{stateLabel(s, task.status)}</Badge>
                  {explicitNext.length ? <><ChevronRight className="h-3.5 w-3.5" /><span className={`hidden lg:inline ${wrapContentClass}`}>{explicitNext.join(", ")}</span></> : <span className="hidden lg:inline">{s.noDependents}</span>}
                </span>
              </button>
            </li>;
          })}
        </ol>
      </div>
      : <div className="space-y-2">{workspace.tasks.map((task) => {
        const prerequisites = dependencyNames(s, task.blockedBy, workspace.tasks);
        return <article key={task.task_id} className="flex flex-col gap-3 rounded-lg border border-border bg-background p-3 sm:flex-row sm:items-center sm:justify-between">
          <button type="button" className="min-w-0 text-left" onClick={() => p.onInspectorChange({ kind: "task", taskId: task.task_id })}>
            <b className={`block ${wrapContentClass}`}>{task.subject}</b>
            <small className={`mt-1 block text-text-secondary ${wrapContentClass}`}>{prerequisites.length ? fill(s.waitingFor, { tasks: prerequisites.join(", ") }) : task.description || s.readyToMove}</small>
            {task.owner ? <small className="mt-1 block text-text-tertiary">{fill(s.ownedBy, { owner: task.owner })}</small> : null}
          </button>
          <span className="flex shrink-0 items-center gap-2"><Badge tone={tone(task.status)}>{stateLabel(s, task.status)}</Badge>{task.pending_actions[0] ? <button type="button" className={compactPrimaryButtonClass} onClick={() => p.onOpenActionCenter(task.pending_actions[0])}>{s.review}</button> : null}</span>
        </article>;
      })}</div>;

  return <div className="space-y-4">
    <MemberRoster s={s} topology={p.topology} fallback={room.members} />
    <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-center"><div><h2 className="font-semibold">{s.workPlan}</h2><p className="text-xs text-text-secondary">{s.workPlanHint}</p></div><Choices value={p.taskMode} values={["list", "graph", "attempts"]} labels={{ list: s.modeList, graph: s.modeGraph, attempts: s.modeAttempts }} onChange={p.onTaskModeChange} name="Task modes" /></div>
    {workspace.tasks.length || workspace.attempts.length ? content : <div className="rounded-lg border border-dashed border-border p-8 text-center"><CircleCheck className="mx-auto mb-2 h-6 w-6 text-success" /><p>{s.noActiveTasks}</p><p className="text-xs text-text-secondary">{s.noActiveTasksHint}</p></div>}
  </div>;
}

function isLeaderReport(item: RoomWorkspaceResponse["conversation"][number]) {
  return /leader[._ -]?report/i.test(item.kind) || /^LEADER_REPORT\s*:/i.test(item.text.trim());
}

function Conversation({ p, s }: { p: RoomsWorkspaceProps; s: Copy }) {
  const items = p.workspace!.conversation;
  const finalReportIndex = items.findLastIndex(isLeaderReport);
  const finalReport = finalReportIndex >= 0 ? items[finalReportIndex] : undefined;
  const thread = items.filter((_, index) => index !== finalReportIndex);
  return <div className="grid min-w-0 items-start gap-4">
    <section aria-label="Conversation thread" className="min-w-0 space-y-3">
      {thread.map((item, index) => {
        const userBrief = item.actor_kind.toLowerCase() === "user" && index === 0;
        return <article key={item.event_id} className={`rounded-lg border border-border bg-background p-3 sm:p-4 ${wrapContentClass}`}>
          <header className="mb-2 flex items-center justify-between gap-3">
            <span className="flex min-w-0 items-center gap-2"><MessageSquareText className="h-4 w-4 shrink-0 text-text-tertiary" /><b className="truncate">@{item.actor_id}</b>{item.actor_kind ? <Badge tone="outline">{actorLabel(s, item.actor_kind)}</Badge> : null}</span>
            <time className="shrink-0 text-xs text-text-tertiary">{stamp(item.created_at)}</time>
          </header>
          {userBrief ? <details className="group">
            <summary className={`cursor-pointer list-none [&::-webkit-details-marker]:hidden ${wrapContentClass}`}><span className={`block whitespace-pre-wrap leading-relaxed group-open:hidden line-clamp-6 ${wrapContentClass}`}>{item.text}</span><span className="mt-2 inline-block text-sm font-medium text-primary group-open:hidden">{s.readFullBrief}</span><span className={`hidden whitespace-pre-wrap leading-relaxed group-open:block ${wrapContentClass}`}>{item.text}</span><span className="mt-2 hidden text-sm font-medium text-primary group-open:inline-block">{s.showLess}</span></summary>
          </details> : <div className={`leading-relaxed ${wrapContentClass}`}><Markdown content={item.text} /></div>}
          {item.task_id ? <button type="button" className={`${compactButtonClass} mt-3`} onClick={() => p.onInspectorChange({ kind: "task", taskId: item.task_id! })}>{s.viewRelatedTask}</button> : null}
        </article>;
      })}
      {!thread.length ? <div className="rounded-lg border border-dashed border-border p-8 text-center"><MessageSquareText className="mx-auto mb-2 h-6 w-6 text-text-tertiary" /><p>{s.noConversation}</p><p className="text-xs text-text-secondary">{s.noConversationHint}</p></div> : null}
    </section>
    {finalReport ? <article aria-label="Final leader report" className={`rounded-xl border border-primary/40 bg-primary/10 p-4 shadow-sm ${wrapContentClass}`}>
      <header className="mb-3 flex items-center justify-between gap-3"><span className="flex items-center gap-2"><ShieldCheck className="h-5 w-5 shrink-0 text-primary" /><b>{s.finalReport}</b></span><time className="shrink-0 text-xs text-text-tertiary">{stamp(finalReport.created_at)}</time></header>
      <div className={`leading-relaxed ${wrapContentClass}`}><Markdown content={finalReport.text.replace(/^LEADER_REPORT\s*:?\s*/i, "")} /></div>
      {finalReport.task_id ? <button type="button" className={`${compactButtonClass} mt-3`} onClick={() => p.onInspectorChange({ kind: "task", taskId: finalReport.task_id! })}>{s.viewRelatedTask}</button> : null}
    </article> : null}
  </div>;
}

function activityCopy(s: Copy, item: RoomActivityItem): { label: string; summary: string } {
  const member = { member: item.member_id ? s.aTeamMember : s.theTeam };
  switch (item.kind) {
    case "message.user":
      return { label: s.activityBriefLabel, summary: s.activityBriefSummary };
    case "message.member":
      return { label: s.activityUpdateLabel, summary: fill(s.activityUpdateSummary, member) };
    case "coordinator.task_assign":
      return { label: s.activityAssignLabel, summary: fill(s.activityAssignSummary, member) };
    case "turn.requested":
      return { label: s.activityRequestedLabel, summary: fill(s.activityRequestedSummary, member) };
    case "turn.settled":
      return { label: s.activitySettledLabel, summary: fill(s.activitySettledSummary, member) };
    default:
      switch (item.category) {
        case "messages": return { label: s.activityMessagesLabel, summary: s.activityMessagesSummary };
        case "tasks": return { label: s.activityTasksLabel, summary: s.activityTasksSummary };
        case "actions": return { label: s.activityActionsLabel, summary: s.activityActionsSummary };
        case "members": return { label: s.activityMembersLabel, summary: s.activityMembersSummary };
        default: return { label: s.activityRoomLabel, summary: s.activityRoomSummary };
      }
  }
}

function Timeline({ p, s }: { p: RoomsWorkspaceProps; s: Copy }) {
  const activity = p.workspace!.activity;
  return <div>
    <div className="mb-4"><h2 className="font-semibold">{s.teamTimeline}</h2><p className="text-xs text-text-secondary">{s.timelineHint}</p></div>
    <ol className="relative ml-2 border-l border-border pl-5">
      {activity.map((item) => {
        const copy = activityCopy(s, item);
        return <li key={item.event_id} className={`relative pb-5 last:pb-0 ${wrapContentClass}`}>
          <span className="absolute -left-[1.62rem] top-1.5 h-2 w-2 rounded-full bg-primary ring-4 ring-background" />
          <button type="button" onClick={() => p.onInspectorChange({ kind: "event", eventId: item.event_id })} className={`block w-full rounded-lg p-2 text-left hover:bg-surface ${wrapContentClass}`}>
            <span className={`flex flex-wrap items-center justify-between gap-2 ${wrapContentClass}`}><b className={wrapContentClass}>{copy.label}</b><time className="text-xs text-text-tertiary">{stamp(item.created_at)}</time></span>
            <p className={`mt-1 line-clamp-2 text-sm text-text-secondary ${wrapContentClass}`}>{copy.summary}</p>
          </button>
        </li>;
      })}
    </ol>
    {!activity.length ? <p className="rounded-lg border border-dashed border-border p-8 text-center text-text-secondary">{s.noActivity}</p> : null}
    <details className="mt-5 rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-sm font-semibold">{s.diagnostics}</summary>
      <p className="mt-1 text-xs text-text-tertiary">{s.rawIdsHidden}</p>
      <dl className="mt-3 grid gap-1 text-xs text-text-secondary"><div>{fill(s.diagEvents, { count: p.workspace!.log.events.length })}</div><div>{fill(s.diagLatestSequence, { seq: p.workspace!.log.latest_seq })}</div><div>{fill(s.diagMoreAvailable, { value: p.workspace!.log.has_more ? s.yes : s.no })}</div></dl>
    </details>
  </div>;
}

function Diagnostics({ s, children }: { s: Copy; children: React.ReactNode }) {
  return <details className="mt-4 rounded-lg border border-border p-3 text-xs"><summary className="cursor-pointer font-semibold">{s.diagnostics}</summary><div className="mt-2 space-y-1 break-all font-mono text-text-secondary">{children}</div></details>;
}

function Inspector({ p, s, room }: { p: RoomsWorkspaceProps; s: Copy; room: RoomSummary }) {
  const workspace = p.workspace!;
  const attempt = p.inspector.kind === "attempt" ? workspace.attempts[p.inspector.attemptIndex] : undefined;
  const taskId = p.inspector.kind === "task" ? p.inspector.taskId : attempt?.identity.task_id;
  const task = workspace.tasks.find((item) => item.task_id === taskId || item.latest_attempt?.identity.task_id === taskId);
  const eventId = p.inspector.kind === "event" ? p.inspector.eventId : undefined;
  const event = eventId ? workspace.activity.find((item) => item.event_id === eventId) : undefined;
  const member = task?.owner ? p.topology?.members.find((item) => item.handle === task.owner || item.member_id === task.owner) : undefined;
  return <aside aria-label="Context inspector" className={`border-t border-border bg-surface/20 p-4 xl:min-h-0 xl:overflow-y-auto xl:border-l xl:border-t-0 ${wrapContentClass}`}>
    <p className="text-xs font-semibold uppercase tracking-[0.15em] text-primary">{s.contextHeading}</p>
    <h2 className={`mt-1 text-xl font-bold ${wrapContentClass}`}>{event?.title ?? task?.subject ?? room.name}</h2>
    {event ? <><p className={`mt-3 text-sm leading-relaxed text-text-secondary ${wrapContentClass}`}>{event.summary}</p>{event.task_id ? <button type="button" className={`${compactButtonClass} mt-3`} onClick={() => p.onInspectorChange({ kind: "task", taskId: event.task_id! })}>{s.openRelatedTask}</button> : null}<EventDiagnostics s={s} event={event} /></> : null}
    {task ? <div className="mt-3 space-y-3"><Badge tone={tone(attempt?.status ?? task.status)}>{stateLabel(s, attempt?.status ?? task.status)}</Badge><div><small className="block text-text-tertiary">{s.owner}</small><p>{task.owner ? `@${task.owner}` : s.unassigned}{member ? <span className="text-text-secondary"> · {roleLabel(s, member.role)}</span> : null}</p></div>{task.description ? <p className={`text-sm leading-relaxed text-text-secondary ${wrapContentClass}`}>{task.description}</p> : null}{task.pending_actions[0] ? <button type="button" className={compactPrimaryButtonClass} onClick={() => p.onOpenActionCenter(task.pending_actions[0])}>{s.reviewAction}</button> : null}{attempt ? <Diagnostics s={s}><div>{s.diagRoomContext}</div><div>{s.diagConversationContext}</div><div>{s.diagAttemptRef}</div><div>{fill(s.diagExecutionGeneration, { value: attempt.execution_generation })}</div><div>{fill(s.diagCancelGeneration, { value: attempt.cancel_generation })}</div><div>{fill(s.diagCreated, { value: stamp(attempt.created_at) })}</div><div>{fill(s.diagUpdated, { value: stamp(attempt.updated_at) })}</div><div>{fill(s.diagStarted, { value: stamp(attempt.started_at) })}</div><div>{fill(s.diagTerminal, { value: stamp(attempt.terminal_at) })}</div><div>{fill(s.diagSettlement, { value: attempt.settlement_status ?? "—" })}</div><div className="font-sans text-warning">{s.payloadRedacted}</div></Diagnostics> : null}</div> : null}
    {!event && !task ? <div className="mt-4 space-y-3 text-sm"><div className="rounded-lg border border-border bg-background p-3"><span className="flex items-center gap-2 font-semibold"><ShieldCheck className="h-4 w-4 text-primary" /> {s.teamHealth}</span><p className="mt-2 text-text-secondary">{p.replicationHealth?.healthy === false ? s.healthAttention : s.healthNormal}</p></div><dl className="grid gap-2"><div><dt className="text-xs text-text-tertiary">{s.connectedPeers}</dt><dd>{fill(s.peersReady, { ready: p.replicationHealth?.ready ?? 0, total: p.replicationHealth?.total_peers ?? p.peerGrants?.length ?? 0 })}</dd></div><div><dt className="text-xs text-text-tertiary">{s.policyEvents}</dt><dd>{p.policyTrace?.event_count ?? 0}</dd></div><div><dt className="text-xs text-text-tertiary">{s.teamMembers}</dt><dd>{p.topology?.members.length ?? room.members.length}</dd></div></dl><Diagnostics s={s}><div>{s.diagRoomContext}</div><div>{fill(s.diagRevision, { value: room.revision })}</div><div>{fill(s.diagLatestSequence, { seq: room.latest_seq })}</div></Diagnostics></div> : null}
  </aside>;
}

function EventDiagnostics({ s, event }: { s: Copy; event: RoomActivityItem }) {
  return <Diagnostics s={s}><div>{fill(s.diagKind, { value: event.kind })}</div><div>{fill(s.diagSequence, { value: event.seq })}</div>{event.task_id ? <div>{s.diagRelatedTask}</div> : null}{event.thread_id ? <div>{s.diagConversationContext}</div> : null}</Diagnostics>;
}

const ACTION_DETAIL_ALLOWLIST = ["tool_name", "scope", "status", "execution_generation", "step_count", "reason"] as const;
function actionPresentation(s: Copy, kind: unknown) {
  switch (kind) {
    case "permission": return { title: s.actionPermission, approve: s.allowOnce, deny: true };
    case "plan_approval": return { title: s.actionPlanApproval, approve: s.approvePlan, deny: true };
    case "shutdown": return { title: s.actionShutdown, approve: s.confirmShutdown, deny: true };
    case "retry": return { title: s.retry, approve: s.retry, deny: false };
    default: return null;
  }
}
function ActionDialog({ p, s }: { p: RoomsWorkspaceProps; s: Copy }) {
  const action = p.actionCenterAction;
  if (!action) return null;
  const presentation = actionPresentation(s, action.kind);
  const safe = Object.fromEntries(Object.entries(action.detail).filter(([key, value]) => ACTION_DETAIL_ALLOWLIST.includes(key as typeof ACTION_DETAIL_ALLOWLIST[number]) && (typeof value === "string" || typeof value === "number" || typeof value === "boolean")));
  const requester = action.from_handle ? `@${action.from_handle}` : s.aTeamMember;
  const title = presentation?.title ?? s.unsupportedAction;
  const riskCopy = action.kind === "retry"
    ? s.retryRisk
    : s.reviewRisk;

  return <Dialog open onOpenChange={(open) => { if (!open) p.onCloseActionCenter(); }}>
    <DialogContent data-testid="room-action-dialog" className={`max-h-[85vh] w-[min(30rem,calc(100vw-2rem))] gap-0 overflow-y-auto rounded-2xl border-border/80 bg-background p-0 shadow-2xl ${wrapContentClass}`}>
      <div className="border-b border-border px-5 py-5 pr-12 sm:px-6 sm:py-6 sm:pr-14">
        <DialogHeader className="gap-1.5 text-left">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-warning">{s.actionRequired}</p>
          <DialogTitle className="text-xl leading-tight">{title}</DialogTitle>
          <DialogDescription className="text-sm">{fill(s.requestedBy, { requester })}</DialogDescription>
        </DialogHeader>
      </div>
      <div className="space-y-4 px-5 py-5 sm:px-6">
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-tertiary">{s.request}</h3>
          <p className={`mt-1.5 text-sm leading-6 text-text-primary ${wrapContentClass}`}>{action.description}</p>
        </section>
        {Object.keys(safe).length ? <dl className="divide-y divide-border rounded-lg border border-border bg-surface/30 px-3 text-sm">{Object.entries(safe).map(([key, value]) => <div key={key} className="grid min-w-0 max-w-full grid-cols-[minmax(0,1fr)_auto] gap-4 py-2.5"><dt className="capitalize text-text-secondary">{detailLabel(s, key)}</dt><dd className={`text-right font-medium text-text-primary ${wrapContentClass}`}>{String(value)}</dd></div>)}</dl> : null}
        <div className="flex items-start gap-2.5 rounded-lg bg-warning/10 p-3 text-xs leading-5 text-text-secondary">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <p><span className="font-semibold text-text-primary">{s.protectedDetails}</span> {s.sensitiveHidden} {riskCopy}</p>
        </div>
      </div>
      <DialogFooter className="flex-col gap-2 border-t border-border bg-surface/20 px-5 py-4 sm:flex-row sm:justify-end sm:px-6">
        {presentation ? <>
          {presentation.deny ? <button type="button" className={`${compactButtonClass} w-full sm:w-auto`} disabled={p.actionBusy} onClick={() => p.onDenyAction(action)}>{s.deny}</button> : <button type="button" className={`${compactButtonClass} w-full sm:w-auto`} disabled={p.actionBusy} onClick={p.onCloseActionCenter}>{s.cancel}</button>}
          <button type="button" className={`${compactPrimaryButtonClass} w-full sm:w-auto`} disabled={p.actionBusy} onClick={() => p.onApproveAction(action)}>{presentation.approve}</button>
        </> : <button type="button" className={`${compactButtonClass} w-full sm:w-auto`} onClick={p.onCloseActionCenter}>{s.close}</button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

export function RoomsWorkspace(p: RoomsWorkspaceProps) {
  const { t } = useI18n();
  const s = useMemo(() => localeSection(t, "roomsWorkspace"), [t]);
  const [createTeamOpen, setCreateTeamOpen] = useState(false);
  const rooms = useMemo(() => filterRoomInbox(p.rooms, p.search, p.preset), [p.rooms, p.search, p.preset]);
  const baseRoom = p.workspace?.room ?? p.rooms.find((item) => item.room_id === p.selectedRoomId);
  const summary = p.rooms.find((item) => item.room_id === p.selectedRoomId)?.workspace;
  const room = baseRoom ? { ...baseRoom, workspace: summary ?? baseRoom.workspace } : undefined;
  if (!room || !p.workspace) return <><div className="grid min-h-[36rem] overflow-hidden rounded-lg border border-border md:grid-cols-[18rem_1fr]"><Inbox p={p} s={s} rooms={rooms} onNewTeam={() => setCreateTeamOpen(true)} /><main className="hidden place-items-center bg-surface/20 md:grid"><div className="text-center"><MessageSquareText className="mx-auto mb-3 h-8 w-8 text-text-tertiary" /><h1 className="text-lg font-semibold">{s.chooseRoom}</h1><p className="mt-1 text-sm text-text-secondary">{s.chooseRoomHint}</p></div></main></div>{p.onCreateTeam ? <CreateTeamDialog profiles={p.profiles ?? []} createOpen={createTeamOpen} onCloseCreate={() => setCreateTeamOpen(false)} onCreate={p.onCreateTeam} busy={p.teamBusy} /> : null}</>;
  const actions = p.workspace.pending_actions;
  return <div className={`grid min-h-[36rem] overflow-hidden rounded-lg border border-border md:grid-cols-[18rem_minmax(0,1fr)] xl:h-[calc(100vh-8rem)] xl:grid-cols-[18rem_minmax(30rem,1fr)_21rem] ${p.className ?? ""}`}>
    <Inbox p={p} s={s} rooms={rooms} onNewTeam={() => setCreateTeamOpen(true)} />
    <main className="min-h-0 overflow-y-auto bg-background"><Header p={p} s={s} room={room} /><section role="tabpanel" className={`p-3 sm:p-4 lg:p-5 ${wrapContentClass}`}>
      <WorkComposer topology={p.topology} room={room} busy={p.teamBusy} onDelegate={p.onDelegateWork} />
      {actions.length ? <section aria-label="Action Center" className="mb-4 space-y-2"><h2 className="flex items-center gap-2 text-sm font-semibold"><AlertTriangle className="h-4 w-4 text-warning" /> {s.actionCenter}</h2>{actions.map((action) => <div key={action.action_id} className="grid gap-3 rounded-lg border border-warning/40 bg-warning/10 p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"><span className={wrapContentClass}><b>{action.kind === "retry" ? s.retryRequired : s.decisionNeeded}</b><span className={`block text-sm text-text-secondary ${wrapContentClass}`}>{action.description}</span></span><button type="button" data-testid="room-action-review" onClick={() => p.onOpenActionCenter(action)} className="inline-flex h-9 w-full items-center justify-center self-center whitespace-nowrap rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground shadow-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 sm:w-auto">{fill(s.reviewKind, { kind: actionKindLabel(s, action.kind) })}</button></div>)}</section> : null}
      {p.tab === "tasks" ? <Tasks p={p} s={s} room={room} /> : p.tab === "conversation" ? <Conversation p={p} s={s} /> : <Timeline p={p} s={s} />}
    </section></main>
    <Inspector p={p} s={s} room={room} />
    <ActionDialog p={p} s={s} />
    {p.onCreateTeam ? <CreateTeamDialog profiles={p.profiles ?? []} createOpen={createTeamOpen} onCloseCreate={() => setCreateTeamOpen(false)} onCreate={p.onCreateTeam} busy={p.teamBusy} /> : null}
  </div>;
}

export default RoomsWorkspace;
