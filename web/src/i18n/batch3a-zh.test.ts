import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

const CJK = /[一-鿿]/;
const NAMESPACES = ["modelsPage", "toolsetDrawer", "modelPicker"] as const;

// Keys whose English is a brand or identifier and may stay untranslated. Every current key has a
// Chinese rendering; add "namespace.key" here only when its value is legitimately the same in every locale.
const UNTRANSLATED_ALLOWLIST = new Set<string>();

const vars = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join(",");

describe.each(NAMESPACES)("%s zh strings", (ns) => {
  const english = (en[ns] ?? {}) as Record<string, string>;
  const chinese = (zh[ns] ?? {}) as Record<string, string | undefined>;

  it("declares English strings", () => {
    expect(Object.keys(english).length).toBeGreaterThan(0);
  });

  it("gives every English key a Chinese value", () => {
    const missing = Object.keys(english).filter((key) => {
      if (UNTRANSLATED_ALLOWLIST.has(`${ns}.${key}`)) return false;
      const value = chinese[key];
      return !value || !CJK.test(value);
    });
    expect(missing).toEqual([]);
  });

  it("keeps the {placeholders} of each English string", () => {
    const mismatched = Object.entries(english).filter(
      ([key, value]) => vars(value) !== vars(chinese[key] ?? ""),
    );
    expect(mismatched).toEqual([]);
  });
});
