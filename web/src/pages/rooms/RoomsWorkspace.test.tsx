import { describe, expect, it } from "vitest";
import type { RoomSummary } from "@/lib/api";
import { filterRoomInbox, roomMatchesPreset, taskProgress } from "./workspace-helpers";

function room(id: string, state: NonNullable<RoomSummary["workspace"]>["state"], name=id): RoomSummary {
  return { room_id:id, name, members:[], revision:1, latest_seq:1, authority_epoch:1, workspace:{ state, health:"healthy", task_counts:{total:4,completed:3}, pending_action_count:state==="needs_action"?1:0, active_member_count:state==="running"?1:0, current_task:null, last_activity_at:1, driver_running:state==="running" } };
}
describe("RoomsWorkspace helpers",()=>{
  it("uses the controlled room preset vocabulary",()=>{ const failed=room("f","failed"); expect(roomMatchesPreset(failed,"failed")).toBe(true); expect(roomMatchesPreset(failed,"running")).toBe(false); expect(roomMatchesPreset(failed,"all")).toBe(true); });
  it("filters inbox by preset and case-insensitive room identity",()=>{ const rooms=[room("one","running","Release Train"),room("two","idle","Research")]; expect(filterRoomInbox(rooms,"release","running").map(x=>x.room_id)).toEqual(["one"]); expect(filterRoomInbox(rooms,"TWO","all").map(x=>x.room_id)).toEqual(["two"]); });
  it("derives bounded progress",()=>{ expect(taskProgress(room("one","running"))).toBe(75); expect(taskProgress({...room("zero","idle"),workspace:{...room("zero","idle").workspace!,task_counts:{total:0,completed:2}}})).toBe(0); });
});
