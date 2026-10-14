import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

const CJK = /[一-鿿]/;
// Brand/identifier-only values that stay as written in Chinese UI.
const SAME_IN_ZH = new Set(["cronPageX.profile"]);

describe("cron page zh strings", () => {
  for (const ns of ["cronPageX", "cronRunHistory"] as const) {
    it(`${ns}: every English key has Chinese with the same placeholders`, () => {
      const english = en[ns] as Record<string, string>;
      const chinese = zh[ns] as Record<string, string>;
      for (const [key, value] of Object.entries(english)) {
        const zhValue = chinese[key];
        expect(zhValue, `${ns}.${key}`).toBeTruthy();
        if (!SAME_IN_ZH.has(`${ns}.${key}`)) expect(zhValue, `${ns}.${key}`).toMatch(CJK);
        expect(zhValue.match(/\{\w+\}/g) ?? [], `${ns}.${key} placeholders`).toEqual(value.match(/\{\w+\}/g) ?? []);
      }
    });
  }
});
