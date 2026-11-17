// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

// Dashboard-side /agent use (fork parity, 1e481b7501): bind a known chat to a profile and unbind one.

const calls = vi.hoisted(() => ({ bind: [] as unknown[], unbind: [] as string[] }));
vi.mock("@/lib/api-profile-bindings", () => ({
  profileBindingsApi: {
    chats: async () => ({ chats: [
      { platform: "dingtalk", chat_id: "cidA==", name: "Ops group", chat_type: "group" },
      { platform: "dingtalk", chat_id: "cidB==", name: "Already bound", chat_type: "group" },
    ] }),
    bind: async (profile: string, chat: unknown) => { calls.bind.push([profile, chat]); return { ok: true }; },
    unbind: async (key: string) => { calls.unbind.push(key); return { ok: true }; },
  },
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | undefined;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  calls.bind.length = 0;
  calls.unbind.length = 0;
});

async function render(onChanged: () => void) {
  const [{ ProfileBindingEditor }, { I18nProvider }] = await Promise.all([import("./ProfileBindingEditor"), import("@/i18n")]);
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const dynamic = [{ source_binding_key: "source:dingtalk:group:cidB==", platform: "dingtalk", scope: "group",
                     chat_id: "cidB==", agent_id: "ops", has_fallback_webhook: false }];
  await act(async () => root?.render(
    <I18nProvider><ProfileBindingEditor profile="ops" summary={{ profile: "ops", static: [], dynamic } as never} onChanged={onChanged} onError={() => undefined} /></I18nProvider>,
  ));
  return container;
}

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 20)); });

describe("ProfileBindingEditor", () => {
  it("binds a chosen chat and unbinds an existing binding", async () => {
    const onChanged = vi.fn();
    const container = await render(onChanged);
    await act(async () => (container.querySelector("button") as HTMLButtonElement).click());  // Manage
    await settle();

    await act(async () => (container.querySelector('[role="combobox"]') as HTMLElement).click());
    const options = [...document.querySelectorAll('[role="option"]')] as HTMLElement[];
    expect(options.some((o) => o.textContent?.includes("Ops group"))).toBe(true);
    expect(options.some((o) => o.textContent?.includes("Already bound"))).toBe(false);   // already bound here
    await act(async () => options.find((o) => o.textContent?.includes("Ops group"))!.click());
    const bindButton = [...container.querySelectorAll("button")].find((b) => b.textContent === "Bind")!;
    await act(async () => bindButton.click());
    await settle();
    expect(calls.bind).toEqual([["ops", { platform: "dingtalk", chat_id: "cidA==", name: "Ops group", chat_type: "group" }]]);

    await act(async () => (container.querySelector('button[aria-label="Unbind this chat"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.unbind).toEqual(["source:dingtalk:group:cidB=="]);
    expect(onChanged).toHaveBeenCalledTimes(2);
  });
});
