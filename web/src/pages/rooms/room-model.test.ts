import { describe, expect, it } from "vitest";
import type { ReplicationHealthResponse, RoomEvent, RoomSummary } from "@/lib/api";
import { aggregateRoomHealth, groupRoomConversations, projectRoomActivity, sortRoomsByPriority, visualTaskState } from "./room-model";

function room(overrides: Partial<RoomSummary>): RoomSummary {
  return { room_id: "room", name: "Room", members: [], revision: 1, latest_seq: 0, authority_epoch: 1, ...overrides };
}

function event(seq: number, actor: string, createdAt: number, kind = "message.agent", payload: Record<string, unknown> = { text: `message ${seq}` }): RoomEvent {
  return { room_id: "r1", seq, event_id: `e${seq}`, kind, actor: { kind: "member", id: actor }, authority_epoch: 1, payload, created_at: createdAt };
}

describe("sortRoomsByPriority", () => {
  it("puts active rooms first and sorts each lifecycle by recency", () => {
    const input = [room({ room_id: "closed", updated_at: 30, disbanded_at: 31 }), room({ room_id: "old", updated_at: 10 }), room({ room_id: "new", updated_at: 20 })];
    expect(sortRoomsByPriority(input).map((item) => item.room_id)).toEqual(["new", "old", "closed"]);
    expect(input.map((item) => item.room_id)).toEqual(["closed", "old", "new"]);
  });

  it("puts action and failure states ahead of more recent running rooms", () => {
    const input = [
      room({ room_id: "running", updated_at: 30, workspace: { state: "running" } as RoomSummary["workspace"] }),
      room({ room_id: "failed", updated_at: 20, workspace: { state: "failed" } as RoomSummary["workspace"] }),
      room({ room_id: "action", updated_at: 10, workspace: { state: "needs_action" } as RoomSummary["workspace"] }),
    ];
    expect(sortRoomsByPriority(input).map((item) => item.room_id)).toEqual(["action", "failed", "running"]);
  });

  it("uses sequence and id as deterministic ties", () => {
    const input = [room({ room_id: "b", updated_at: 10, latest_seq: 1 }), room({ room_id: "c", updated_at: 10, latest_seq: 2 }), room({ room_id: "a", updated_at: 10, latest_seq: 1 })];
    expect(sortRoomsByPriority(input).map((item) => item.room_id)).toEqual(["c", "a", "b"]);
  });
});

describe("visualTaskState", () => {
  it.each([
    [{ blocked: true, status: "running" }, "blocked"],
    [{ status: "failed" }, "blocked"],
    [{ status: "completed" }, "completed"],
    [{ status: "in_progress" }, "working"],
    [{ status: "pending" }, "queued"],
    [{ current_task_id: "task-1" }, "working"],
    [{ status: "mystery" }, "idle"],
    [null, "idle"],
  ] as const)("maps %o to %s", (input, expected) => expect(visualTaskState(input)).toBe(expected));
});

describe("projectRoomActivity", () => {
  it("prefers human payload text and identifies the actor", () => {
    expect(projectRoomActivity(event(1, "alice", 10, "teammate.result_report", { text: "Built the UI" }))).toMatchObject({ actor: "alice", summary: "Built the UI", timestamp: 10 });
  });

  it("projects known protocol activity and humanizes unknown kinds", () => {
    expect(projectRoomActivity(event(1, "lead", 10, "coordinator.task_assign", { target_handle: "bob" })).summary).toBe("Assigned task to @bob");
    expect(projectRoomActivity(event(2, "system", 11, "custom.some_event", {})).summary).toBe("custom · some event");
  });
});

describe("groupRoomConversations", () => {
  it("sorts by sequence and groups adjacent same-actor events inside the gap", () => {
    const groups = groupRoomConversations([event(3, "alice", 500), event(1, "alice", 100), event(2, "alice", 120), event(4, "bob", 510)], 60);
    expect(groups.map((group) => [group.actor, group.events.map((item) => item.summary)])).toEqual([
      ["alice", ["message 1", "message 2"]], ["alice", ["message 3"]], ["bob", ["message 4"]],
    ]);
  });

  it("does not merge non-adjacent messages from the same actor", () => {
    expect(groupRoomConversations([event(1, "alice", 1), event(2, "bob", 2), event(3, "alice", 3)]).map((group) => group.actor)).toEqual(["alice", "bob", "alice"]);
  });
});

describe("aggregateRoomHealth", () => {
  const replication = (overrides: Partial<ReplicationHealthResponse> = {}): ReplicationHealthResponse => ({ room_id: "r1", healthy: true, total_peers: 2, ready: 2, unavailable: 0, needs_reauthorization: 0, peers: [], ...overrides });

  it("reports healthy replication with no workspace issues", () => {
    expect(aggregateRoomHealth(replication())).toEqual({ level: "healthy", healthy: true, issues: [], readyPeers: 2, totalPeers: 2 });
  });

  it("distinguishes degraded pending work from unhealthy execution conditions", () => {
    expect(aggregateRoomHealth(replication(), { pendingActions: 2 }).level).toBe("degraded");
    const result = aggregateRoomHealth(replication({ healthy: false, ready: 0, unavailable: 1 }), { blockedTasks: 1 });
    expect(result.level).toBe("unhealthy");
    expect(result.issues).toEqual(expect.arrayContaining(["Replication is unhealthy", "1 peer(s) unavailable", "1 blocked task(s)"]));
  });

  it("returns unknown when no health signal exists", () => expect(aggregateRoomHealth(null)).toMatchObject({ level: "unknown", healthy: null }));
});
