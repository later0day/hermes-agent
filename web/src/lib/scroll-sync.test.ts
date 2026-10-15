import { describe, expect, it } from "vitest";
import { syncScrollTop } from "./scroll-sync";

describe("syncScrollTop", () => {
  // editor: entries start at 0 / 300 / 1000, max 1500; preview: 0 / 120 / 400, max 600
  const editor = [0, 300, 1000];
  const preview = [0, 120, 400];

  it("lands on the same entry start", () => {
    expect(syncScrollTop(editor, preview, 300, 1500, 600)).toBe(120);
    expect(syncScrollTop(preview, editor, 400, 600, 1500)).toBe(1000);
  });

  it("keeps the same fraction of the entry in view", () => {
    expect(syncScrollTop(editor, preview, 650, 1500, 600)).toBe(260); // halfway through entry 2
  });

  it("maps the tail past the last entry and clamps out-of-range input", () => {
    expect(syncScrollTop(editor, preview, 1500, 1500, 600)).toBe(600);
    expect(syncScrollTop(editor, preview, 5000, 1500, 600)).toBe(600);
    expect(syncScrollTop(editor, preview, -10, 1500, 600)).toBe(0);
  });

  it("tolerates anchors beyond a short pane's max scroll", () => {
    expect(syncScrollTop([0, 900], [0, 50], 450, 900, 40)).toBe(20);
  });
});
