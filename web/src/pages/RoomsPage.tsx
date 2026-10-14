import { useEffect, useMemo, useState } from "react";
import { api, type PendingAction, type ProfileInfo, type RoomCreateRequest } from "@/lib/api";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";
import RoomsWorkspace, { type RoomInspector } from "./rooms/RoomsWorkspace";
import { requireRoomActionSuccess, roomErrorMessage } from "./rooms/room-model";
import { useRoomsWorkspace } from "./rooms/useRoomsWorkspace";

export default function RoomsPage() {
  const { toast, showToast } = useToast();
  const { t } = useI18n();
  const s = useMemo(() => localeSection(t, "roomsWorkspace"), [t]);
  const [inspector, setInspector] = useState<RoomInspector>({ kind: "room" });
  const [actionCenterAction, setActionCenterAction] = useState<PendingAction | null>(null);
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [teamBusy, setTeamBusy] = useState(false);
  useEffect(() => { void api.getProfiles().then((result) => setProfiles(result.profiles)).catch((error) => showToast(roomErrorMessage(error, s.serviceUnavailable), "error")); }, [showToast, s]);
  const state = useRoomsWorkspace({
    requireSuccess: (response) => requireRoomActionSuccess(response, s.actionFailed),
    onError: (error) => showToast(roomErrorMessage(error, s.serviceUnavailable), "error"),
    describeError: (error) => roomErrorMessage(error, s.serviceUnavailable),
  });

  const selectRoom = (roomId: string) => {
    state.setSelectedRoomId(roomId || null);
    state.setSelectedTaskId(null);
    setInspector({ kind: "room" });
  };

  const selectInspector = (next: RoomInspector) => {
    setInspector(next);
    state.setSelectedTaskId(next.kind === "task" ? next.taskId : null);
  };

  const approve = async (action: PendingAction) => {
    if (await state.approveAction(action.action_id)) {
      setActionCenterAction(null);
      showToast(action.kind === "retry" ? s.retryQueued : s.actionApproved, "success");
      await state.refreshRoom();
    }
  };

  const deny = async (action: PendingAction) => {
    if (await state.denyAction(action.action_id)) {
      setActionCenterAction(null);
      showToast(s.actionDenied, "success");
      await state.refreshRoom();
    }
  };

  const createTeam = async (request: RoomCreateRequest) => {
    setTeamBusy(true);
    try {
      await api.createRoom(request);
      await state.refreshList();
      selectRoom(request.room_id);
      showToast(s.teamCreated, "success");
      return true;
    } catch (error) { showToast(roomErrorMessage(error, s.serviceUnavailable), "error"); return false; }
    finally { setTeamBusy(false); }
  };

  const delegateWork = async (recipient: string, text: string) => {
    if (!state.selectedRoomId) return false;
    setTeamBusy(true);
    try {
      await api.sendRoomMessage(state.selectedRoomId, { recipient, text, event_id: `web-${crypto.randomUUID()}`, thread_id: `web-${crypto.randomUUID()}` });
      await state.refreshRoom();
      showToast(s.workSent.replace("{recipient}", recipient), "success");
      return true;
    } catch (error) { showToast(roomErrorMessage(error, s.serviceUnavailable), "error"); return false; }
    finally { setTeamBusy(false); }
  };

  return (
    <>
      <RoomsWorkspace
        rooms={state.rooms ?? []}
        selectedRoomId={state.selectedRoomId}
        workspace={state.workspace}
        topology={state.ancillary.topology.data}
        peerGrants={state.ancillary.peerGrants.data}
        replicationHealth={state.ancillary.replication.data}
        policyTrace={state.ancillary.policy.data}
        search={state.search}
        preset={state.preset}
        tab={state.tab}
        taskMode={state.mode}
        inspector={inspector}
        actionCenterAction={actionCenterAction}
        profiles={profiles}
        teamBusy={teamBusy}
        onCreateTeam={createTeam}
        onDelegateWork={delegateWork}
        onSearchChange={state.setSearch}
        onPresetChange={state.setPreset}
        onSelectRoom={selectRoom}
        onTabChange={state.setTab}
        onTaskModeChange={state.setMode}
        onInspectorChange={selectInspector}
        onOpenActionCenter={setActionCenterAction}
        onCloseActionCenter={() => setActionCenterAction(null)}
        onApproveAction={(action) => void approve(action)}
        onDenyAction={(action) => void deny(action)}
        actionBusy={state.busyActionId !== null}
      />
      {state.loadingList || state.loadingWorkspace ? (
        <span className="sr-only" role="status">{s.loadingWorkspace}</span>
      ) : null}
      {state.listError || state.workspaceError ? (
        <div role="alert" className="mx-4 mt-2 border border-destructive/40 p-3 text-sm text-destructive">
          {state.listError || state.workspaceError}
          <button className="ml-3 underline" type="button" onClick={() => void state.refresh()}>{s.retry}</button>
        </div>
      ) : null}
      <Toast toast={toast} />
    </>
  );
}
