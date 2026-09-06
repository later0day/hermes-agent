// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { RoomSummary, RoomWorkspaceResponse } from "@/lib/api";
import { RoomsWorkspace, type RoomsWorkspaceProps } from "./RoomsWorkspace";

const noop = () => undefined;

function summary(roomId: string, completed: number, total: number): RoomSummary {
  return {
    room_id: roomId,
    name: roomId,
    members: [],
    revision: 1,
    latest_seq: 1,
    authority_epoch: 1,
    workspace: {
      state: "idle",
      health: "healthy",
      task_counts: { completed, total },
      pending_action_count: 0,
      active_member_count: 0,
      current_task: null,
      last_activity_at: 1,
      driver_running: false,
    },
  };
}

function props(rooms: RoomSummary[]): RoomsWorkspaceProps {
  const selected = rooms[0];
  const workspace = {
    room: { ...selected, workspace: undefined },
    tasks: [],
    attempts: [],
    pending_actions: [],
    conversation: [],
    activity: [],
    log: { events: [], redacted: true },
  } as unknown as RoomWorkspaceResponse;
  return {
    rooms,
    selectedRoomId: selected.room_id,
    workspace,
    search: "",
    preset: "all",
    tab: "tasks",
    taskMode: "list",
    inspector: { kind: "room" },
    onSearchChange: noop,
    onPresetChange: noop,
    onSelectRoom: noop,
    onTabChange: noop,
    onTaskModeChange: noop,
    onInspectorChange: noop,
    onOpenActionCenter: noop,
    onCloseActionCenter: noop,
    onApproveAction: noop,
    onDenyAction: noop,
  };
}

let root: Root | undefined;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  document.body.innerHTML = "";
  root = undefined;
});

describe("RoomsWorkspace summary header", () => {
  it("uses only the selected inbox summary when workspace.room is raw", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <RoomsWorkspace {...props([summary("selected", 8, 8), summary("other", 1, 9)])} />,
    ));

    const header = container.querySelector("main header");
    expect(header?.textContent).toContain("8 / 8");
    expect(header?.textContent).not.toContain("1 / 9");
  });
});
