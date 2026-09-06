import type { RoomSummary } from "@/lib/api";
import type { RoomPreset } from "./RoomsWorkspace";

export function roomMatchesPreset(room: RoomSummary, preset: RoomPreset): boolean {
  if (preset === "all") return true;
  return room.workspace?.state === preset;
}

export function filterRoomInbox(
  rooms: readonly RoomSummary[],
  query: string,
  preset: RoomPreset,
): RoomSummary[] {
  const normalized = query.trim().toLowerCase();
  return rooms.filter(
    (room) =>
      roomMatchesPreset(room, preset)
      && (!normalized
        || [
          room.name,
          room.room_id,
          room.workspace?.current_task?.subject,
          ...room.members.flatMap((member) => [member.handle, member.display_name]),
        ].some((value) => value?.toLowerCase().includes(normalized))),
  );
}

export function taskProgress(room: RoomSummary): number {
  const counts = room.workspace?.task_counts;
  return counts?.total
    ? Math.max(0, Math.min(100, (counts.completed / counts.total) * 100))
    : 0;
}
