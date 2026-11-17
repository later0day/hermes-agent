import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

const CJK = /[一-鿿]/;

// Keys whose English is a brand or identifier and may stay untranslated. Every current key has a
// Chinese rendering; add a key here only when its value is legitimately the same in every locale.
const UNTRANSLATED_ALLOWLIST = new Set<string>();

describe("roomsWorkspace zh strings", () => {
  it("gives every English key a Chinese value", () => {
    const zhSection = (zh.roomsWorkspace ?? {}) as Record<string, string | undefined>;
    const missing = Object.keys(en.roomsWorkspace ?? {}).filter((key) => {
      if (UNTRANSLATED_ALLOWLIST.has(key)) return false;
      const value = zhSection[key];
      return !value || !CJK.test(value);
    });
    expect(missing).toEqual([]);
  });

  it("keeps the {placeholders} of each English string", () => {
    const zhSection = (zh.roomsWorkspace ?? {}) as Record<string, string | undefined>;
    const mismatched = Object.entries(en.roomsWorkspace ?? {}).filter(([key, english]) => {
      const vars = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join(",");
      return vars(english) !== vars(zhSection[key] ?? "");
    });
    expect(mismatched).toEqual([]);
  });
});
