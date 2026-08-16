// @vitest-environment jsdom
import { act, useEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The ThemeProvider calls these on mount / on switch. Stub them so the test
// exercises the real applyTheme() cascade without any network.
vi.mock("@/lib/api", () => ({
  api: {
    getThemes: vi.fn().mockResolvedValue({ themes: [], active: "default" }),
    setTheme: vi.fn().mockResolvedValue({ ok: true }),
    getFontPref: vi.fn().mockResolvedValue({ font: null }),
    setFontPref: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

import { ThemeProvider, useTheme } from "./context";
import { googleTheme } from "./presets";

let container: HTMLDivElement;
let root: Root;

// jsdom (this version) ships no `CSS.escape`; the real injectFontStylesheet()
// uses it to build the dedupe selector. Provide a minimal, spec-adjacent shim
// so the font-injection path runs exactly as it does in the browser.
beforeEach(() => {
  const g = globalThis as unknown as { CSS?: { escape?: (s: string) => string } };
  if (!g.CSS) g.CSS = {};
  if (typeof g.CSS.escape !== "function") {
    g.CSS.escape = (value: string) =>
      String(value).replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`);
  }
  window.localStorage.clear();
  document.documentElement.removeAttribute("style");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.documentElement.removeAttribute("style");
});

/** Grabs the useTheme() setter so the test can drive a real theme switch. */
let switchTo: (name: string) => void = () => {};
function Harness(): ReactNode {
  const { setTheme } = useTheme();
  useEffect(() => {
    switchTo = setTheme;
  }, [setTheme]);
  return null;
}

function cssVar(name: string): string {
  return document.documentElement.style.getPropertyValue(name).trim();
}

describe("theme switch → Google", () => {
  it("registers google as a built-in selectable theme", () => {
    const captured: { names: string[] } = { names: [] };
    function Probe(): ReactNode {
      const { availableThemes } = useTheme();
      useEffect(() => {
        captured.names = availableThemes.map((t) => t.name);
      }, [availableThemes]);
      return null;
    }
    act(() => {
      root.render(
        <ThemeProvider>
          <Probe />
        </ThemeProvider>,
      );
    });
    expect(captured.names).toContain("google");
  });

  it("applies Google palette + overrides to documentElement on switch", () => {
    act(() => {
      root.render(
        <ThemeProvider>
          <Harness />
        </ThemeProvider>,
      );
    });

    // Flip to the Google theme via the real context setter.
    act(() => {
      switchTo("google");
    });

    // Palette base vars (layerVars): white canvas + dark ink text.
    expect(cssVar("--background-base").toLowerCase()).toBe("#ffffff");
    expect(cssVar("--midground-base").toLowerCase()).toBe("#202124");

    // colorOverrides → --color-* cascade.
    expect(cssVar("--color-primary").toLowerCase()).toBe("#1a73e8");
    expect(cssVar("--color-ring").toLowerCase()).toBe("#1a73e8");
    expect(cssVar("--color-border").toLowerCase()).toBe("#dadce0");
    expect(cssVar("--color-destructive").toLowerCase()).toBe("#d93025");
    expect(cssVar("--color-accent").toLowerCase()).toBe("#e8f0fe");

    // Layout radius (pill) + Roboto font stack.
    expect(cssVar("--radius")).toBe("1.5rem");
    expect(cssVar("--theme-font-sans")).toContain("Roboto");

    // Series accents (Google blue/green) for analytics charts.
    expect(cssVar("--series-input-token")).toBe("#4285F4");
    expect(cssVar("--series-output-token")).toBe("#34A853");

    // Terminal colors from the theme (light).
    expect(cssVar("--theme-terminal-background").toLowerCase()).toBe("#ffffff");
    expect(cssVar("--theme-terminal-foreground").toLowerCase()).toBe("#202124");

    // Google Fonts stylesheet injected into <head>.
    const link = document.head.querySelector(
      'link[rel="stylesheet"][href*="Roboto"]',
    );
    expect(link).not.toBeNull();
  });

  it("clears Google overrides when switching to a theme without them", () => {
    act(() => {
      root.render(
        <ThemeProvider>
          <Harness />
        </ThemeProvider>,
      );
    });
    act(() => switchTo("google"));
    expect(cssVar("--color-primary").toLowerCase()).toBe("#1a73e8");

    // 'default' (Hermes Teal) sets no colorOverrides → override vars cleared.
    act(() => switchTo("default"));
    expect(cssVar("--color-primary")).toBe("");
    expect(cssVar("--series-input-token")).toBe("");
  });

  it("presets.googleTheme is well-formed", () => {
    expect(googleTheme.name).toBe("google");
    expect(googleTheme.swatchColors).toEqual(["#4285F4", "#EA4335", "#FBBC05"]);
    expect(googleTheme.colorOverrides?.primary).toBe("#1a73e8");
  });
});
