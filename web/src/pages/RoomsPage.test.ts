import { describe, expect, it } from "vitest";
import type { RoomEvent, RoomRoleKind } from "@/lib/api";
import { filterEvents, requireRoomActionSuccess } from "./RoomsPage";

function event(kind: string, actor: RoomEvent["actor"]): RoomEvent {
  return {
    room_id: "room-1",
    seq: 1,
    event_id: kind,
    kind,
    actor,
    authority_epoch: 1,
    payload: {},
    created_at: 1,
  };
}

const events = [
  event("message.member", { kind: "member", id: "lead" }),
  event("message.member", { kind: "member", id: "worker" }),
  event("message.user", { kind: "user", id: "user-1" }),
  event("turn.settled", { kind: "gateway", id: "gateway-1" }),
];
const roles = new Map<string, RoomRoleKind>([
  ["lead", "team_lead"],
  ["worker", "teammate"],
]);

describe("Rooms event actor filters", () => {
  it("classifies member messages from topology roles", () => {
    expect(filterEvents(events, "coordinator", "all", roles).map((x) => x.actor.id))
      .toEqual(["lead"]);
    expect(filterEvents(events, "teammate", "all", roles).map((x) => x.actor.id))
      .toEqual(["worker"]);
  });

  it("keeps user and gateway/system filters grounded in actor identity", () => {
    expect(filterEvents(events, "user", "all", roles).map((x) => x.kind))
      .toEqual(["message.user"]);
    expect(filterEvents(events, "system", "all", roles).map((x) => x.kind))
      .toEqual(["turn.settled"]);
  });
});

describe("Rooms pending action responses", () => {
  it("accepts successful action responses", () => {
    expect(() => requireRoomActionSuccess({ ok: true })).not.toThrow();
  });

  it("surfaces a backend rejection instead of showing false success", () => {
    expect(() => requireRoomActionSuccess({ ok: false, message: "session not found" }))
      .toThrow("session not found");
  });
});
