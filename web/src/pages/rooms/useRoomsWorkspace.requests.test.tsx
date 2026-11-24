// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { roomsApi, type RoomSummary, type RoomWorkspaceResponse } from "@/lib/api-rooms";
import { useRoomsWorkspace } from "./useRoomsWorkspace";

let root: Root | undefined;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

function Probe({ onState }: { onState: (state: ReturnType<typeof useRoomsWorkspace>) => void }) {
  // A fresh onError/requireSuccess on every render is exactly how RoomsPage calls the hook; depending on
  // them re-ran the load effect on every render (an unbounded /api/rooms loop).
  const state = useRoomsWorkspace({ onError: () => undefined, requireSuccess: () => undefined });
  onState(state);
  return null;
}

function roomSummary(state: "running" | "idle"): RoomSummary {
  return {
    room_id: "room-1", name: "Room", members: [], revision: 1, latest_seq: 1, authority_epoch: 1,
    workspace: {
      state, health: "healthy", task_counts: { total: 1, completed: state === "idle" ? 1 : 0 },
      pending_action_count: 0, active_member_count: state === "running" ? 1 : 0,
      current_task: null, last_activity_at: 1, driver_running: state === "running",
    },
  };
}

function workspace(status: "in_progress" | "completed"): RoomWorkspaceResponse {
  const state = status === "completed" ? "idle" : "running";
  return {
    room: roomSummary(state),
    tasks: [{
      task_id: "task-1", subject: "Task", status, visual_state: status === "completed" ? "completed" : "working",
      pending_actions: [],
    }],
    attempts: [], pending_actions: [], conversation: [], activity: [],
    log: { events: [], cursor: 0, latest_seq: 0, has_more: false, authority: {} },
  };
}

function mockAncillaryRequests() {
  vi.spyOn(roomsApi, "getRoomTopology").mockResolvedValue({} as never);
  vi.spyOn(roomsApi, "getRoomPendingActions").mockResolvedValue({ actions: [] } as never);
  vi.spyOn(roomsApi, "getRoomPeerGrants").mockResolvedValue({ peer_grants: [] } as never);
  vi.spyOn(roomsApi, "getRoomReplicationHealth").mockResolvedValue({} as never);
  vi.spyOn(roomsApi, "getRoomPolicyTrace").mockResolvedValue({} as never);
}

async function renderProbe(onState: (state: ReturnType<typeof useRoomsWorkspace>) => void) {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(<Probe onState={onState} />));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
}

describe("useRoomsWorkspace request stability", () => {
  it("loads the room list once even when callers pass inline callbacks every render", async () => {
    const listRooms = vi.spyOn(roomsApi, "listRooms").mockResolvedValue({ rooms: [] } as never);
    let loading = true;
    await renderProbe((state) => { loading = state.loadingList; });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(loading).toBe(false);
    expect(listRooms).toHaveBeenCalledTimes(1);
  });

  it("refreshes active work until it settles, then stops", async () => {
    vi.spyOn(roomsApi, "listRooms").mockResolvedValue({ rooms: [roomSummary("running")] });
    const getWorkspace = vi.spyOn(roomsApi, "getRoomWorkspace")
      .mockResolvedValueOnce(workspace("in_progress"))
      .mockResolvedValue(workspace("completed"));
    mockAncillaryRequests();
    let current: ReturnType<typeof useRoomsWorkspace> | undefined;
    await renderProbe((state) => { current = state; });
    await act(async () => {
      await vi.waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1));
    });
    expect(current?.workspace?.tasks[0]?.status).toBe("in_progress");

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    expect(current?.workspace?.tasks[0]?.status).toBe("completed");
    expect(current?.workspace?.room.workspace?.task_counts.completed).toBe(1);
    expect(getWorkspace).toHaveBeenCalledTimes(2);

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    expect(getWorkspace).toHaveBeenCalledTimes(2);
  });

  it("does not poll a workspace whose work is already terminal", async () => {
    vi.spyOn(roomsApi, "listRooms").mockResolvedValue({ rooms: [roomSummary("idle")] });
    const getWorkspace = vi.spyOn(roomsApi, "getRoomWorkspace").mockResolvedValue(workspace("completed"));
    mockAncillaryRequests();
    await renderProbe(() => undefined);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1100)); });
    expect(getWorkspace).toHaveBeenCalledTimes(1);
  });
});
