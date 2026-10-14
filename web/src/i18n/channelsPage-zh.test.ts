import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

const CJK = /[一-鿿]/;

// Keys whose Chinese is legitimately brand/identifier-only; empty today, kept explicit on purpose.
const IDENTIFIER_ONLY = new Set<string>([]);

describe.each(["channelsPage", "pairingPage"] as const)("zh %s", (section) => {
  it("translates every English key into Chinese", () => {
    const zhSection = (zh[section] ?? {}) as Record<string, string | undefined>;
    const untranslated = Object.keys(en[section] ?? {}).filter((key) => {
      const value = zhSection[key];
      if (!value) return true;
      return !IDENTIFIER_ONLY.has(key) && !CJK.test(value);
    });
    expect(untranslated).toEqual([]);
  });

  it("keeps every {placeholder} the English string interpolates", () => {
    const zhSection = (zh[section] ?? {}) as Record<string, string>;
    const mismatched = Object.entries(en[section] ?? {}).filter(([key, value]) => {
      const tokens = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(",");
      return tokens(value) !== tokens(zhSection[key] ?? "");
    });
    expect(mismatched).toEqual([]);
  });
});
