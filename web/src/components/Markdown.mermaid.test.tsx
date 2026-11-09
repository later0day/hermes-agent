// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "./Markdown";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { initialize, renderDiagram } = vi.hoisted(() => ({
  initialize: vi.fn(),
  renderDiagram: vi.fn(),
}));

vi.mock("mermaid", () => ({
  default: { initialize, render: renderDiagram },
}));

let container: HTMLDivElement | undefined;
let root: Root | undefined;

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  container = undefined;
  root = undefined;
  vi.clearAllMocks();
});

describe("Markdown Mermaid blocks", () => {
  it("loads Mermaid lazily and renders the diagram with strict security", async () => {
    renderDiagram.mockResolvedValue({ svg: '<svg data-testid="diagram"><text>A to B</text></svg>' });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<Markdown content={"```mermaid\ngraph TD; A-->B\n```"} />);
      await Promise.resolve();
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(initialize).toHaveBeenCalledWith({
      startOnLoad: false,
      theme: "dark",
      securityLevel: "strict",
    });
    expect(renderDiagram).toHaveBeenCalledWith(expect.stringMatching(/^mermaid-\d+$/), "graph TD; A-->B");
    expect(container.querySelector('[data-testid="diagram"]')?.textContent).toBe("A to B");
  });
});
