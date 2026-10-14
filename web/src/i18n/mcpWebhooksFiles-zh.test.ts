import { describe, expect, it } from "vitest";

import { en } from "./en";
import { localeSection } from "./section";
import { zh } from "./zh";

const CJK = /[一-鿿]/;

// Values that are identical in every locale (brand / identifier only).
const BRAND_ONLY: Record<string, string[]> = {
  mcpPage: [],
  webhooksPage: ["webhookUrl"],
  filesPage: [],
};

describe.each(["mcpPage", "webhooksPage", "filesPage"] as const)("zh %s", (section) => {
  const english = en[section] as Record<string, string>;
  const chinese = localeSection(zh, section) as Record<string, string>;

  it("translates every English key into Chinese", () => {
    const untranslated = Object.keys(english).filter((key) => {
      const value = chinese[key];
      if (!value) return true;
      return !BRAND_ONLY[section].includes(key) && !CJK.test(value);
    });
    expect(untranslated).toEqual([]);
  });

  it("keeps every {placeholder} the English string interpolates", () => {
    const mismatched = Object.keys(english).filter((key) => {
      const placeholders = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join();
      return placeholders(english[key]) !== placeholders(chinese[key] ?? "");
    });
    expect(mismatched).toEqual([]);
  });
});
