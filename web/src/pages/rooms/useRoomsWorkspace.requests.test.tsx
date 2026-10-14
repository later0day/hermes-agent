// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useRoomsWorkspace } from "./useRoomsWorkspace";

let root: Root | undefined;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

function Probe({ onState }: { onState: (loading: boolean) => void }) {
  // A fresh onError/requireSuccess on every render is exactly how RoomsPage calls the hook; depending on
  // them re-ran the load effect on every render (an unbounded /api/rooms loop).
  const state = useRoomsWorkspace({ onError: () => undefined, requireSuccess: () => undefined });
  onState(state.loadingList);
  return null;
}

describe("useRoomsWorkspace request stability", () => {
  it("loads the room list once even when callers pass inline callbacks every render", async () => {
    const listRooms = vi.spyOn(api, "listRooms").mockResolvedValue({ rooms: [] } as never);
    let loading = true;
    const container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root?.render(<Probe onState={(value) => { loading = value; }} />));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(loading).toBe(false);
    expect(listRooms).toHaveBeenCalledTimes(1);
  });
});
