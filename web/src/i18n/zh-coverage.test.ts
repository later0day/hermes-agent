import { describe, expect, it } from "vitest";

import { resolvePageTitle } from "@/lib/resolve-page-title";

import { en } from "./en";
import { zh } from "./zh";

type Tree = { [key: string]: unknown };

function leaves(tree: Tree, prefix = ""): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [k, v] of leaves(value as Tree, path)) out.set(k, v);
    } else {
      out.set(path, value);
    }
  }
  return out;
}

const CJK = /[一-鿿]/;

describe("zh dashboard locale", () => {
  it("resolves every English key — a key zh lacks must not render blank", () => {
    const zhLeaves = leaves(zh as unknown as Tree);
    const blank = [...leaves(en as unknown as Tree).keys()].filter((key) => {
      const value = zhLeaves.get(key);
      return value === undefined || value === "";
    });
    expect(blank).toEqual([]);
  });

  it("translates the sidebar, consent banner and kanban dialog the fork had in Chinese", () => {
    for (const key of ["files", "channels", "pairing", "system"] as const) {
      expect(zh.app.nav[key]).toMatch(CJK);
    }
    expect(zh.app.pluginTabLabels?.kanban).toMatch(CJK);
    expect(zh.app.sharedMetricsTitle).toMatch(CJK);
    expect(zh.app.sharedMetricsShare).toMatch(CJK);
    expect(zh.kanban?.create).toMatch(CJK);
    expect(zh.theme.fontSans).toMatch(CJK);
  });

  it("titles built-in and plugin pages in Chinese", () => {
    expect(resolvePageTitle("/channels", zh, [])).toBe("频道");
    expect(resolvePageTitle("/system/", zh, [])).toBe("系统");
    expect(resolvePageTitle("/kanban", zh, [{ path: "/kanban", label: "Kanban", name: "kanban" }])).toBe("看板");
    expect(resolvePageTitle("/kanban", en, [{ path: "/kanban", label: "Kanban", name: "kanban" }])).toBe("Kanban");
  });
});
