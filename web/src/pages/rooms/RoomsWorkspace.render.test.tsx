// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RoomConversationItem, RoomSummary, RoomWorkspaceResponse } from "@/lib/api";
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

async function renderWorkspace(value: RoomsWorkspaceProps) {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(<RoomsWorkspace {...value} />));
  return container;
}

function message(overrides: Partial<RoomConversationItem>): RoomConversationItem {
  return {
    event_id: overrides.event_id ?? "event",
    kind: overrides.kind ?? "message.member",
    actor_id: overrides.actor_id ?? "researcher",
    actor_kind: overrides.actor_kind ?? "member",
    text: overrides.text ?? "Evidence update",
    created_at: overrides.created_at ?? 1,
    ...overrides,
  };
}

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

  it("elevates the final leader report and keeps internal task ids out of the conversation", async () => {
    const value = props([summary("research-team", 3, 3)]);
    value.tab = "conversation";
    value.workspace!.conversation = [
      message({ event_id: "brief", actor_id: "desktop", actor_kind: "user", text: "A very long research brief ".repeat(30) }),
      message({ event_id: "old-report", actor_id: "leader-a", text: "LEADER_REPORT: Historical report remains visible." }),
      message({ event_id: "finding", actor_id: "site-a", task_id: "task/private-non-hex", text: "Source A evidence" }),
      message({ event_id: "report", actor_id: "leader-c", task_id: "task/private-final", text: "LEADER_REPORT: Sources agree after temporal reconciliation." }),
    ];

    const container = await renderWorkspace(value);
    expect(container.querySelectorAll('[aria-label="Final leader report"]')).toHaveLength(1);
    expect(container.querySelector('[aria-label="Final leader report"]')?.textContent).toContain("Sources agree");
    expect(container.textContent).toContain("Read full brief");
    expect(container.querySelector('[aria-label="Conversation thread"]')?.textContent).toContain("Historical report remains visible");
    expect(container.querySelector('[aria-label="Final leader report"]')?.textContent).not.toContain("Historical report remains visible");
    expect(container.textContent).not.toContain("task/private");
  });

  it("renders a concise private Timeline instead of raw prompts and task ids", async () => {
    const value = props([summary("research-team", 1, 1)]);
    value.tab = "activity";
    value.workspace!.activity = [{
      event_id: "user-event", seq: 1, kind: "message.user", category: "messages",
      title: "PRIVATE FULL RESEARCH PROMPT", summary: "PRIVATE FULL RESEARCH PROMPT",
      created_at: 1, raw_event: { room_id: "research-team", seq: 1, event_id: "user-event", kind: "message.user", actor: {}, authority_epoch: 1, created_at: 1, redacted: true },
    }, {
      event_id: "turn-event", seq: 2, kind: "turn.settled", category: "tasks",
      member_id: "member-private", title: "SECRET title task/not-hex", summary: "SECRET summary token-123",
      created_at: 2, raw_event: { room_id: "research-team", seq: 2, event_id: "turn-event", kind: "turn.settled", actor: {}, authority_epoch: 1, created_at: 2, redacted: true },
    }];

    const container = await renderWorkspace(value);
    expect(container.textContent).toContain("Research brief received");
    expect(container.textContent).not.toContain("PRIVATE FULL RESEARCH PROMPT");
    expect(container.textContent).toContain("Work completed");
    expect(container.textContent).not.toContain("SECRET title");
    expect(container.textContent).not.toContain("SECRET summary");
    expect(container.textContent).not.toContain("task/not-hex");
    expect(container.textContent).not.toContain("member-private");
    expect(container.querySelector("details")?.open).toBe(false);
  });

  it("shows ordered graph handoffs without exposing task ids", async () => {
    const value = props([summary("research-team", 2, 2)]);
    value.taskMode = "graph";
    value.workspace!.tasks = [
      { task_id: "task/second", subject: "@researcher · Round 2", status: "settled", visual_state: "completed", owner: "researcher", blockedBy: ["task:first"], pending_actions: [] },
      { task_id: "task:independent", subject: "@reviewer · Independent", status: "settled", visual_state: "completed", owner: "reviewer", pending_actions: [] },
      { task_id: "task:first", subject: "@leader · Round 1", status: "settled", visual_state: "completed", owner: "leader", pending_actions: [] },
    ];

    const container = await renderWorkspace(value);
    const graphText = container.querySelector("ol")?.textContent ?? "";
    expect(container.textContent).toContain("Execution order and handoffs");
    expect(graphText.indexOf("Round 1")).toBeLessThan(graphText.indexOf("Round 2"));
    expect(graphText).toContain("Round 2@researcher · after @leader · Round 1");
    expect(graphText).toContain("Independent@reviewer · starts independently");
    expect(graphText).not.toContain("Independent@reviewer · after");
    expect(container.textContent).not.toContain("task/");
    expect(container.textContent).not.toContain("task:");
  });

  it("fails closed for an unknown runtime action kind", async () => {
    const value = props([summary("action-room", 0, 1)]);
    value.actionCenterAction = {
      action_id: "unknown-1", room_id: "action-room", kind: "future_kind", from_handle: "leader",
      description: "Unsupported request", created_at: 1, detail: {},
    } as unknown as NonNullable<RoomsWorkspaceProps["actionCenterAction"]>;
    value.onApproveAction = vi.fn();
    value.onDenyAction = vi.fn();

    await renderWorkspace(value);
    const dialog = document.querySelector('[role="dialog"]');
    const labels = [...dialog!.querySelectorAll("button")].map((button) => button.textContent);
    expect(dialog?.textContent).toContain("Unsupported action");
    expect(labels).toContain("Close");
    expect(labels).not.toContain("Allow once");
    expect(labels).not.toContain("Deny");
  });

  it("keeps retry actions approve-only and strips unsafe detail", async () => {
    const value = props([summary("action-room", 0, 1)]);
    const action = {
      action_id: "retry-1", room_id: "action-room", kind: "retry", from_handle: "leader",
      description: "Retry uncertain Room turn", created_at: 1,
      detail: { status: "deferred", execution_generation: 1, task_id: "dtask:private", command: "PRIVATE COMMAND", cwd: "/private", env: "SECRET" },
    } as unknown as NonNullable<RoomsWorkspaceProps["actionCenterAction"]>;
    value.actionCenterAction = action;
    value.onApproveAction = vi.fn();
    value.onDenyAction = vi.fn();

    await renderWorkspace(value);
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("Retry uncertain Room turn");
    expect(dialog?.textContent).toContain("execution generation");
    expect(dialog?.textContent).not.toContain("dtask:");
    expect(dialog?.textContent).not.toContain("PRIVATE COMMAND");
    expect(dialog?.textContent).not.toContain("/private");
    expect(dialog?.textContent).not.toContain("SECRET");
    expect([...dialog!.querySelectorAll("button")].some((button) => button.textContent === "Deny")).toBe(false);
  });

  it("keeps the workspace Action review control compact and responsive", async () => {
    const value = props([summary("action-room", 0, 1)]);
    value.workspace!.pending_actions = [{
      action_id: "retry-card", room_id: "action-room", kind: "retry", from_handle: "leader",
      description: "Retry uncertain Room turn", created_at: 1, detail: { status: "deferred" },
    }];

    const container = await renderWorkspace(value);
    const button = container.querySelector('[data-testid="room-action-review"]');
    expect(button).not.toBeNull();
    expect(button?.className).toContain("h-9");
    expect(button?.className).toContain("w-full");
    expect(button?.className).toContain("sm:w-auto");
    expect(button?.className).toContain("whitespace-nowrap");
    expect(button?.className).toContain("self-center");
    expect(button?.closest("div")?.className).toContain("sm:grid-cols-[minmax(0,1fr)_auto]");
  });
});
