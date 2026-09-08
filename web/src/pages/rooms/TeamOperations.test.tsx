// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProfileInfo, RoomSummary, RoomTopologyResponse } from "@/lib/api";
import { buildMemberHandles, CreateTeamDialog, WorkComposer } from "./TeamOperations";

const profile = (name: string): ProfileInfo => ({ name, path: "/profiles/" + name, is_default: false, model: "model", provider: "provider", has_env: true, skill_count: 0, gateway_running: true, description: name + " agent", description_auto: false, display_name: name[0].toUpperCase() + name.slice(1), distribution_name: null, distribution_version: null, distribution_source: null, has_alias: false });
let root: Root | undefined;
afterEach(async () => { if (root) await act(async () => root?.unmount()); document.body.innerHTML = ""; root = undefined; });
async function render(node: React.ReactNode) { const container = document.createElement("div"); document.body.append(container); root = createRoot(container); await act(async () => root?.render(node)); return container; }

describe("TeamOperations", () => {
  it("requires 2–6 profiles and one leader, then emits decider/worker roster", async () => {
    const onCreate = vi.fn(async (...args: [import("@/lib/api").RoomCreateRequest]) => Boolean(args[0]));
    await render(<CreateTeamDialog profiles={[profile("lead"), profile("researcher")]} createOpen onCloseCreate={() => undefined} onCreate={onCreate} />);
    const submit = document.querySelector<HTMLButtonElement>("[data-testid=create-team-submit]")!;
    expect(submit.disabled).toBe(true);
    await act(async () => { const input = document.querySelector<HTMLInputElement>("[aria-label='Team name']")!; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Research Team"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    const checks = [...document.querySelectorAll<HTMLInputElement>("input[type=checkbox]")];
    await act(async () => { checks.forEach((check) => check.click()); });
    await act(async () => { const select = document.querySelector<HTMLSelectElement>("[aria-label='Team leader']")!; Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, "lead"); select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(submit.disabled).toBe(false);
    await act(async () => submit.click());
    expect(onCreate).toHaveBeenCalledTimes(1);
    const request = onCreate.mock.calls[0]![0];
    expect(request.name).toBe("Research Team");
    expect(request.members.map((member) => [member.profile, member.role])).toEqual([["lead", "decider"], ["researcher", "worker"]]);
  });


  it("builds unique non-empty handles for duplicate and Unicode display names", () => {
    const first = { ...profile("alpha"), display_name: "Same Name" };
    const second = { ...profile("beta"), display_name: "Same Name" };
    const unicode = { ...profile("researcher"), display_name: "研究员" };
    expect(buildMemberHandles([first, second, unicode], ["alpha", "beta", "researcher"]).map((member) => member.handle)).toEqual(["same-name", "same-name-2", "researcher"]);
  });


  it("resets a stale recipient when the selected team changes", async () => {
    const onDelegate = vi.fn(async () => true);
    const roomA = { room_id: "a", name: "A", members: [], revision: 1, latest_seq: 1, authority_epoch: 1 } as RoomSummary;
    const roomB = { ...roomA, room_id: "b", name: "B" } as RoomSummary;
    const topologyA = { room_id: "a", members: [{ member_id: "lead-a", profile: "lead-a", handle: "lead-a", role: "team_lead" }, { member_id: "worker-a", profile: "worker-a", handle: "worker-a", role: "teammate" }] } as RoomTopologyResponse;
    const topologyB = { room_id: "b", members: [{ member_id: "lead-b", profile: "lead-b", handle: "lead-b", role: "team_lead" }, { member_id: "worker-b", profile: "worker-b", handle: "worker-b", role: "teammate" }] } as RoomTopologyResponse;
    await render(<WorkComposer room={roomA} topology={topologyA} onDelegate={onDelegate} />);
    const recipient = document.querySelector<HTMLSelectElement>("[aria-label='Work recipient']")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(recipient, "worker-a"); recipient.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(recipient.value).toBe("worker-a");
    await act(async () => root!.render(<WorkComposer room={roomB} topology={topologyB} onDelegate={onDelegate} />));
    expect(document.querySelector<HTMLSelectElement>("[aria-label='Work recipient']")!.value).toBe("lead-b");
  });

  it("defaults to the leader and can delegate directly to a worker", async () => {
    const onDelegate = vi.fn(async () => true);
    const room = { room_id: "room", name: "Room", members: [], revision: 1, latest_seq: 1, authority_epoch: 1 } as RoomSummary;
    const topology = { room_id: "room", members: [{ member_id: "lead", profile: "lead", handle: "lead", role: "team_lead" }, { member_id: "worker", profile: "worker", handle: "worker", role: "teammate" }] } as RoomTopologyResponse;
    await render(<WorkComposer room={room} topology={topology} onDelegate={onDelegate} />);
    const recipient = document.querySelector<HTMLSelectElement>("[aria-label='Work recipient']")!;
    expect(recipient.value).toBe("lead");
    expect(recipient.textContent).toContain("Leader · lead");
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(recipient, "worker"); recipient.dispatchEvent(new Event("change", { bubbles: true })); });
    const task = document.querySelector<HTMLTextAreaElement>("[aria-label='New team task']")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(task, "Verify the sources"); task.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => document.querySelector<HTMLButtonElement>("[data-testid=delegate-work-submit]")!.click());
    expect(onDelegate).toHaveBeenCalledWith("worker", "Verify the sources");
  });
});
