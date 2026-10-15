// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

// Memory page: how full each store is (the memory tool refuses `add` past the limit — xcx sat at 98% on
// 2026-10-15 with nothing on this page saying so), one preview card per entry, and an unsaved edit that
// survives the per-profile remount instead of vanishing.

const store = vi.hoisted(() => ({
  "MEMORY.md": { content: "first *note*\n§\nSELECT * FROM t; COUNT(*) works", char_limit: 40 },
  "USER.md": { content: "", char_limit: 1375 },
}));

vi.mock("@/lib/api-memory", () => ({
  memoryApi: {
    get: async (_profile: string, doc: "MEMORY.md" | "USER.md") => ({ ...store[doc], exists: !!store[doc].content }),
    update: vi.fn(async () => ({ ok: true })),
  },
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | undefined;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  localStorage.clear();
  sessionStorage.clear();
});

async function renderMemory(view: "edit" | "preview" | "split") {
  localStorage.setItem("hermes-memory-view", view);
  const [{ default: MemoryPage }, { I18nProvider }, { ProfileContext }, { PageHeaderProvider }] = await Promise.all([
    import("./MemoryPage"), import("@/i18n"), import("@/contexts/profile-context"), import("@/contexts/PageHeaderProvider"),
  ]);
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(
    <MemoryRouter><I18nProvider>
      <ProfileContext.Provider value={{ profile: "xcx", currentProfile: "default", profiles: ["default", "xcx"], setProfile: () => {} }}>
        <PageHeaderProvider pluginTabs={[]}><MemoryPage /></PageHeaderProvider>
      </ProfileContext.Provider>
    </I18nProvider></MemoryRouter>,
  ));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
  return container;
}

describe("MemoryPage", () => {
  it("shows usage against the profile's limit, warns near it, and previews one card per entry", async () => {
    const page = await renderMemory("preview");
    // "first *note*" (12) + "\n§\n" (3) + "SELECT * FROM t; COUNT(*) works" (31) = 46 of 40: over the limit
    expect(page.textContent).toContain("46 / 40 chars");
    expect(page.textContent).toContain("Entries: 2");
    expect(page.textContent).toContain("Over the limit");
    const cards = page.querySelectorAll("ol > li");
    expect(cards).toHaveLength(2);
    expect(cards[0].querySelector("em")?.textContent).toBe("note");
    expect(cards[1].querySelector("em")).toBeNull(); // SELECT * … COUNT(*) stays literal
  });

  it("keeps an unsaved edit across a remount and restores it with a notice", async () => {
    let page = await renderMemory("edit");
    const area = page.querySelector<HTMLTextAreaElement>("#memory-editor-MEMORY\\.md")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(area, "draft text");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(page.textContent).toContain("Unsaved changes");

    await act(async () => root?.unmount());
    root = undefined;
    document.body.innerHTML = "";
    page = await renderMemory("edit");
    expect(page.querySelector<HTMLTextAreaElement>("#memory-editor-MEMORY\\.md")!.value).toBe("draft text");
    expect(page.textContent).toContain("Restored your unsaved draft");
  });
});
