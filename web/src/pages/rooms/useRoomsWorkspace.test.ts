import { describe, expect, it } from "vitest";
import {
  createLatestRequestGuard,
  mergeRoomsWorkspaceUrl,
  parseRoomsWorkspaceUrl,
  selectAvailableRoom,
  selectAvailableTask,
} from "./useRoomsWorkspace";

describe("rooms workspace URL helpers", () => {
  it("parses supported state and safely defaults invalid values", () => {
    expect(parseRoomsWorkspaceUrl("?room=r%201&tab=activity&task=t1&view=list&filter=failed&search=needle")).toEqual({
      roomId: "r 1", tab: "activity", taskId: "t1", mode: "list", preset: "failed", search: "needle",
    });
    expect(parseRoomsWorkspaceUrl("?tab=bogus&view=bogus&filter=bogus")).toEqual({
      roomId: null, tab: "tasks", taskId: null, mode: "list", preset: "all", search: "",
    });
  });

  it("updates owned keys without corrupting unrelated query params", () => {
    const merged = mergeRoomsWorkspaceUrl("?profile=alice&room=old&plugin=x%2Fy&tab=health", {
      roomId: "new room", tab: "conversation", taskId: "task/1", mode: "graph", preset: "needs_action", search: "a b",
    });
    const params = new URLSearchParams(merged);
    expect(Object.fromEntries(params)).toEqual({
      profile: "alice", plugin: "x/y", room: "new room", tab: "conversation", task: "task/1",
      view: "graph", filter: "needs_action", search: "a b",
    });
  });

  it("removes default and empty owned values only", () => {
    expect(mergeRoomsWorkspaceUrl("?keep=1&room=old&task=old&tab=health", {
      roomId: null, tab: "tasks", taskId: null, mode: "list", preset: "all", search: "",
    })).toBe("?keep=1");
  });
});

describe("rooms workspace selection helpers", () => {
  const rooms = [{ room_id: "first" }, { room_id: "second" }];
  it("retains a valid room and otherwise chooses the first available room", () => {
    expect(selectAvailableRoom("second", rooms)).toBe("second");
    expect(selectAvailableRoom("missing", rooms)).toBe("first");
    expect(selectAvailableRoom(null, [])).toBeNull();
  });

  it("retains only a task belonging to the current workspace", () => {
    const workspace = { tasks: [{ task_id: "t1" }] } as Parameters<typeof selectAvailableTask>[1];
    expect(selectAvailableTask("t1", workspace)).toBe("t1");
    expect(selectAvailableTask("old", workspace)).toBeNull();
    expect(selectAvailableTask("t1", null)).toBeNull();
  });
});

describe("latest request guard", () => {
  it("invalidates every prior request token monotonically", () => {
    const guard = createLatestRequestGuard();
    const first = guard.next();
    expect(guard.current(first)).toBe(true);
    const second = guard.next();
    expect(second).toBeGreaterThan(first);
    expect(guard.current(first)).toBe(false);
    expect(guard.current(second)).toBe(true);
    guard.next();
    expect(guard.current(second)).toBe(false);
  });
});
