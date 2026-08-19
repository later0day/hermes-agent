import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Bot,
  Check,
  ChevronRight,
  Copy,
  Edit3,

  Link2,
  Loader2,
  MessageSquare,
  Plus,
  Route,
  Sparkles,
  Trash2,
  Users,
  Wrench,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@nous-research/ui/ui/components/dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { api } from "@/lib/api";
import type { ProfileInfo, RoomMessageRow, RoomPlan, RoomRecord, RoomToolCall } from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";

// Regex matching the backend's profile-name validation.
const ROOM_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

// ─────────────────────────────────────────────────────────────────────────
// Chat rendering helpers
// ─────────────────────────────────────────────────────────────────────────

function formatChatTime(unixSec: number): string {
  if (!unixSec || unixSec <= 0) return "";
  const d = new Date(unixSec * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const hh = d.getHours().toString().padStart(2, "0");
  const mm = d.getMinutes().toString().padStart(2, "0");
  if (sameDay) return `${hh}:${mm}`;
  const mo = (d.getMonth() + 1).toString().padStart(2, "0");
  const day = d.getDate().toString().padStart(2, "0");
  return `${mo}/${day} ${hh}:${mm}`;
}

// Deterministic pastel-ish color for a member name → avatar background.
function avatarColorFor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) & 0xffffff;
  }
  const hue = h % 360;
  return `hsl(${hue}, 65%, 55%)`;
}

function avatarInitials(name: string): string {
  if (!name) return "?";
  if (name === "You") return "Y";
  // Take first char of each underscore-separated part, up to 2 chars
  const parts = name.split(/[_\-\s]+/).filter(Boolean);
  if (parts.length === 0) return name[0].toUpperCase();
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

// A fenced code block with a header bar (language label + copy button),
// horizontal scroll, and readable monospace typography. Rendered inside
// chat bubbles for member/observer replies that paste code.
function CodeBlock({ lang, code }: { lang: string; code: string }): React.ReactNode {
  const [copied, setCopied] = React.useState(false);
  const onCopy = React.useCallback(() => {
    const text = code;
    const done = () => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        /* clipboard blocked — ignore */
      });
    } else {
      done();
    }
  }, [code]);

  return (
    <div className="room-surface my-2 overflow-hidden bg-secondary/60 text-left">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-3 py-1.5">
        <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          {lang || "code"}
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="room-chip flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          aria-label="Copy code"
          title="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" /> Copied
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" /> Copy
            </>
          )}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 text-[13px] leading-relaxed text-foreground">
        <code className="font-mono">{code}</code>
      </pre>
    </div>
  );
}

// Minimal markdown → JSX renderer supporting fenced code blocks,
// inline code, bold, italic, and preserved newlines. Deliberately
// small — no external dep. Only what's needed for chat bubbles.
function renderChatContent(text: string): React.ReactNode {
  if (!text) return null;

  // Split on fenced code blocks: ```lang\n...\n```
  const parts: React.ReactNode[] = [];
  const re = /```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(renderInline(text.slice(last, m.index), key++));
    }
    const lang = m[1] || "";
    // Trim the trailing newline the closing fence leaves behind so the
    // block doesn't render an empty last line.
    const code = m[2].replace(/\n$/, "");
    parts.push(<CodeBlock key={key++} lang={lang} code={code} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parts.push(renderInline(text.slice(last), key++));
  }
  return <>{parts}</>;
}

function renderInline(text: string, k: number): React.ReactNode {
  // Split on inline code `...`, bold **...**, italic *...*, preserving order.
  // Simple approach: walk char by char.
  const nodes: React.ReactNode[] = [];
  let buf = "";
  let i = 0;
  let subkey = 0;
  const flush = () => {
    if (buf) {
      // Preserve newlines within a text run.
      nodes.push(
        <span key={`${k}-t-${subkey++}`} className="whitespace-pre-wrap">
          {buf}
        </span>,
      );
      buf = "";
    }
  };
  while (i < text.length) {
    // Inline code
    if (text[i] === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i) {
        flush();
        nodes.push(
          <code
            key={`${k}-c-${subkey++}`}
            className="room-chip bg-secondary/70 px-1 py-0.5 text-xs font-mono"
          >
            {text.slice(i + 1, end)}
          </code>,
        );
        i = end + 1;
        continue;
      }
    }
    // Bold **
    if (text[i] === "*" && text[i + 1] === "*") {
      const end = text.indexOf("**", i + 2);
      if (end > i + 2) {
        flush();
        nodes.push(
          <strong key={`${k}-b-${subkey++}`}>{text.slice(i + 2, end)}</strong>,
        );
        i = end + 2;
        continue;
      }
    }
    // Italic *  (avoid matching bold)
    if (text[i] === "*" && text[i + 1] !== "*") {
      const end = text.indexOf("*", i + 1);
      if (end > i + 1 && text[end + 1] !== "*") {
        flush();
        nodes.push(
          <em key={`${k}-i-${subkey++}`}>{text.slice(i + 1, end)}</em>,
        );
        i = end + 1;
        continue;
      }
    }
    buf += text[i];
    i++;
  }
  flush();
  return <React.Fragment key={`inl-${k}`}>{nodes}</React.Fragment>;
}

// ─────────────────────────────────────────────────────────────────────────
// Observer routing + tool-call rendering (beautifului "Thinking" + "Tool
// chips"). The backend persists the observer's route_to_member decision and
// members' tool_calls into the shared message store; these helpers surface
// that data instead of dropping it.
// ─────────────────────────────────────────────────────────────────────────

interface RouteInfo {
  members: string[];      // routed target member(s)
  reason: string;         // why the observer routed here
  isNewTopic?: boolean;
  matched?: boolean;      // first-hop classifier: real domain match vs fallback
}

// Parse a route_to_member tool_call's arguments into a RouteInfo.
function parseRouteToolCall(tcs: RoomToolCall[] | null | undefined): RouteInfo | null {
  if (!tcs || tcs.length === 0) return null;
  for (const tc of tcs) {
    if (tc?.function?.name !== "route_to_member") continue;
    try {
      const args = JSON.parse(tc.function.arguments || "{}");
      const raw = args.member;
      const members = Array.isArray(raw) ? raw.map(String) : raw != null ? [String(raw)] : [];
      return {
        members,
        reason: String(args.reason || ""),
        isNewTopic: Boolean(args.is_new_topic),
        matched: args.matched === undefined ? undefined : Boolean(args.matched),
      };
    } catch {
      return null;
    }
  }
  return null;
}

// A compact chip for a tool call (name + optional first arg preview).
function ToolChip({ tc }: { tc: RoomToolCall }): React.ReactNode {
  const name = tc?.function?.name || "tool";
  let preview = "";
  try {
    const args = JSON.parse(tc?.function?.arguments || "{}");
    const firstKey = Object.keys(args)[0];
    if (firstKey) {
      const v = args[firstKey];
      preview = typeof v === "string" ? v : JSON.stringify(v);
      if (preview.length > 32) preview = preview.slice(0, 32) + "…";
    }
  } catch {
    /* ignore malformed args */
  }
  return (
    <span className="room-chip inline-flex items-center gap-1 border border-border bg-muted/60 px-1.5 py-0.5 text-[11px] font-mono">
      <Wrench className="h-3 w-3 opacity-70" />
      <span className="font-medium">{name}</span>
      {preview && <span className="text-muted-foreground">{preview}</span>}
    </span>
  );
}

// Expandable observer-routing trace (beautifului "Thinking" component). Shows
// the routed target(s) inline, expands to reveal the observer's reason.
function RoutingTrace({ route }: { route: RouteInfo }): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const targets = route.members.join(", ") || "—";
  return (
    <div className="flex justify-center">
      <div className="w-full max-w-[85%]">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="room-trace-toggle flex w-full items-center gap-1.5 px-2 py-1 text-[11px] text-muted-foreground"
          aria-expanded={open}
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 transition-transform",
              open && "rotate-90",
            )}
          />
          <Route className="h-3 w-3 shrink-0 opacity-70" />
          <span className="font-medium">Routed to {targets}</span>
          {route.matched === false && (
            <Badge tone="secondary" className="ml-1 text-[9px]">fallback</Badge>
          )}
          {route.isNewTopic && (
            <Badge tone="secondary" className="text-[9px]">new topic</Badge>
          )}
        </button>
        {open && route.reason && (
          <div className="mt-0.5 ml-[1.35rem] border-l-2 border-border pl-2 text-[11px] leading-relaxed text-muted-foreground">
            {route.reason}
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────

export default function RoomsPage() {
  const { setEnd } = usePageHeader();
  const [searchParams, setSearchParams] = useSearchParams();
  const { toast, showToast } = useToast();

  const [rooms, setRooms] = useState<RoomRecord[]>([]);
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(
    searchParams.get("room"),
  );
  const [pendingDeleteRoom, setPendingDeleteRoom] = useState<RoomRecord | null>(null);

  // Dialogs
  const [showCreate, setShowCreate] = useState(false);
  const [showPlanner, setShowPlanner] = useState(false);

  // Create form
  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createMembers, setCreateMembers] = useState<string[]>([]);
  const [createDefaultMember, setCreateDefaultMember] = useState("");
  const [creating, setCreating] = useState(false);

  // Planner
  const [planRequirement, setPlanRequirement] = useState("");
  const [planMaxMembers, setPlanMaxMembers] = useState(5);
  const [planning, setPlanning] = useState(false);
  const [plannedResult, setPlannedResult] = useState<RoomPlan | null>(null);
  const [plannedRoomName, setPlannedRoomName] = useState("");
  const [confirmingPlan, setConfirmingPlan] = useState(false);

  // Room editor state (for the currently selected room)
  
  const [editDescription, setEditDescription] = useState("");
  const [editMembers, setEditMembers] = useState<string[]>([]);
  const [editDefaultMember, setEditDefaultMember] = useState("");
  const [editing, setEditing] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);

  // Bind
  const [bindKey, setBindKey] = useState("");
  const [binding, setBinding] = useState(false);

  // Chat panel (dashboard-side messaging)
  interface ChatMessage {
    id: number;
    seq: number | null;   // store sequence (null for optimistic local messages)
    kind: "user" | "member" | "observer";
    sender: string;
    content: string;
    timestamp: number;    // unix seconds
    toolCalls?: RoomToolCall[];  // member tool calls → tool chips
    route?: RouteInfo;           // observer route_to_member → thinking trace
  }
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBroadcast, setChatBroadcast] = useState(false);
  const [chatSending, setChatSending] = useState(false);
  const chatSeqRef = useRef(0);
  const chatEndRef = useRef<HTMLDivElement | null>(null);

  // Convert shared-store message rows → chat bubbles. Keeps observer rows
  // (rendered as a thinking/routing trace) and member tool_calls (rendered
  // as tool chips) instead of dropping them. tool_result rows stay hidden —
  // they're raw routing plumbing with no user-facing value.
  const rowsToBubbles = useCallback((rows: RoomMessageRow[]): ChatMessage[] => {
    const bubbles: ChatMessage[] = [];
    chatSeqRef.current = 0;
    for (const m of rows) {
      if (m.sender_kind === "user") {
        chatSeqRef.current += 1;
        bubbles.push({
          id: chatSeqRef.current,
          seq: m.sequence,
          kind: "user",
          sender: m.sender_name === "dashboard" ? "You" : m.sender_name,
          content: m.content,
          timestamp: m.timestamp,
        });
      } else if (m.sender_kind === "member") {
        chatSeqRef.current += 1;
        bubbles.push({
          id: chatSeqRef.current,
          seq: m.sequence,
          kind: "member",
          sender: m.sender_name,
          content: m.content,
          timestamp: m.timestamp,
          toolCalls: m.tool_calls ?? undefined,
        });
      } else if (m.sender_kind === "observer") {
        // Only surface observer rows that carry a route decision; skip
        // empty/plumbing observer turns.
        const route = parseRouteToolCall(m.tool_calls);
        if (route) {
          chatSeqRef.current += 1;
          bubbles.push({
            id: chatSeqRef.current,
            seq: m.sequence,
            kind: "observer",
            sender: m.sender_name,
            content: m.content,
            timestamp: m.timestamp,
            route,
          });
        }
      }
      // tool_result rows omitted from the visible chat
    }
    return bubbles;
  }, []);

  const selectedRoom = useMemo(
    () => rooms.find((r) => r.room_id === selectedRoomId) ?? null,
    [rooms, selectedRoomId],
  );

  const loadRooms = useCallback(async () => {
    try {
      const [roomsResp, profilesResp] = await Promise.all([
        api.listRooms(),
        api.getProfiles(),
      ]);
      setRooms(roomsResp.rooms);
      setProfiles(profilesResp.profiles.filter((p) => !p.name.startsWith("room_")));
    } catch {
      showToast("Failed to load rooms", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  // Ref so useConfirmDelete's stable callback can invoke the freshest loadRooms
  const loadRoomsRef = useRef(loadRooms);
  useEffect(() => {
    loadRoomsRef.current = loadRooms;
  }, [loadRooms]);

  const roomDelete = useConfirmDelete({
    onDelete: useCallback(async (roomId: string) => {
      const room = rooms.find((r) => r.room_id === roomId);
      try {
        await api.deleteRoom(roomId);
        showToast(`Deleted ${room?.room_name ?? roomId}`, "success");
        if (selectedRoomId === roomId) setSelectedRoomId(null);
        await loadRoomsRef.current();
      } catch (err) {
        showToast("Delete failed", "error");
        throw err;
      }
    }, [rooms, selectedRoomId, showToast]),
  });

  useEffect(() => {
    loadRooms();
  }, [loadRooms]);

  useEffect(() => {
    setEnd(
      <div className="rooms-scope flex items-center gap-2">
        <Button outlined size="sm" onClick={() => setShowPlanner(true)}>
          <Sparkles className="mr-1 h-4 w-4" />
          Plan with AI
        </Button>
        <Button size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="mr-1 h-4 w-4" />
          New Room
        </Button>
      </div>,
    );
    return () => setEnd(null);
  }, [setEnd]);

  // Sync selectedRoomId → URL
  useEffect(() => {
    const current = searchParams.get("room");
    if (selectedRoomId && current !== selectedRoomId) {
      const params = new URLSearchParams(searchParams);
      params.set("room", selectedRoomId);
      setSearchParams(params, { replace: true });
    } else if (!selectedRoomId && current) {
      const params = new URLSearchParams(searchParams);
      params.delete("room");
      setSearchParams(params, { replace: true });
    }
  }, [selectedRoomId, searchParams, setSearchParams]);

  // Sync selected room → edit form
  useEffect(() => {
    if (selectedRoom) {
      setEditDescription(selectedRoom.description || "");
      setEditMembers([...selectedRoom.members]);
      setEditDefaultMember(selectedRoom.default_member || selectedRoom.members[0] || "");
      setEditing(false);
    }
  }, [selectedRoom]);

  // ─── Create room ────────────────────────────────────────────────────────
  const handleCreate = useCallback(async () => {
    if (!ROOM_NAME_RE.test(createName)) {
      showToast("Invalid name", "error");
      return;
    }
    if (createMembers.length === 0) {
      showToast("No members", "error");
      return;
    }
    setCreating(true);
    try {
      const resp = await api.createRoom({
        name: createName,
        members: createMembers,
        description: createDescription,
        default_member: createDefaultMember || createMembers[0],
      });
      showToast(`Room ${resp.room.room_name} created`, "success");
      setShowCreate(false);
      setCreateName("");
      setCreateDescription("");
      setCreateMembers([]);
      setCreateDefaultMember("");
      await loadRooms();
      setSelectedRoomId(resp.room.room_id);
    } catch {
      showToast("Create failed", "error");
    } finally {
      setCreating(false);
    }
  }, [createName, createMembers, createDescription, createDefaultMember, loadRooms, showToast]);

  // ─── Delete room ────────────────────────────────────────────────────────
  // Delete flow is driven by useConfirmDelete (roomDelete) which was
  // constructed at the top of the component. Clicking the delete button
  // calls roomDelete.requestDelete(room.room_id); confirmation dialog is
  // rendered near the bottom of the JSX.

  // ─── Save edit ──────────────────────────────────────────────────────────
  const handleSaveEdit = useCallback(async () => {
    if (!selectedRoom) return;
    if (editMembers.length === 0) {
      showToast("No members", "error");
      return;
    }
    setSavingEdit(true);
    try {
      const resp = await api.patchRoom(selectedRoom.room_id, {
        members: editMembers,
        description: editDescription,
        default_member: editDefaultMember || editMembers[0],
      });
      showToast(`Saved ${resp.room.room_name}`, "success");
      setEditing(false);
      await loadRooms();
    } catch {
      showToast("Save failed", "error");
    } finally {
      setSavingEdit(false);
    }
  }, [selectedRoom, editMembers, editDescription, editDefaultMember, loadRooms, showToast]);

  // ─── Planner ────────────────────────────────────────────────────────────
  const handlePlan = useCallback(async () => {
    if (!planRequirement.trim()) {
      showToast("Enter a requirement", "error");
      return;
    }
    setPlanning(true);
    setPlannedResult(null);
    try {
      const resp = await api.planRoom({
        requirement: planRequirement,
        max_members: planMaxMembers,
      });
      setPlannedResult(resp.plan);
    } catch {
      showToast("Planning failed", "error");
    } finally {
      setPlanning(false);
    }
  }, [planRequirement, planMaxMembers, showToast]);

  const handleConfirmPlan = useCallback(async () => {
    setConfirmingPlan(true);
    try {
      const resp = await api.confirmRoomPlan(
        plannedRoomName.trim() ? { room_name: plannedRoomName.trim() } : {},
      );
      showToast(`Room ${resp.room.room_name} created`, "success");
      setShowPlanner(false);
      setPlannedResult(null);
      setPlanRequirement("");
      setPlannedRoomName("");
      await loadRooms();
      setSelectedRoomId(resp.room.room_id);
    } catch {
      showToast("Confirmation failed", "error");
    } finally {
      setConfirmingPlan(false);
    }
  }, [plannedRoomName, loadRooms, showToast]);

  // ─── Bind ───────────────────────────────────────────────────────────────
  const handleBind = useCallback(async () => {
    if (!selectedRoom || !bindKey.trim()) return;
    setBinding(true);
    try {
      await api.bindRoom(selectedRoom.room_id, bindKey.trim());
      showToast("Room bound to source", "success");
      setBindKey("");
    } catch {
      showToast("Bind failed", "error");
    } finally {
      setBinding(false);
    }
  }, [selectedRoom, bindKey, showToast]);

  // ─── Load room history when selecting a room ────────────────────────────
  useEffect(() => {
    if (!selectedRoom) {
      setChatHistory([]);
      setChatInput("");
      chatSeqRef.current = 0;
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.listRoomMessages(selectedRoom.room_id, 100);
        if (cancelled) return;
        setChatHistory(rowsToBubbles(resp.messages));
        setChatInput("");
      } catch {
        // Non-fatal — just start with an empty chat if fetch fails
        setChatHistory([]);
        setChatInput("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedRoomId, selectedRoom, rowsToBubbles]);

  // ─── Send message from dashboard chat panel ─────────────────────────────
  const handleSendChat = useCallback(async () => {
    if (!selectedRoom || !chatInput.trim()) return;
    const messageText = chatInput.trim();

    // Optimistically show the user's message
    chatSeqRef.current += 1;
    const nowSec = Math.floor(Date.now() / 1000);
    setChatHistory((prev) => [
      ...prev,
      {
        id: chatSeqRef.current,
        seq: null,
        kind: "user",
        sender: "You",
        content: messageText,
        timestamp: nowSec,
      },
    ]);
    setChatInput("");
    setChatSending(true);

    try {
      const resp = await api.dispatchRoomMessage(
        selectedRoom.room_id,
        messageText,
        chatBroadcast,
      );
      const newBubbles: ChatMessage[] = [];
      for (const m of resp.target_members) {
        const reply = resp.replies[m] || "(no reply)";
        chatSeqRef.current += 1;
        newBubbles.push({
          id: chatSeqRef.current,
          seq: null,
          kind: "member",
          sender: m,
          content: reply,
          timestamp: Math.floor(Date.now() / 1000),
        });
      }
      // Single setState call so React batches all replies into the
      // same render. Previously we called setChatHistory((prev)=>[...])
      // inside the loop, but React batches multiple setState calls
      // and each closure captured the SAME `prev`, causing only the
      // last member's reply to actually land in state.
      setChatHistory((prev) => [...prev, ...newBubbles]);
      // After dispatch, reload history so we get real seq numbers (enables
      // delete) plus the persisted observer routing trace + tool_calls that
      // the optimistic bubbles above don't carry. Non-blocking, best-effort.
      try {
        const fresh = await api.listRoomMessages(selectedRoom.room_id, 100);
        setChatHistory(rowsToBubbles(fresh.messages));
      } catch { /* silent */ }
    } catch (err) {
      chatSeqRef.current += 1;
      setChatHistory((prev) => [
        ...prev,
        {
          id: chatSeqRef.current,
          seq: null,
          kind: "member",
          sender: "system",
          content: `Error: ${err instanceof Error ? err.message : String(err)}`,
          timestamp: Math.floor(Date.now() / 1000),
        },
      ]);
    } finally {
      setChatSending(false);
    }
  }, [selectedRoom, chatInput, chatBroadcast]);

  // Auto-scroll to bottom when history changes
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [chatHistory.length]);

  // Delete a message
  const handleDeleteMessage = useCallback(async (msg: ChatMessage) => {
    if (!selectedRoom || msg.seq == null) return;
    try {
      await api.deleteRoomMessage(selectedRoom.room_id, msg.seq);
      setChatHistory((prev) => prev.filter((m) => m.id !== msg.id));
      showToast("Message deleted", "success");
    } catch (err) {
      showToast(`Delete failed: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
  }, [selectedRoom, showToast]);

  // ─── Available profiles (not in the roster) ─────────────────────────────
  const availableForCreate = useMemo(
    () => profiles.filter((p) => !createMembers.includes(p.name)),
    [profiles, createMembers],
  );
  const availableForEdit = useMemo(
    () => profiles.filter((p) => !editMembers.includes(p.name)),
    [profiles, editMembers],
  );

  // ─── Render ─────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="rooms-root flex min-h-0 w-full min-w-0 flex-1 h-full">
      {/* ═══ Left: rooms list ═══ */}
      <aside className="w-72 border-r border-border overflow-y-auto shrink-0">
        <div className="p-3 space-y-1">
          {rooms.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              <Users className="mx-auto mb-2 h-8 w-8 opacity-40" />
              <p className="mb-3">No rooms yet</p>
              <Button size="sm" onClick={() => setShowPlanner(true)}>
                <Sparkles className="mr-1 h-4 w-4" />
                Plan your first room
              </Button>
            </div>
          ) : (
            rooms.map((room) => (
              <button
                key={room.room_id}
                type="button"
                onClick={() => setSelectedRoomId(room.room_id)}
                className={cn(
                  "room-tile group w-full px-3 py-2 text-left",
                  "hover:bg-accent focus:outline-none focus-visible:shadow-[0_0_0_1.5px_var(--color-ring)]",
                  selectedRoomId === room.room_id && "room-tile-selected",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <Bot className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="truncate font-medium text-sm">
                      {room.room_name}
                    </span>
                  </div>
                  <Badge tone="secondary" className="shrink-0">
                    {room.members.length}
                  </Badge>
                </div>
                {room.description && (
                  <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                    {room.description}
                  </p>
                )}
                <div className="mt-2 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setPendingDeleteRoom(room);
                      roomDelete.requestDelete(room.room_id);
                    }}
                    className="text-xs text-destructive hover:underline"
                    aria-label={`Delete ${room.room_name}`}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              </button>
            ))
          )}
        </div>
      </aside>

      {/* ═══ Center: room detail ═══ */}
      <main className="w-[440px] shrink-0 overflow-y-auto p-4 border-l border-border order-3">
        {!selectedRoom ? (
          <div className="flex h-full items-center justify-center text-center text-muted-foreground">
            <div>
              <MessageSquare className="mx-auto mb-4 h-12 w-12 opacity-30" />
              <p>Select a room from the sidebar to view details.</p>
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto space-y-6">
            {/* Room header */}
            <Card className="room-surface-raised border-border bg-card">
              <CardHeader>
                <div className="flex items-center justify-between gap-3">
                  <CardTitle className="text-2xl min-w-0 break-words">
                    {selectedRoom.room_name}
                  </CardTitle>
                  {!editing ? (
                    <Button outlined size="sm" onClick={() => setEditing(true)}>
                      <Edit3 className="mr-1 h-4 w-4" />
                      Edit
                    </Button>
                  ) : (
                    <div className="flex gap-2">
                      <Button outlined size="sm" onClick={() => {
                        setEditing(false);
                        if (selectedRoom) {
                          setEditDescription(selectedRoom.description || "");
                          setEditMembers([...selectedRoom.members]);
                          setEditDefaultMember(selectedRoom.default_member || selectedRoom.members[0] || "");
                        }
                      }}>
                        <X className="mr-1 h-4 w-4" />
                        Cancel
                      </Button>
                      <Button size="sm" onClick={handleSaveEdit} disabled={savingEdit}>
                        {savingEdit ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Check className="mr-1 h-4 w-4" />}
                        Save
                      </Button>
                    </div>
                  )}
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div>
                  <Label>Description</Label>
                  {editing ? (
                    <textarea
                      className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                      rows={3}
                      value={editDescription}
                      onChange={(e) => setEditDescription(e.target.value)}
                    />
                  ) : (
                    <p className="mt-1 text-sm text-muted-foreground">
                      {selectedRoom.description || "(no description)"}
                    </p>
                  )}
                </div>
                <div>
                  <Label>Observer profile</Label>
                  <p className="mt-1 font-mono text-sm">{selectedRoom.observer_profile}</p>
                </div>
              </CardContent>
            </Card>

            {/* Members */}
            <Card className="room-surface-raised border-border bg-card">
              <CardHeader>
                <CardTitle className="text-lg flex items-center gap-2">
                  <Users className="h-5 w-5" />
                  Members ({editing ? editMembers.length : selectedRoom.members.length})
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex flex-wrap gap-2 mb-3 min-w-0">
                  {(editing ? editMembers : selectedRoom.members).map((m) => {
                    const meta = profiles.find((p) => p.name === m);
                    return (
                      <div
                        key={m}
                        className="room-chip flex min-w-0 max-w-full items-center gap-2 border border-border bg-muted/40 px-3 py-1.5"
                      >
                        <span className="font-mono text-sm break-all">{m}</span>
                        {meta?.description && (
                          <span className="text-xs text-muted-foreground max-w-[16rem] truncate">
                            {meta.description}
                          </span>
                        )}
                        {editing && (
                          <button
                            type="button"
                            onClick={() =>
                              setEditMembers((prev) => prev.filter((x) => x !== m))
                            }
                            className="ml-1 text-muted-foreground hover:text-destructive"
                            aria-label={`Remove ${m}`}
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
                {editing && (
                  <div className="flex gap-2">
                    <Select
                      value=""
                      onValueChange={(v: string) => {
                        if (v) setEditMembers((prev) => [...prev, v]);
                      }}
                    >
                      <SelectOption value="">+ Add member…</SelectOption>
                      {availableForEdit.map((p) => (
                        <SelectOption key={p.name} value={p.name}>
                          {p.name}
                        </SelectOption>
                      ))}
                    </Select>
                  </div>
                )}
                {editing && editMembers.length > 0 && (
                  <div className="mt-4">
                    <Label>Default member</Label>
                    <Select value={editDefaultMember} onValueChange={setEditDefaultMember}>
                      {editMembers.map((m) => (
                        <SelectOption key={m} value={m}>{m}</SelectOption>
                      ))}
                    </Select>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Bind IM source */}
            <Card className="room-surface-raised border-border bg-card">
              <CardHeader>
                <CardTitle className="text-lg flex items-center gap-2">
                  <Link2 className="h-5 w-5" />
                  Bind to IM channel
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground mb-3">
                  Enter a <code className="text-xs bg-muted px-1 rounded break-all">source_binding_key</code> (from an inbound message on DingTalk/Slack) to route that chat to this room.
                </p>
                <div className="flex gap-2 min-w-0">
                  <Input
                    value={bindKey}
                    onChange={(e) => setBindKey(e.target.value)}
                    placeholder="source:dingtalk:group:cid...:user_id"
                    className="font-mono text-xs flex-1 min-w-0"
                  />
                  <Button onClick={handleBind} disabled={binding || !bindKey.trim()} className="shrink-0">
                    {binding ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Link2 className="mr-1 h-4 w-4" />}
                    Bind
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        )}
      </main>

      {/* ═══ Right: chat panel — dashboard-side messaging ═══ */}
      {selectedRoom && (
        <aside className="flex-1 min-w-[400px] flex flex-col order-2">
          <div className="p-3 border-b border-border">
            <div className="flex items-center gap-2 text-sm font-medium">
              <MessageSquare className="h-4 w-4 shrink-0" />
              <span className="min-w-0 break-words">Chat with {selectedRoom.room_name}</span>
            </div>
            <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
              <label className="flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={chatBroadcast}
                  onChange={(e) => setChatBroadcast(e.target.checked)}
                  className="h-3 w-3"
                />
                Broadcast to all {selectedRoom.members.length} members
              </label>
            </div>
          </div>

          {/* Message list */}
          <div className="flex-1 overflow-y-auto overflow-x-hidden p-3 space-y-3">
            {chatHistory.length === 0 ? (
              <div className="text-center text-sm text-muted-foreground py-8">
                <MessageSquare className="mx-auto h-8 w-8 opacity-30 mb-2" />
                <p>Send a test message below.</p>
                <p className="text-xs mt-1">
                  Bind this room to an IM channel for live routing.
                </p>
              </div>
            ) : (
              chatHistory.map((m) => {
                if (m.kind === "observer") {
                  return m.route ? (
                    <div key={m.id} className="pl-10 pr-1">
                      <RoutingTrace route={m.route} />
                    </div>
                  ) : null;
                }
                const isUser = m.kind === "user";
                const avatarBg = isUser ? "var(--color-primary)" : avatarColorFor(m.sender);
                return (
                  <div
                    key={m.id}
                    className={cn(
                      "group flex gap-2",
                      isUser ? "flex-row-reverse" : "flex-row",
                    )}
                  >
                    {/* Avatar with soft ring + online status dot */}
                    <div className="relative shrink-0">
                      <div
                        className={cn(
                          "room-avatar flex items-center justify-center w-8 h-8 rounded-full text-xs font-semibold",
                          isUser ? "text-primary-foreground" : "text-white",
                        )}
                        style={{ backgroundColor: avatarBg }}
                        title={m.sender}
                      >
                        {avatarInitials(m.sender)}
                      </div>
                      {!isUser && (
                        <span
                          className="room-status-dot absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-success"
                          aria-hidden
                        />
                      )}
                    </div>

                    {/* Bubble + meta */}
                    <div
                      className={cn(
                        "flex flex-col min-w-0 max-w-[85%]",
                        isUser ? "items-end" : "items-start",
                      )}
                    >
                      <div
                        className={cn(
                          "px-3 py-2 break-words text-sm",
                          isUser
                            ? "room-chip bg-primary text-primary-foreground shadow-[0_1px_2px_rgba(16,24,40,0.12)]"
                            : "room-surface bg-card text-card-foreground",
                        )}
                      >
                        {renderChatContent(m.content)}
                      </div>
                      {!isUser && m.toolCalls && m.toolCalls.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {m.toolCalls.map((tc, i) => (
                            <ToolChip key={tc.id ?? i} tc={tc} />
                          ))}
                        </div>
                      )}
                      <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground px-1">
                        <span className="font-mono">{m.sender}</span>
                        {m.timestamp > 0 && (
                          <span>{formatChatTime(m.timestamp)}</span>
                        )}
                        {m.seq != null && (
                          <button
                            type="button"
                            className="opacity-0 group-hover:opacity-100 transition-opacity hover:text-destructive"
                            onClick={() => handleDeleteMessage(m)}
                            aria-label="Delete message"
                            title="Delete message"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })
            )}
            {chatSending && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground pl-10">
                <span className="flex items-center gap-1">
                  <span className="room-typing-dot" />
                  <span className="room-typing-dot" />
                  <span className="room-typing-dot" />
                </span>
                <span>Members are thinking…</span>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-border">
            <div className="flex gap-2">
              <textarea
                rows={2}
                className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm resize-none"
                placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void handleSendChat();
                  }
                }}
                disabled={chatSending}
              />
              <Button
                onClick={handleSendChat}
                disabled={chatSending || !chatInput.trim()}
                size="sm"
                className="self-end"
              >
                {chatSending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  "Send"
                )}
              </Button>
            </div>
          </div>
        </aside>
      )}

      {/* ═══ Create Room dialog ═══ */}
      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create a new room</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="create-room-name">Name</Label>
              <Input
                id="create-room-name"
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
                placeholder="support_team"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Lowercase ASCII, digits, _, - only. Max 64 chars.
              </p>
            </div>
            <div>
              <Label htmlFor="create-room-desc">Description</Label>
              <textarea
                id="create-room-desc"
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                rows={2}
                value={createDescription}
                onChange={(e) => setCreateDescription(e.target.value)}
                placeholder="What this room is for"
              />
            </div>
            <div>
              <Label>Members</Label>
              <div className="flex flex-wrap gap-2 mb-2">
                {createMembers.map((m) => (
                  <div key={m} className="room-chip flex items-center gap-1 bg-accent px-2 py-1 text-sm">
                    <span className="font-mono">{m}</span>
                    <button
                      type="button"
                      onClick={() =>
                        setCreateMembers((prev) => prev.filter((x) => x !== m))
                      }
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
              <Select
                value=""
                onValueChange={(v: string) => {
                  if (v) setCreateMembers((prev) => [...prev, v]);
                }}
              >
                <SelectOption value="">+ Add member…</SelectOption>
                {availableForCreate.map((p) => (
                  <SelectOption key={p.name} value={p.name}>
                    {p.name} {p.description ? `— ${p.description.slice(0, 40)}` : ""}
                  </SelectOption>
                ))}
              </Select>
            </div>
            {createMembers.length > 0 && (
              <div>
                <Label>Default member</Label>
                <Select
                  value={createDefaultMember || createMembers[0]}
                  onValueChange={setCreateDefaultMember}
                >
                  {createMembers.map((m) => (
                    <SelectOption key={m} value={m}>{m}</SelectOption>
                  ))}
                </Select>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button outlined onClick={() => setShowCreate(false)}>Cancel</Button>
            <Button onClick={handleCreate} disabled={creating}>
              {creating ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ═══ Planner dialog ═══ */}
      <Dialog open={showPlanner} onOpenChange={setShowPlanner}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <span className="inline-flex items-center gap-2">
                <Sparkles className="h-5 w-5" />
                Plan a room with AI
              </span>
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="plan-requirement">What do you need this room to do?</Label>
              <textarea
                id="plan-requirement"
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                rows={4}
                value={planRequirement}
                onChange={(e) => setPlanRequirement(e.target.value)}
                placeholder="e.g. 我需要一个客服+财务+技术的团队来处理客户咨询"
                disabled={planning || Boolean(plannedResult)}
              />
            </div>
            <div>
              <Label htmlFor="plan-max-members">Max members</Label>
              <Input
                id="plan-max-members"
                type="number"
                min={2}
                max={5}
                value={planMaxMembers}
                onChange={(e) => setPlanMaxMembers(parseInt(e.target.value) || 5)}
                disabled={planning || Boolean(plannedResult)}
              />
            </div>

            {plannedResult && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Suggested plan</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <div>
                    <Label className="text-xs">Rationale</Label>
                    <p>{plannedResult.rationale}</p>
                  </div>
                  <div>
                    <Label className="text-xs">Room description</Label>
                    <p>{plannedResult.room_description}</p>
                  </div>
                  <div>
                    <Label className="text-xs">Members</Label>
                    <ul className="space-y-2 mt-1">
                      {plannedResult.members.map((m, idx) => (
                        <li key={idx} className="room-chip flex items-start gap-2 border border-border p-2">
                          <Badge tone={m.is_new ? "success" : "secondary"}>
                            {m.is_new ? "🆕 new" : "✅ existing"}
                          </Badge>
                          <div className="flex-1 min-w-0">
                            <div className="font-mono text-xs">{m.name}</div>
                            <div className="text-xs text-muted-foreground">{m.description}</div>
                            {m.reason && (
                              <div className="mt-1 text-xs italic text-muted-foreground">
                                Reason: {m.reason}
                              </div>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <Label className="text-xs">Room name (optional override)</Label>
                    <Input
                      value={plannedRoomName}
                      onChange={(e) => setPlannedRoomName(e.target.value)}
                      placeholder="Auto-derived from description"
                    />
                  </div>
                </CardContent>
              </Card>
            )}
          </div>
          <DialogFooter>
            {!plannedResult ? (
              <>
                <Button outlined onClick={() => setShowPlanner(false)}>Cancel</Button>
                <Button onClick={handlePlan} disabled={planning || !planRequirement.trim()}>
                  {planning ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Sparkles className="mr-1 h-4 w-4" />}
                  Generate plan
                </Button>
              </>
            ) : (
              <>
                <Button outlined onClick={() => setPlannedResult(null)} disabled={confirmingPlan}>
                  Try again
                </Button>
                <Button onClick={handleConfirmPlan} disabled={confirmingPlan}>
                  {confirmingPlan ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Check className="mr-1 h-4 w-4" />}
                  Confirm &amp; create
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Confirm delete + toast */}
      <DeleteConfirmDialog
        open={roomDelete.isOpen}
        loading={roomDelete.isDeleting}
        onCancel={roomDelete.cancel}
        onConfirm={roomDelete.confirm}
        title={
          pendingDeleteRoom
            ? `Delete room "${pendingDeleteRoom.room_name}"?`
            : "Delete room?"
        }
        description="This will tear down the observer profile and clear all bindings. Cannot be undone."
        confirmLabel="Delete"
      />
      <Toast toast={toast} />
    </div>
  );
}
