import { describe, expect, it } from "vitest";

import { defaultTheme, pureInkLightTheme } from "@/themes/presets";

import { mermaidThemeFor } from "./mermaid-theme";

describe("mermaidThemeFor", () => {
  it("draws light diagrams on a light dashboard and dark ones on a dark dashboard", () => {
    expect(mermaidThemeFor(pureInkLightTheme.palette.background.hex)).toBe("default");
    expect(mermaidThemeFor(defaultTheme.palette.background.hex)).toBe("dark");
  });
});
