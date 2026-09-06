import { useMemo } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  MessageSquareText,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
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
  ObserverStatus,
  PeerGrant,
  PendingAction,
  PolicyTraceResponse,
  ReplicationHealthResponse,
  RoomActivityItem,
  RoomMemberRole,
  RoomSummary,
  RoomTopologyResponse,
  RoomWorkspaceResponse,
  RoomWorkspaceTask,
} from "@/lib/api";
import { filterRoomInbox, taskProgress } from "./workspace-helpers";

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
  observer?: ObserverStatus | null;
  peerGrants?: readonly PeerGrant[] | null;
  replicationHealth?: ReplicationHealthResponse | null;
  policyTrace?: PolicyTraceResponse | null;
  search: string;
  preset: RoomPreset;
  tab: RoomWorkspaceTab;
  taskMode: RoomTaskMode;
  inspector: RoomInspector;
  actionCenterAction?: PendingAction | null;
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
const displayMember = (member: RoomMemberRole) => member.handle || member.profile || "Team member";
const roleLabel = (role: RoomMemberRole["role"]) => role === "team_lead"
  ? "Team lead"
  : role === "coordinator"
    ? "Coordinator"
    : role === "observer"
      ? "Observer"
      : "Teammate";

function Choices<T extends string>({ value, values, onChange, name }: {
  value: T;
  values: readonly T[];
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
    >{humanize(item)}</button>)}
  </div>;
}

function Inbox({ p, rooms }: { p: RoomsWorkspaceProps; rooms: RoomSummary[] }) {
  return <aside aria-label="Room inbox" className={`min-h-0 flex-col border-border bg-background/40 md:flex md:border-r ${p.selectedRoomId ? "hidden" : "flex"}`}>
    <div className="border-b border-border p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">Team inbox</p>
      <h2 className="mt-1 text-2xl font-bold">Rooms</h2>
      <p className="mt-1 text-sm text-text-secondary">Work that needs your attention, in one place.</p>
      <label className="relative mt-4 block">
        <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-text-tertiary" />
        <Input aria-label="Search rooms" value={p.search} onChange={(event) => p.onSearchChange(event.target.value)} className="pl-9" placeholder="Search teams or work" />
      </label>
      <div className="mt-3 overflow-x-auto"><Choices value={p.preset} values={["all", "needs_action", "failed", "running", "idle"]} onChange={p.onPresetChange} name="Room presets" /></div>
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
              <span className="mt-0.5 block truncate text-xs text-text-secondary">{room.workspace?.current_task?.subject ?? "Waiting for new work"}</span>
            </span>
            <small className="shrink-0 text-text-tertiary">{stamp(room.workspace?.last_activity_at)}</small>
          </span>
          {room.workspace?.task_counts.total ? <>
            <span className="mt-3 block h-1.5 overflow-hidden rounded-full bg-border"><span className="block h-full rounded-full bg-primary" style={{ width: `${taskProgress(room)}%` }} /></span>
            <span className="mt-2 flex items-center justify-between text-xs text-text-secondary">
              <span>{room.workspace.task_counts.completed} of {room.workspace.task_counts.total} complete</span>
              {actions ? <Badge tone="warning">{actions} {actions === 1 ? "action" : "actions"}</Badge> : null}
            </span>
          </> : actions ? <Badge tone="warning" className="mt-3">{actions} {actions === 1 ? "action" : "actions"}</Badge> : null}
        </button>;
      })}
      {!rooms.length ? <div className="p-8 text-center"><MessageSquareText className="mx-auto mb-2 h-6 w-6 text-text-tertiary" /><p>No rooms match</p><p className="text-xs text-text-secondary">Try another search or filter.</p></div> : null}
    </div>
  </aside>;
}

function Header({ p, room }: { p: RoomsWorkspaceProps; room: RoomSummary }) {
  const counts = room.workspace?.task_counts;
  return <header className="border-b border-border bg-background/90 px-4 pt-3 backdrop-blur">
    <button type="button" onClick={() => p.onSelectRoom("")} className="mb-2 inline-flex items-center gap-1 text-sm md:hidden"><ChevronLeft className="h-4 w-4" /> Inbox</button>
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">Team workspace</p>
        <h1 className="truncate text-2xl font-bold">{room.name}</h1>
        <p className="mt-1 truncate text-sm text-text-secondary">{room.workspace?.current_task?.subject ?? "Ready for the next assignment"}</p>
      </div>
      <Badge tone={room.disbanded_at ? "outline" : "success"}>{room.disbanded_at ? "Closed" : "Live"}</Badge>
    </div>
    <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border text-center text-sm sm:grid-cols-4">
      {[
        [counts ? `${counts.completed} / ${counts.total}` : "—", "Tasks"],
        [room.workspace?.active_member_count ?? 0, "Working now"],
        [counts?.failed ?? counts?.blocked ?? 0, "Blocked"],
        [room.workspace?.pending_action_count ?? 0, "Needs you"],
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
      >{view === "activity" ? "Timeline" : view}</button>)}
    </div>
  </header>;
}

function MemberRoster({ topology, fallback }: { topology?: RoomTopologyResponse | null; fallback: RoomSummary["members"] }) {
  if (!topology?.members.length) return fallback.length ? <div className="flex flex-wrap gap-2">{fallback.map((member, index) => <Badge key={member.member_id ?? member.handle ?? index} tone="outline">{member.display_name || member.handle || member.profile || "Team member"}</Badge>)}</div> : null;
  return <section aria-labelledby="room-team-heading" className="rounded-lg border border-border bg-surface/40 p-3">
    <div className="mb-3 flex items-center gap-2"><Users className="h-4 w-4 text-primary" /><h3 id="room-team-heading" className="font-semibold">Team</h3></div>
    <div className="grid gap-2 sm:grid-cols-2">
      {topology.members.map((member) => <div key={member.member_id} className="flex items-center justify-between gap-3 rounded-md bg-background p-2.5">
        <span className="min-w-0"><b className="block truncate">{displayMember(member)}</b><small className="text-text-secondary">{roleLabel(member.role)}</small></span>
        <span className="text-right text-xs text-text-secondary">{member.current_task ? <span className="block max-w-40 truncate">{member.current_task}</span> : null}{member.role === "observer" ? humanize(member.observer_state ?? "waiting") : member.activity_level != null ? `${member.activity_level}% active` : "Available"}</span>
      </div>)}
    </div>
  </section>;
}

function dependencyNames(ids: readonly string[] | undefined, tasks: readonly RoomWorkspaceTask[]) {
  return (ids ?? []).map((id) => tasks.find((task) => task.task_id === id)?.subject ?? "another task");
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

function Tasks({ p, room }: { p: RoomsWorkspaceProps; room: RoomSummary }) {
  const workspace = p.workspace!;
  const content = p.taskMode === "attempts"
    ? <details open className="rounded-lg border border-border bg-surface/30 p-3">
      <summary className="cursor-pointer font-semibold">Execution diagnostics</summary>
      <p className="mb-2 mt-1 text-xs text-text-secondary">Attempt history and generations are operational details.</p>
      <div>{workspace.attempts.map((attempt, index) => {
        const subject = workspace.tasks.find((task) => task.task_id === attempt.identity.task_id)?.subject ?? "Task attempt";
        return <button key={attempt.identity.turn_id} type="button" onClick={() => p.onInspectorChange({ kind: "attempt", attemptIndex: index })} className="flex w-full items-center justify-between gap-3 border-t border-border py-3 text-left"><span>{subject}<small className="block text-text-tertiary">Generation {attempt.execution_generation}</small></span><Badge tone={tone(attempt.status)}>{humanize(attempt.status)}</Badge></button>;
      })}</div>
    </details>
    : p.taskMode === "graph"
      ? <div className="rounded-lg bg-surface/20 px-3 py-2">
        <p className="mb-2 text-xs text-text-secondary">Execution order and handoffs</p>
        <ol className="divide-y divide-border/60">
          {orderedTasks(workspace.tasks).map((task, index) => {
            const before = dependencyNames(task.blockedBy, workspace.tasks);
            const explicitNext = workspace.tasks.filter((candidate) => candidate.blockedBy?.includes(task.task_id)).map((candidate) => candidate.subject);
            return <li key={task.task_id} className="relative py-2.5 pl-7 first:pt-1 last:pb-1">
              <span aria-hidden="true" className="absolute left-1 top-3.5 grid h-4 w-4 place-items-center rounded-full bg-primary/10 text-[9px] font-semibold text-primary">{index + 1}</span>
              <button type="button" onClick={() => p.onInspectorChange({ kind: "task", taskId: task.task_id })} className="grid w-full gap-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3">
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold">{task.subject}</span>
                  <span className="block text-xs text-text-tertiary">
                    {task.owner ? <>@{task.owner}</> : "Unassigned"}
                    {before.length ? <> · after {before.join(", ")}</> : " · starts independently"}
                  </span>
                </span>
                <span className="flex items-center gap-2 text-xs text-text-secondary">
                  <Badge tone={tone(task.status)}>{humanize(task.status)}</Badge>
                  {explicitNext.length ? <><ChevronRight className="h-3.5 w-3.5" /><span className="hidden max-w-40 truncate lg:inline">{explicitNext.join(", ")}</span></> : <span className="hidden lg:inline">No dependents</span>}
                </span>
              </button>
            </li>;
          })}
        </ol>
      </div>
      : <div className="space-y-2">{workspace.tasks.map((task) => {
        const prerequisites = dependencyNames(task.blockedBy, workspace.tasks);
        return <article key={task.task_id} className="flex flex-col gap-3 rounded-lg border border-border bg-background p-3 sm:flex-row sm:items-center sm:justify-between">
          <button type="button" className="min-w-0 text-left" onClick={() => p.onInspectorChange({ kind: "task", taskId: task.task_id })}>
            <b className="block truncate">{task.subject}</b>
            <small className="mt-1 block text-text-secondary">{prerequisites.length ? `Waiting for ${prerequisites.join(", ")}` : task.description || "Ready to move forward"}</small>
            {task.owner ? <small className="mt-1 block text-text-tertiary">Owned by @{task.owner}</small> : null}
          </button>
          <span className="flex shrink-0 items-center gap-2"><Badge tone={tone(task.status)}>{humanize(task.status)}</Badge>{task.pending_actions[0] ? <Button type="button" size="xs" onClick={() => p.onOpenActionCenter(task.pending_actions[0])}>Review</Button> : null}</span>
        </article>;
      })}</div>;

  return <div className="space-y-4">
    <MemberRoster topology={p.topology} fallback={room.members} />
    <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-center"><div><h2 className="font-semibold">Work plan</h2><p className="text-xs text-text-secondary">Follow progress or open an item for context.</p></div><Choices value={p.taskMode} values={["list", "graph", "attempts"]} onChange={p.onTaskModeChange} name="Task modes" /></div>
    {workspace.tasks.length || workspace.attempts.length ? content : <div className="rounded-lg border border-dashed border-border p-8 text-center"><CircleCheck className="mx-auto mb-2 h-6 w-6 text-success" /><p>No active tasks</p><p className="text-xs text-text-secondary">This team is ready for new work.</p></div>}
  </div>;
}

function isLeaderReport(item: RoomWorkspaceResponse["conversation"][number]) {
  return /leader[._ -]?report/i.test(item.kind) || /^LEADER_REPORT\s*:/i.test(item.text.trim());
}

function Conversation({ p }: { p: RoomsWorkspaceProps }) {
  const items = p.workspace!.conversation;
  const finalReportIndex = items.findLastIndex(isLeaderReport);
  const finalReport = finalReportIndex >= 0 ? items[finalReportIndex] : undefined;
  const thread = items.filter((_, index) => index !== finalReportIndex);
  return <div className="grid items-start gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(20rem,0.85fr)]">
    <section aria-label="Conversation thread" className="min-w-0 space-y-3">
      {thread.map((item, index) => {
        const userBrief = item.actor_kind.toLowerCase() === "user" && index === 0;
        return <article key={item.event_id} className="rounded-lg border border-border bg-background p-3 sm:p-4">
          <header className="mb-2 flex items-center justify-between gap-3">
            <span className="flex min-w-0 items-center gap-2"><MessageSquareText className="h-4 w-4 shrink-0 text-text-tertiary" /><b className="truncate">@{item.actor_id}</b>{item.actor_kind ? <Badge tone="outline">{humanize(item.actor_kind)}</Badge> : null}</span>
            <time className="shrink-0 text-xs text-text-tertiary">{stamp(item.created_at)}</time>
          </header>
          {userBrief ? <details className="group">
            <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden"><span className="block whitespace-pre-wrap leading-relaxed group-open:hidden line-clamp-6">{item.text}</span><span className="mt-2 inline-block text-sm font-medium text-primary group-open:hidden">Read full brief</span><span className="hidden whitespace-pre-wrap leading-relaxed group-open:block">{item.text}</span><span className="mt-2 hidden text-sm font-medium text-primary group-open:inline-block">Show less</span></summary>
          </details> : <p className="whitespace-pre-wrap leading-relaxed">{item.text}</p>}
          {item.task_id ? <Button className="mt-3" size="xs" outlined onClick={() => p.onInspectorChange({ kind: "task", taskId: item.task_id! })}>View related task</Button> : null}
        </article>;
      })}
      {!thread.length ? <div className="rounded-lg border border-dashed border-border p-8 text-center"><MessageSquareText className="mx-auto mb-2 h-6 w-6 text-text-tertiary" /><p>No conversation yet</p><p className="text-xs text-text-secondary">Team updates will appear here.</p></div> : null}
    </section>
    {finalReport ? <article aria-label="Final leader report" className="rounded-xl border border-primary/40 bg-primary/10 p-4 shadow-sm 2xl:sticky 2xl:top-4">
      <header className="mb-3 flex items-center justify-between gap-3"><span className="flex items-center gap-2"><ShieldCheck className="h-5 w-5 shrink-0 text-primary" /><b>Final report</b></span><time className="shrink-0 text-xs text-text-tertiary">{stamp(finalReport.created_at)}</time></header>
      <p className="whitespace-pre-wrap leading-relaxed">{finalReport.text.replace(/^LEADER_REPORT\s*:?\s*/i, "")}</p>
      {finalReport.task_id ? <Button className="mt-3" size="xs" outlined onClick={() => p.onInspectorChange({ kind: "task", taskId: finalReport.task_id! })}>View related task</Button> : null}
    </article> : null}
  </div>;
}

function activityCopy(item: RoomActivityItem): { label: string; summary: string } {
  const member = item.member_id ? "A team member" : "The team";
  switch (item.kind) {
    case "message.user":
      return { label: "Research brief received", summary: "The team lead received a new assignment." };
    case "message.member":
      return { label: "Team update received", summary: `${member} shared an update.` };
    case "coordinator.task_assign":
      return { label: "Task assigned", summary: `${member} received an assignment.` };
    case "turn.requested":
      return { label: "Work requested", summary: `${member} was asked to continue.` };
    case "turn.settled":
      return { label: "Work completed", summary: `${member} finished a turn.` };
    default:
      switch (item.category) {
        case "messages": return { label: "Conversation updated", summary: "The team conversation changed." };
        case "tasks": return { label: "Task activity", summary: "The work plan changed." };
        case "actions": return { label: "Action requested", summary: "A team action needs attention." };
        case "members": return { label: "Team updated", summary: "Team membership or availability changed." };
        default: return { label: "Room activity", summary: "The room state changed." };
      }
  }
}

function Timeline({ p }: { p: RoomsWorkspaceProps }) {
  const activity = p.workspace!.activity;
  return <div>
    <div className="mb-4"><h2 className="font-semibold">Team timeline</h2><p className="text-xs text-text-secondary">A readable history of decisions, handoffs, and outcomes.</p></div>
    <ol className="relative ml-2 border-l border-border pl-5">
      {activity.map((item) => {
        const copy = activityCopy(item);
        return <li key={item.event_id} className="relative pb-5 last:pb-0">
          <span className="absolute -left-[1.62rem] top-1.5 h-2 w-2 rounded-full bg-primary ring-4 ring-background" />
          <button type="button" onClick={() => p.onInspectorChange({ kind: "event", eventId: item.event_id })} className="block w-full rounded-lg p-2 text-left hover:bg-surface">
            <span className="flex flex-wrap items-center justify-between gap-2"><b>{copy.label}</b><time className="text-xs text-text-tertiary">{stamp(item.created_at)}</time></span>
            <p className="mt-1 line-clamp-2 text-sm text-text-secondary">{copy.summary}</p>
          </button>
        </li>;
      })}
    </ol>
    {!activity.length ? <p className="rounded-lg border border-dashed border-border p-8 text-center text-text-secondary">No activity yet.</p> : null}
    <details className="mt-5 rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-sm font-semibold">Diagnostics</summary>
      <p className="mt-1 text-xs text-text-tertiary">Raw identifiers stay hidden from the workspace.</p>
      <dl className="mt-3 grid gap-1 text-xs text-text-secondary"><div>Events: {p.workspace!.log.events.length}</div><div>Latest sequence: {p.workspace!.log.latest_seq}</div><div>More available: {p.workspace!.log.has_more ? "Yes" : "No"}</div></dl>
    </details>
  </div>;
}

function Diagnostics({ children }: { children: React.ReactNode }) {
  return <details className="mt-4 rounded-lg border border-border p-3 text-xs"><summary className="cursor-pointer font-semibold">Diagnostics</summary><div className="mt-2 space-y-1 break-all font-mono text-text-secondary">{children}</div></details>;
}

function Inspector({ p, room }: { p: RoomsWorkspaceProps; room: RoomSummary }) {
  const workspace = p.workspace!;
  const attempt = p.inspector.kind === "attempt" ? workspace.attempts[p.inspector.attemptIndex] : undefined;
  const taskId = p.inspector.kind === "task" ? p.inspector.taskId : attempt?.identity.task_id;
  const task = workspace.tasks.find((item) => item.task_id === taskId || item.latest_attempt?.identity.task_id === taskId);
  const eventId = p.inspector.kind === "event" ? p.inspector.eventId : undefined;
  const event = eventId ? workspace.activity.find((item) => item.event_id === eventId) : undefined;
  const member = task?.owner ? p.topology?.members.find((item) => item.handle === task.owner || item.member_id === task.owner) : undefined;
  return <aside aria-label="Context inspector" className="border-t border-border bg-surface/20 p-4 xl:min-h-0 xl:overflow-y-auto xl:border-l xl:border-t-0">
    <p className="text-xs font-semibold uppercase tracking-[0.15em] text-primary">Context</p>
    <h2 className="mt-1 text-xl font-bold">{event?.title ?? task?.subject ?? room.name}</h2>
    {event ? <><p className="mt-3 text-sm leading-relaxed text-text-secondary">{event.summary}</p>{event.task_id ? <Button className="mt-3" size="xs" onClick={() => p.onInspectorChange({ kind: "task", taskId: event.task_id! })}>Open related task</Button> : null}<EventDiagnostics event={event} /></> : null}
    {task ? <div className="mt-3 space-y-3"><Badge tone={tone(attempt?.status ?? task.status)}>{humanize(attempt?.status ?? task.status)}</Badge><div><small className="block text-text-tertiary">Owner</small><p>{task.owner ? `@${task.owner}` : "Unassigned"}{member ? <span className="text-text-secondary"> · {roleLabel(member.role)}</span> : null}</p></div>{task.description ? <p className="text-sm leading-relaxed text-text-secondary">{task.description}</p> : null}{task.pending_actions[0] ? <Button onClick={() => p.onOpenActionCenter(task.pending_actions[0])}>Review action</Button> : null}{attempt ? <Diagnostics><div>Room context available</div><div>Conversation context available</div><div>Attempt reference available</div><div>Execution generation: {attempt.execution_generation}</div><div>Cancel generation: {attempt.cancel_generation}</div><div>Created: {stamp(attempt.created_at)}</div><div>Updated: {stamp(attempt.updated_at)}</div><div>Started: {stamp(attempt.started_at)}</div><div>Terminal: {stamp(attempt.terminal_at)}</div><div>Settlement: {attempt.settlement_status ?? "—"}</div><div className="font-sans text-warning">Payload and result are redacted.</div></Diagnostics> : null}</div> : null}
    {!event && !task ? <div className="mt-4 space-y-3 text-sm"><div className="rounded-lg border border-border bg-background p-3"><span className="flex items-center gap-2 font-semibold"><ShieldCheck className="h-4 w-4 text-primary" /> Team health</span><p className="mt-2 text-text-secondary">{p.replicationHealth?.healthy === false ? "Some teammates need attention." : "Team connections are operating normally."}</p></div><dl className="grid gap-2"><div><dt className="text-xs text-text-tertiary">Connected peers</dt><dd>{p.replicationHealth?.ready ?? 0} of {p.replicationHealth?.total_peers ?? p.peerGrants?.length ?? 0}</dd></div><div><dt className="text-xs text-text-tertiary">Observer</dt><dd>{humanize(p.observer?.state ?? "Unknown")}</dd></div><div><dt className="text-xs text-text-tertiary">Policy events</dt><dd>{p.policyTrace?.event_count ?? 0}</dd></div><div><dt className="text-xs text-text-tertiary">Team members</dt><dd>{p.topology?.members.length ?? room.members.length}</dd></div></dl><Diagnostics><div>Room context available</div><div>Revision: {room.revision}</div><div>Latest sequence: {room.latest_seq}</div></Diagnostics></div> : null}
  </aside>;
}

function EventDiagnostics({ event }: { event: RoomActivityItem }) {
  return <Diagnostics><div>Kind: {event.kind}</div><div>Sequence: {event.seq}</div>{event.task_id ? <div>Related task available</div> : null}{event.thread_id ? <div>Conversation context available</div> : null}</Diagnostics>;
}

const ACTION_DETAIL_ALLOWLIST = ["tool_name", "scope", "status", "execution_generation", "step_count", "reason"] as const;
function actionPresentation(kind: unknown) {
  switch (kind) {
    case "permission": return { title: "Permission", approve: "Allow once", deny: true };
    case "plan_approval": return { title: "Plan approval", approve: "Approve plan", deny: true };
    case "shutdown": return { title: "Shutdown", approve: "Confirm shutdown", deny: true };
    case "retry": return { title: "Retry", approve: "Retry", deny: false };
    default: return null;
  }
}
function ActionDialog({ p }: { p: RoomsWorkspaceProps }) {
  const action = p.actionCenterAction;
  if (!action) return null;
  const presentation = actionPresentation(action.kind);
  const safe = Object.fromEntries(Object.entries(action.detail).filter(([key, value]) => ACTION_DETAIL_ALLOWLIST.includes(key as typeof ACTION_DETAIL_ALLOWLIST[number]) && (typeof value === "string" || typeof value === "number" || typeof value === "boolean")));
  return <Dialog open onOpenChange={(open) => { if (!open) p.onCloseActionCenter(); }}><DialogContent><DialogHeader><DialogTitle>Action Center · {presentation?.title ?? "Unsupported action"}</DialogTitle><DialogDescription>Requested by @{action.from_handle}</DialogDescription></DialogHeader><p>{action.description}</p><dl className="rounded-lg border border-border p-3 text-xs">{Object.entries(safe).map(([key, value]) => <div key={key} className="flex justify-between gap-3 py-1"><dt className="capitalize text-text-secondary">{humanize(key)}</dt><dd className="text-right">{String(value)}</dd></div>)}</dl><p className="flex items-center gap-1 text-xs text-text-tertiary"><ShieldCheck className="h-3.5 w-3.5" /> Sensitive action detail is redacted.</p><DialogFooter>{presentation ? <>{presentation.deny ? <Button type="button" outlined disabled={p.actionBusy} onClick={() => p.onDenyAction(action)}>Deny</Button> : null}<Button type="button" disabled={p.actionBusy} onClick={() => p.onApproveAction(action)}>{presentation.approve}</Button></> : <Button type="button" outlined onClick={p.onCloseActionCenter}>Close</Button>}</DialogFooter></DialogContent></Dialog>;
}

export function RoomsWorkspace(p: RoomsWorkspaceProps) {
  const rooms = useMemo(() => filterRoomInbox(p.rooms, p.search, p.preset), [p.rooms, p.search, p.preset]);
  const baseRoom = p.workspace?.room ?? p.rooms.find((item) => item.room_id === p.selectedRoomId);
  const summary = p.rooms.find((item) => item.room_id === p.selectedRoomId)?.workspace;
  const room = baseRoom ? { ...baseRoom, workspace: summary ?? baseRoom.workspace } : undefined;
  if (!room || !p.workspace) return <div className="grid min-h-[36rem] overflow-hidden rounded-lg border border-border md:grid-cols-[18rem_1fr]"><Inbox p={p} rooms={rooms} /><main className="hidden place-items-center bg-surface/20 md:grid"><div className="text-center"><MessageSquareText className="mx-auto mb-3 h-8 w-8 text-text-tertiary" /><h1 className="text-lg font-semibold">Choose a team room</h1><p className="mt-1 text-sm text-text-secondary">Select an inbox item to see its work and conversation.</p></div></main></div>;
  const actions = p.workspace.pending_actions;
  return <div className={`grid min-h-[36rem] overflow-hidden rounded-lg border border-border md:grid-cols-[18rem_minmax(0,1fr)] xl:h-[calc(100vh-8rem)] xl:grid-cols-[18rem_minmax(30rem,1fr)_21rem] ${p.className ?? ""}`}>
    <Inbox p={p} rooms={rooms} />
    <main className="min-h-0 overflow-y-auto bg-background"><Header p={p} room={room} /><section role="tabpanel" className="p-3 sm:p-4 lg:p-5">
      {actions.length ? <section aria-label="Action Center" className="mb-4 space-y-2"><h2 className="flex items-center gap-2 text-sm font-semibold"><AlertTriangle className="h-4 w-4 text-warning" /> Action Center</h2>{actions.map((action) => <div key={action.action_id} className="flex flex-col justify-between gap-3 rounded-lg border border-warning/40 bg-warning/10 p-3 sm:flex-row sm:items-center"><span><b>{action.kind === "retry" ? "Retry required" : "Your decision is needed"}</b><span className="block text-sm text-text-secondary">{action.description}</span></span><Button className="shrink-0" size="xs" onClick={() => p.onOpenActionCenter(action)}>Review {action.kind}</Button></div>)}</section> : null}
      {p.tab === "tasks" ? <Tasks p={p} room={room} /> : p.tab === "conversation" ? <Conversation p={p} /> : <Timeline p={p} />}
    </section></main>
    <Inspector p={p} room={room} />
    <ActionDialog p={p} />
  </div>;
}

export default RoomsWorkspace;
