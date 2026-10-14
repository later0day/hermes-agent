import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

const CJK = /[一-鿿]/;

const NAMESPACES = [
  "profileBuilderPage",
  "chatPageX",
  "envPageX",
  "analyticsPageX",
  "profilesPageX",
] as const;

// `namespace.key` whose value is a brand or identifier and legitimately the same in every locale.
const UNTRANSLATED_ALLOWLIST = new Set<string>(["profileBuilderPage.stepMcp"]);

const placeholders = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join(",");

describe.each(NAMESPACES)("%s zh strings", (ns) => {
  const english = (en[ns] ?? {}) as Record<string, string>;
  const chinese = (zh[ns] ?? {}) as Record<string, string | undefined>;

  it("gives every English key a non-empty Chinese value", () => {
    expect(Object.keys(english).length).toBeGreaterThan(0);
    const missing = Object.keys(english).filter((key) => {
      const value = chinese[key];
      if (UNTRANSLATED_ALLOWLIST.has(`${ns}.${key}`)) return !value;
      return !value || !CJK.test(value);
    });
    expect(missing).toEqual([]);
  });

  it("keeps the {placeholders} of each English string", () => {
    const mismatched = Object.entries(english)
      .filter(([key, text]) => placeholders(text) !== placeholders(chinese[key] ?? ""))
      .map(([key]) => key);
    expect(mismatched).toEqual([]);
  });
});
