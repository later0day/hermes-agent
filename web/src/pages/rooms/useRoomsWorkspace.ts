import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type ObserverStatus,
  type PeerGrant,
  type PendingAction,
  type PolicyTraceResponse,
  type ReplicationHealthResponse,
  type RoomActionResponse,
  type RoomSummary,
  type RoomTopologyResponse,
  type RoomWorkspaceResponse,
} from "@/lib/api";
import { sortRoomsByPriority } from "./room-model";

export type RoomsWorkspaceTab = "tasks" | "conversation" | "activity";
export type RoomsWorkspaceMode = "list" | "graph" | "attempts";
export type RoomsWorkspacePreset = "all" | "needs_action" | "failed" | "running" | "idle";

const requireSuccessfulAction = (response: RoomActionResponse): void => {
  if (!response.ok) throw new Error(response.message || "Room action failed");
};

export interface RoomsWorkspaceUrlState {
  roomId: string | null;
  tab: RoomsWorkspaceTab;
  taskId: string | null;
  mode: RoomsWorkspaceMode;
  preset: RoomsWorkspacePreset;
  search: string;
}

const TABS: RoomsWorkspaceTab[] = ["tasks", "conversation", "activity"];
const MODES: RoomsWorkspaceMode[] = ["list", "graph", "attempts"];
const PRESETS: RoomsWorkspacePreset[] = ["all", "needs_action", "failed", "running", "idle"];
const OWNED_QUERY_KEYS = ["room", "tab", "task", "view", "filter", "search"] as const;

function oneOf<T extends string>(value: string | null, choices: readonly T[], fallback: T): T {
  return value && choices.includes(value as T) ? value as T : fallback;
}

export function parseRoomsWorkspaceUrl(search: string): RoomsWorkspaceUrlState {
  const params = new URLSearchParams(search);
  return {
    roomId: params.get("room") || null,
    tab: oneOf(params.get("tab") === "workspace" ? "tasks" : params.get("tab"), TABS, "tasks"),
    taskId: params.get("task") || null,
    mode: oneOf(params.get("view") === "board" ? "list" : params.get("view"), MODES, "list"),
    preset: oneOf(params.get("filter"), PRESETS, "all"),
    search: params.get("search") || "",
  };
}

/** Returns a query string while preserving parameters owned by other pages/features. */
export function mergeRoomsWorkspaceUrl(search: string, state: RoomsWorkspaceUrlState): string {
  const params = new URLSearchParams(search);
  for (const key of OWNED_QUERY_KEYS) params.delete(key);
  if (state.roomId) params.set("room", state.roomId);
  if (state.tab !== "tasks") params.set("tab", state.tab);
  if (state.taskId) params.set("task", state.taskId);
  if (state.mode !== "list") params.set("view", state.mode);
  if (state.preset !== "all") params.set("filter", state.preset);
  if (state.search) params.set("search", state.search);
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export function selectAvailableRoom(
  requested: string | null,
  rooms: readonly Pick<RoomSummary, "room_id">[],
): string | null {
  if (requested && rooms.some((room) => room.room_id === requested)) return requested;
  return rooms[0]?.room_id ?? null;
}

export function selectAvailableTask(
  requested: string | null,
  workspace: Pick<RoomWorkspaceResponse, "tasks"> | null,
): string | null {
  if (requested && workspace?.tasks.some((task) => task.task_id === requested)) return requested;
  return null;
}

/** Small independently testable monotonic guard used by both list and room reads. */
export function createLatestRequestGuard(): { next: () => number; current: (token: number) => boolean } {
  let latest = 0;
  return { next: () => ++latest, current: (token) => token === latest };
}

interface Ancillary<T> { data: T | null; loading: boolean; error: string | null }
interface AncillaryState {
  topology: Ancillary<RoomTopologyResponse>;
  pendingActions: Ancillary<PendingAction[]>;
  observer: Ancillary<ObserverStatus>;
  peerGrants: Ancillary<PeerGrant[]>;
  replication: Ancillary<ReplicationHealthResponse>;
  policy: Ancillary<PolicyTraceResponse>;
}
const emptyAncillary = <T,>(): Ancillary<T> => ({ data: null, loading: false, error: null });
const initialAncillary = (): AncillaryState => ({
  topology: emptyAncillary(), pendingActions: emptyAncillary(), observer: emptyAncillary(),
  peerGrants: emptyAncillary(), replication: emptyAncillary(), policy: emptyAncillary(),
});

export interface UseRoomsWorkspaceOptions {
  requireSuccess?: (response: RoomActionResponse) => void;
  onError?: (error: unknown) => void;
}

export function useRoomsWorkspace(options: UseRoomsWorkspaceOptions = {}) {
  const initial = useMemo(() => typeof window === "undefined" ? parseRoomsWorkspaceUrl("") : parseRoomsWorkspaceUrl(window.location.search), []);
  const [rooms, setRooms] = useState<RoomSummary[] | null>(null);
  const [showDisbanded, setShowDisbanded] = useState(false);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(initial.roomId);
  const [search, setSearch] = useState(initial.search);
  const [preset, setPreset] = useState<RoomsWorkspacePreset>(initial.preset);
  const [tab, setTab] = useState<RoomsWorkspaceTab>(initial.tab);
  const [mode, setMode] = useState<RoomsWorkspaceMode>(initial.mode);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initial.taskId);
  const [workspace, setWorkspace] = useState<RoomWorkspaceResponse | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingWorkspace, setLoadingWorkspace] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [ancillary, setAncillary] = useState<AncillaryState>(initialAncillary);
  const [busyActionId, setBusyActionId] = useState<string | null>(null);
  const listGuard = useRef(createLatestRequestGuard());
  const roomGuard = useRef(createLatestRequestGuard());
  const mounted = useRef(true);
  const onError = options.onError;
  const requireSuccess = options.requireSuccess ?? requireSuccessfulAction;

  useEffect(() => () => { mounted.current = false; listGuard.current.next(); roomGuard.current.next(); }, []);

  const loadList = useCallback(async () => {
    const token = listGuard.current.next();
    setLoadingList(true); setListError(null);
    try {
      const response = await api.listRooms(showDisbanded);
      if (!mounted.current || !listGuard.current.current(token)) return;
      const next = sortRoomsByPriority(response.rooms);
      setRooms(next);
      setSelectedRoomId((current) => selectAvailableRoom(current, next));
    } catch (error) {
      if (mounted.current && listGuard.current.current(token)) { setListError(String(error)); onError?.(error); }
    } finally {
      if (mounted.current && listGuard.current.current(token)) setLoadingList(false);
    }
  }, [showDisbanded, onError]);

  const loadRoom = useCallback(async (roomId: string | null) => {
    const token = roomGuard.current.next();
    if (!roomId) { setWorkspace(null); setSelectedTaskId(null); setAncillary(initialAncillary()); setLoadingWorkspace(false); return; }
    setLoadingWorkspace(true); setWorkspaceError(null); setWorkspace(null);
    setAncillary(Object.fromEntries(Object.keys(initialAncillary()).map((key) => [key, { data: null, loading: true, error: null }])) as unknown as AncillaryState);
    const commit = <K extends keyof AncillaryState>(key: K, data: AncillaryState[K]["data"] | null, error: unknown = null) => {
      if (!mounted.current || !roomGuard.current.current(token)) return;
      setAncillary((previous) => ({ ...previous, [key]: { data, loading: false, error: error ? String(error) : null } }));
    };
    void api.getRoomTopology(roomId).then((r) => commit("topology", r)).catch((e) => commit("topology", null, e));
    void api.getRoomPendingActions(roomId).then((r) => commit("pendingActions", r.actions)).catch((e) => commit("pendingActions", null, e));
    void api.getObserverStatus(roomId).then((r) => commit("observer", r)).catch((e) => commit("observer", null, e));
    void api.getRoomPeerGrants(roomId).then((r) => commit("peerGrants", r.peer_grants)).catch((e) => commit("peerGrants", null, e));
    void api.getRoomReplicationHealth(roomId).then((r) => commit("replication", r)).catch((e) => commit("replication", null, e));
    void api.getRoomPolicyTrace(roomId).then((r) => commit("policy", r)).catch((e) => commit("policy", null, e));
    try {
      const response = await api.getRoomWorkspace(roomId);
      if (!mounted.current || !roomGuard.current.current(token)) return;
      setWorkspace(response);
      setAncillary((previous) => ({
        ...previous,
        pendingActions: { data: response.pending_actions, loading: false, error: previous.pendingActions.error },
      }));
      setSelectedTaskId((current) => selectAvailableTask(current, response));
    } catch (error) {
      if (mounted.current && roomGuard.current.current(token)) { setWorkspaceError(String(error)); onError?.(error); }
    } finally {
      if (mounted.current && roomGuard.current.current(token)) setLoadingWorkspace(false);
    }
  }, [onError]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadList(), 0);
    return () => window.clearTimeout(timeout);
  }, [loadList]);
  useEffect(() => {
    const timeout = window.setTimeout(() => void loadRoom(selectedRoomId), 0);
    return () => window.clearTimeout(timeout);
  }, [loadRoom, selectedRoomId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = mergeRoomsWorkspaceUrl(window.location.search, { roomId: selectedRoomId, tab, taskId: selectedTaskId, mode, preset, search });
    window.history.replaceState(window.history.state, "", window.location.pathname + query + window.location.hash);
  }, [selectedRoomId, tab, selectedTaskId, mode, preset, search]);

  const refresh = useCallback(async () => { await Promise.all([loadList(), loadRoom(selectedRoomId)]); }, [loadList, loadRoom, selectedRoomId]);
  const act = useCallback(async (decision: "approve" | "deny", actionId: string) => {
    if (!selectedRoomId || busyActionId) return false;
    setBusyActionId(actionId);
    try {
      const response = await (decision === "approve" ? api.approveRoomAction(selectedRoomId, actionId) : api.denyRoomAction(selectedRoomId, actionId));
      requireSuccess(response);
      setAncillary((previous) => ({ ...previous, pendingActions: { ...previous.pendingActions, data: previous.pendingActions.data?.filter((item) => item.action_id !== actionId) ?? null } }));
      setWorkspace((previous) => previous ? { ...previous, pending_actions: previous.pending_actions.filter((item) => item.action_id !== actionId) } : null);
      return true;
    } catch (error) { onError?.(error); return false; }
    finally { if (mounted.current) setBusyActionId(null); }
  }, [selectedRoomId, busyActionId, requireSuccess, onError]);

  return {
    rooms, showDisbanded, setShowDisbanded, selectedRoomId, setSelectedRoomId,
    search, setSearch, preset, setPreset, tab, setTab, mode, setMode, selectedTaskId, setSelectedTaskId,
    workspace, loadingList, loadingWorkspace, listError, workspaceError, ancillary, busyActionId,
    refresh, refreshList: loadList, refreshRoom: () => loadRoom(selectedRoomId),
    approveAction: (actionId: string) => act("approve", actionId),
    denyAction: (actionId: string) => act("deny", actionId),
  };
}
