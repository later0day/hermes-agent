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
    const conversationLayout = container.querySelector('[aria-label="Conversation thread"]')?.parentElement;
    expect(conversationLayout?.className).not.toContain("grid-cols-");
    expect(conversationLayout?.className).toContain("min-w-0");
  });

  it("uses the full workspace width when no final report exists", async () => {
    const value = props([summary("wide-room", 1, 1)]);
    value.tab = "conversation";
    value.workspace!.conversation = [message({ event_id: "update", text: "A normal team update" })];

    const container = await renderWorkspace(value);
    const thread = container.querySelector('[aria-label="Conversation thread"]');
    expect(thread).not.toBeNull();
    expect(thread?.nextElementSibling).toBeNull();
    expect(thread?.parentElement?.className).not.toContain("grid-cols-");
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
    expect(dialog?.textContent).toContain("Action required");
    expect(dialog?.textContent).toContain("Retry uncertain Room turn");
    expect(dialog?.textContent).toContain("execution generation");
    expect(dialog?.textContent).toContain("Retry only if the previous attempt did not complete");
    expect(dialog?.textContent).not.toContain("dtask:");
    expect(dialog?.textContent).not.toContain("PRIVATE COMMAND");
    expect(dialog?.textContent).not.toContain("/private");
    expect(dialog?.textContent).not.toContain("SECRET");
    const buttons = [...dialog!.querySelectorAll("button")];
    expect(buttons.some((button) => button.textContent === "Deny")).toBe(false);
    expect(buttons.some((button) => button.textContent === "Cancel")).toBe(true);
    const footer = [...dialog!.querySelectorAll("div")].find((element) => element.className.includes("sm:flex-row") && element.querySelector("button"));
    expect(footer?.className).toContain("flex-col");
    expect(buttons.find((button) => button.textContent === "Retry")?.className).toContain("w-full");
    expect(dialog?.getAttribute("data-testid")).toBe("room-action-dialog");
    expect(dialog?.className).toContain("w-[min(30rem,calc(100vw-2rem))]");
    expect(dialog?.className).toContain("max-h-[85vh]");
    expect(dialog?.className).toContain("overflow-y-auto");
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

  it("wraps every arbitrary-content surface without changing diagnostic scrolling", async () => {
    const long = "x".repeat(512);
    const room = summary("wrapping-room", 0, 2);
    room.workspace!.current_task = { subject: long } as NonNullable<NonNullable<typeof room.workspace>["current_task"]>;
    const value = props([room]);
    value.topology = {
      room_id: room.room_id,
      coordinator_id: null,
      team_lead_id: null,
      members: [{ member_id: "member-1", handle: "worker", profile: "default", role: "teammate", current_task: long }],
    };
    value.workspace!.tasks = [
      { task_id: "task-1", subject: long, description: long, status: "running", visual_state: "running", owner: "worker", pending_actions: [] },
      { task_id: "task-2", subject: long, description: long, status: "pending", visual_state: "pending", blockedBy: ["task-1"], pending_actions: [] },
    ];
    value.workspace!.conversation = [
      message({ event_id: "brief", actor_kind: "user", text: long }),
      message({ event_id: "member", text: long }),
      message({ event_id: "report", kind: "leader_report", text: `LEADER_REPORT: ${long}` }),
    ];
    value.workspace!.activity = [{
      event_id: "event-1", seq: 1, kind: "custom.safe", category: "other", title: long, summary: long,
      created_at: 1, raw_event: { room_id: room.room_id, seq: 1, event_id: "event-1", kind: "custom.safe", actor: {}, authority_epoch: 1, created_at: 1, redacted: true },
    }];
    value.workspace!.pending_actions = [{
      action_id: "action-card", room_id: room.room_id, kind: "retry", from_handle: "leader", description: long, created_at: 1, detail: {},
    }];
    value.actionCenterAction = {
      action_id: "action-dialog", room_id: room.room_id, kind: "permission", from_handle: "leader", description: long, created_at: 1,
      detail: { scope: long, reason: long },
    };

    const assertWrap = (element: Element | null | undefined) => {
      expect(element).not.toBeNull();
      expect(element?.className).toContain("min-w-0");
      expect(element?.className).toContain("max-w-full");
      expect(element?.className).toContain("break-words");
      expect(element?.className).toContain("[overflow-wrap:anywhere]");
    };

    let container = await renderWorkspace(value);
    assertWrap(container.querySelector("main header p.mt-1"));
    assertWrap([...container.querySelectorAll("section[aria-labelledby=room-team-heading] span")].find((node) => node.textContent?.startsWith(long)));
    assertWrap([...container.querySelectorAll("button small")].find((node) => node.textContent === long));
    assertWrap([...container.querySelectorAll("span")].find((node) => node.textContent === long && node.closest('[aria-label="Action Center"]')));
    assertWrap(document.querySelector('[role="dialog"]'));
    assertWrap([...document.querySelectorAll("dd")].find((node) => node.textContent === long));

    await act(async () => root?.unmount());
    root = undefined;
    value.tab = "conversation";
    container = await renderWorkspace(value);
    const thread = container.querySelector('[aria-label="Conversation thread"]')!;
    assertWrap(thread.querySelector("details summary"));
    assertWrap(thread.querySelector("details summary span:first-child"));
    assertWrap([...thread.querySelectorAll("p")].find((node) => node.textContent === long));
    assertWrap(container.querySelector('[aria-label="Final leader report"]'));
    assertWrap(container.querySelector('[aria-label="Final leader report"] p'));

    await act(async () => root?.unmount());
    root = undefined;
    value.tab = "activity";
    value.inspector = { kind: "event", eventId: "event-1" };
    container = await renderWorkspace(value);
    assertWrap(container.querySelector("ol li"));
    assertWrap(container.querySelector("ol li button"));
    assertWrap(container.querySelector('[aria-label="Context inspector"] h2'));
    assertWrap(container.querySelector('[aria-label="Context inspector"] p.mt-3'));
    expect(container.querySelector("details .font-mono")?.className).toContain("break-all");

    await act(async () => root?.unmount());
    root = undefined;
    value.tab = "tasks";
    value.inspector = { kind: "task", taskId: "task-1" };
    container = await renderWorkspace(value);
    assertWrap(container.querySelector('[aria-label="Context inspector"] h2'));
    assertWrap([...container.querySelectorAll('[aria-label="Context inspector"] p')].find((node) => node.textContent === long));

    await act(async () => root?.unmount());
    root = undefined;
    value.taskMode = "graph";
    container = await renderWorkspace(value);
    for (const node of container.querySelectorAll("ol button .font-semibold, ol button .text-text-tertiary")) assertWrap(node);
  });

  it("constrains every workspace action control instead of relying on inherited Button sizing", async () => {
    const value = props([summary("research-room", 1, 1)]);
    value.tab = "conversation";
    value.workspace!.conversation = [
      message({ event_id: "finding", actor_id: "researcher", task_id: "task-1", text: "Evidence" }),
      message({ event_id: "report", actor_id: "leader", task_id: "task-1", text: "LEADER_REPORT: Result" }),
    ];

    const container = await renderWorkspace(value);
    const actionButtons = [...container.querySelectorAll("button")].filter((button) =>
      /View related task|Open related task|Review action|Review retry/.test(button.textContent ?? ""),
    );
    expect(actionButtons.length).toBeGreaterThan(0);
    for (const button of actionButtons) {
      expect(button.className).toMatch(/h-(8|9)/);
      expect(button.className).toContain("whitespace-nowrap");
      expect(button.className).toMatch(/w-auto|sm:w-auto/);
    }
  });
});
