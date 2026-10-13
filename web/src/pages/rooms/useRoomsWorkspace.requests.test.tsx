// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useRoomsWorkspace } from "./useRoomsWorkspace";

afterEach(() => vi.restoreAllMocks());

describe("useRoomsWorkspace request stability", () => {
  it("loads the room list once even when callers pass inline callbacks every render", async () => {
    const listRooms = vi.spyOn(api, "listRooms").mockResolvedValue({ rooms: [] } as never);
    // A fresh onError/requireSuccess on every render is exactly how RoomsPage calls the hook; depending on
    // them re-ran the load effect on every render (an unbounded /api/rooms loop).
    const { result } = renderHook(() => useRoomsWorkspace({ onError: () => undefined, requireSuccess: () => undefined }));
    await waitFor(() => expect(result.current.loadingList).toBe(false));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(listRooms).toHaveBeenCalledTimes(1);
  });
});
