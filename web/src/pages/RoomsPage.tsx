import { useCallback, useEffect, useMemo, useState } from "react";
import { Home, RotateCw, Users } from "lucide-react";
import {
  api,
  type RoomDetailResponse,
  type RoomEvent,
  type RoomLogResponse,
  type RoomSummary,
} from "@/lib/api";
import { useI18n } from "@/i18n";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";

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

/** Extract a human-readable summary line from an event payload. */
function eventContent(ev: RoomEvent): string {
  const p = ev.payload || {};
  const text = typeof p.text === "string" ? p.text : "";
  switch (ev.kind) {
    case "message.user":
      return text;
    case "message.member": {
      const member = typeof p.member_id === "string" ? p.member_id : "?";
      return `@${member}: ${text}`;
    }
    case "turn.requested":
      return `[dispatch] member=${p.member_id ?? "?"} round=${p.round_index ?? "?"} task=${(p.task_id as string)?.slice(0, 20) ?? ""}`;
    case "turn.settled": {
      const passed = p.passed ? " (pass)" : "";
      return `[settled] member=${p.member_id ?? "?"} round=${p.round_index ?? "?"}${passed}`;
    }
    case "turn.cancelled":
      return `[cancelled] ${p.reason ?? ""}`;
    case "room.activity":
      return `[${p.status ?? "activity"}] ${p.reason_code ?? ""}`;
    default:
      return "";
  }
}

/** Truncate text for display in a table cell. */
function truncateText(text: string, max = 200): string {
  if (!text) return "";
  if (text.length <= max) return text;
  return text.slice(0, max).trimEnd() + "…";
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

  const loadList = useCallback(() => {
    let cancelled = false;
    setLoadingList(true);
    api
      .listRooms(showDisbanded)
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
    return () => {
      cancelled = true;
    };
  }, [showDisbanded, showToast, t]);

  useEffect(() => loadList(), [loadList]);

  const loadDetail = useCallback(
    (roomId: string) => {
      let cancelled = false;
      setLoadingDetail(true);
      setDetail(null);
      setLog(null);
      Promise.all([api.getRoom(roomId), api.getRoomLog(roomId, 0, 100)])
        .then(([d, lg]) => {
          if (cancelled) return;
          setDetail(d);
          setLog(lg);
        })
        .catch(() => !cancelled && showToast(t.common.loading, "error"))
        .finally(() => !cancelled && setLoadingDetail(false));
      return () => {
        cancelled = true;
      };
    },
    [showToast, t],
  );

  useEffect(() => {
    if (selected) return loadDetail(selected);
    setDetail(null);
    setLog(null);
  }, [selected, loadDetail]);

  const loadMore = useCallback(() => {
    if (!selected || !log) return;
    api
      .getRoomLog(selected, log.cursor, 100)
      .then((more) =>
        setLog((prev) =>
          prev
            ? { ...more, events: [...prev.events, ...more.events] }
            : more,
        ),
      )
      .catch(() => showToast(t.common.loading, "error"));
  }, [selected, log, showToast, t]);

  const driverLabel = useMemo(() => {
    const s = detail?.driver_status;
    if (!s) return L.idle;
    if (s.blocked) return L.blocked;
    if (s.working) return L.working;
    if (s.running) return L.running;
    return L.idle;
  }, [detail, L]);

  return (
    <div className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-4 p-4 lg:grid-cols-[320px_1fr]">
      {/* Room list */}
      <Card className="rounded-none">
        <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Home className="h-4 w-4 text-muted-foreground" />
              {L.title}
            </CardTitle>
            <CardDescription className="text-xs">{L.description}</CardDescription>
          </div>
          <Button
            ghost
            size="xs"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => loadList()}
            disabled={loadingList}
            aria-label={t.common.refresh}
          >
            <RotateCw />
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showDisbanded}
              onChange={(e) => setShowDisbanded(e.target.checked)}
            />
            {L.showDisbanded}
          </label>
          {loadingList ? (
            <div className="flex min-h-[120px] items-center justify-center">
              <Spinner />
            </div>
          ) : !rooms || rooms.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {L.empty}
            </p>
          ) : (
            <ul className="space-y-1">
              {rooms.map((r) => (
                <li key={r.room_id}>
                  <button
                    type="button"
                    onClick={() => setSelected(r.room_id)}
                    className={`w-full border px-3 py-2 text-left text-sm transition-colors ${
                      selected === r.room_id
                        ? "border-ring bg-accent"
                        : "border-input hover:bg-accent/50"
                    } ${r.disbanded_at ? "opacity-60" : ""}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{r.name}</span>
                      <span className="flex items-center gap-1 text-xs text-muted-foreground">
                        <Users className="h-3 w-3" />
                        {r.members.length}
                      </span>
                    </div>
                    <div className="truncate font-mono text-xs text-muted-foreground">
                      {r.room_id}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Detail + event log */}
      <div className="space-y-4">
        {!selected ? (
          <Card className="rounded-none">
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              {L.selectRoom}
            </CardContent>
          </Card>
        ) : loadingDetail ? (
          <Card className="rounded-none">
            <CardContent className="flex min-h-[200px] items-center justify-center">
              <Spinner />
            </CardContent>
          </Card>
        ) : detail ? (
          <>
            <Card className="rounded-none">
              <CardHeader className="space-y-0">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Home className="h-4 w-4 text-muted-foreground" />
                  {detail.room.name}
                  <span className="font-mono text-xs font-normal text-muted-foreground">
                    {detail.room.room_id}
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div>
                  <span className="text-xs uppercase text-muted-foreground">
                    {L.members} ({detail.room.members.length})
                  </span>
                  <p className="mt-1">{memberHandles(detail.room.members)}</p>
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
                  <MetaCell label={L.driver} value={driverLabel} />
                  <MetaCell label={L.revision} value={String(detail.room.revision)} />
                  <MetaCell
                    label={L.latestSeq}
                    value={String(detail.room.latest_seq)}
                  />
                  <MetaCell
                    label={L.authorityEpoch}
                    value={String(detail.room.authority_epoch)}
                  />
                  <MetaCell
                    label={L.created}
                    value={fmtTs(detail.room.created_at)}
                  />
                  <MetaCell
                    label={L.updated}
                    value={fmtTs(detail.room.updated_at)}
                  />
                  {detail.room.disbanded_at ? (
                    <MetaCell
                      label={L.disbanded}
                      value={fmtTs(detail.room.disbanded_at)}
                    />
                  ) : null}
                  <MetaCell
                    label={L.authorityGateway}
                    value={detail.room.authority_gateway_id ?? "—"}
                  />
                </dl>
              </CardContent>
            </Card>

            <Card className="rounded-none">
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <CardTitle className="text-sm">{L.eventLog}</CardTitle>
                <Button
                  ghost
                  size="xs"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => selected && loadDetail(selected)}
                  aria-label={t.common.refresh}
                >
                  <RotateCw />
                </Button>
              </CardHeader>
              <CardContent>
                {!log || log.events.length === 0 ? (
                  <p className="py-6 text-center text-sm text-muted-foreground">
                    {L.noEvents}
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
                        {log.events.map((ev) => {
                          const content = truncateText(eventContent(ev), 300);
                          const isUser = ev.kind === "message.user";
                          const isMember = ev.kind === "message.member";
                          return (
                            <tr key={ev.seq} className="border-t border-input/50 align-top">
                              <td className="py-1 pr-3 tabular-nums whitespace-nowrap">{ev.seq}</td>
                              <td className="py-1 pr-3 whitespace-nowrap">{ev.kind}</td>
                              <td className={
                                "py-1 pr-3 max-w-[400px] " +
                                (isUser ? "text-blue-600 dark:text-blue-400" :
                                 isMember ? "text-green-600 dark:text-green-400" : "text-muted-foreground")
                              }>
                                {content || "—"}
                              </td>
                              <td className="py-1 pr-3 text-muted-foreground whitespace-nowrap">
                                {fmtTs(ev.created_at)}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    {log.has_more && (
                      <div className="mt-3 flex justify-center">
                        <Button ghost size="xs" onClick={loadMore}>
                          {L.loadMore}
                        </Button>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          </>
        ) : null}
      </div>
      <Toast toast={toast} />
    </div>
  );
}

function MetaCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="uppercase text-muted-foreground">{label}</dt>
      <dd className="truncate">{value}</dd>
    </div>
  );
}
