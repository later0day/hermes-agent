import { useI18n } from "@/i18n/context";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Bot,
  Check,
  Edit3,
  Info,
  Link2,
  Loader2,
  MessageSquare,
  Plus,
  Sparkles,
  Trash2,
  Users,
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
import type { ProfileInfo, RoomPlan, RoomRecord } from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";

// Regex matching the backend's profile-name validation.
const ROOM_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export default function RoomsPage() {
  const { t: _t } = useI18n();
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
    } catch (err) {
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
      <div className="flex items-center gap-2">
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
    } catch (err) {
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
    } catch (err) {
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
    } catch (err) {
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
    } catch (err) {
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
    } catch (err) {
      showToast("Bind failed", "error");
    } finally {
      setBinding(false);
    }
  }, [selectedRoom, bindKey, showToast]);

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
    <div className="flex min-h-0 w-full min-w-0 flex-1 h-full">
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
                  "group w-full rounded-md px-3 py-2 text-left transition-colors",
                  "hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring",
                  selectedRoomId === room.room_id && "bg-accent",
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
      <main className="flex-1 overflow-y-auto p-6">
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
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between gap-3">
                  <CardTitle className="text-2xl">
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
            <Card>
              <CardHeader>
                <CardTitle className="text-lg flex items-center gap-2">
                  <Users className="h-5 w-5" />
                  Members ({editing ? editMembers.length : selectedRoom.members.length})
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex flex-wrap gap-2 mb-3">
                  {(editing ? editMembers : selectedRoom.members).map((m) => {
                    const meta = profiles.find((p) => p.name === m);
                    return (
                      <div
                        key={m}
                        className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-1.5"
                      >
                        <span className="font-mono text-sm">{m}</span>
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
            <Card>
              <CardHeader>
                <CardTitle className="text-lg flex items-center gap-2">
                  <Link2 className="h-5 w-5" />
                  Bind to IM channel
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground mb-3">
                  Enter a <code className="text-xs bg-muted px-1 rounded">source_binding_key</code> (from an inbound message on DingTalk/Slack) to route that chat to this room.
                </p>
                <div className="flex gap-2">
                  <Input
                    value={bindKey}
                    onChange={(e) => setBindKey(e.target.value)}
                    placeholder="source:dingtalk:group:cid...:user_id"
                    className="font-mono text-xs flex-1"
                  />
                  <Button onClick={handleBind} disabled={binding || !bindKey.trim()}>
                    {binding ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Link2 className="mr-1 h-4 w-4" />}
                    Bind
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        )}
      </main>

      {/* ═══ Right: chat preview ═══ */}
      {selectedRoom && (
        <aside className="w-96 border-l border-border overflow-y-auto p-4 shrink-0">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <MessageSquare className="h-4 w-4" />
                Chat with this room
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="flex items-start gap-2 rounded-md bg-blue-50 dark:bg-blue-950/30 p-3 border border-blue-200 dark:border-blue-900">
                <Info className="h-4 w-4 mt-0.5 text-blue-600 dark:text-blue-400 shrink-0" />
                <div className="text-xs">
                  <p className="font-medium mb-1">Live chat coming in M4</p>
                  <p className="text-muted-foreground">
                    Rooms currently receive messages through bound IM channels (DingTalk, Slack, etc.). Bind this room to a channel and test from the messenger.
                  </p>
                </div>
              </div>
              <div>
                <Label className="text-xs">Rooms API</Label>
                <div className="mt-1 space-y-1 font-mono text-xs text-muted-foreground">
                  <div>GET /api/rooms/{selectedRoom.room_id}</div>
                  <div>POST /api/rooms/{selectedRoom.room_id}/bind</div>
                  <div>POST /api/rooms/plan</div>
                </div>
              </div>
              <div>
                <Label className="text-xs">Slash commands</Label>
                <div className="mt-1 space-y-1 font-mono text-xs text-muted-foreground">
                  <div>/room bind {selectedRoom.room_name}</div>
                  <div>/room plan &lt;requirement&gt;</div>
                </div>
              </div>
            </CardContent>
          </Card>
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
                  <div key={m} className="flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-sm">
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
                        <li key={idx} className="flex items-start gap-2 rounded-md border p-2">
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
