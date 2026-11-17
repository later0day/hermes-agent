import { describe, expect, it } from "vitest";
import { parseBlocks, parseInline, type InlineNode } from "./markdown-parse";

const types = (nodes: InlineNode[]) => nodes.map((n) => n.type);
const text = (nodes: InlineNode[]): string =>
  nodes.map((n) => ("content" in n ? n.content : "children" in n ? text(n.children) : n.type === "link" ? n.text : "\n")).join("");

describe("parseInline emphasis", () => {
  it("keeps stars with whitespace or punctuation around them literal (live memory entry)", () => {
    const line = "SLS查询：GET SELECT * 可能返回空，查明细必须POST download。只有简单COUNT(*)可靠。";
    const nodes = parseInline(line);
    expect(types(nodes)).toEqual(["text"]);
    expect(text(nodes)).toBe(line);
  });

  it("leaves arithmetic and globs alone", () => {
    for (const s of ["a * b * c", "rm *.log and *.tmp", "2*3 = 6"]) expect(types(parseInline(s))).toEqual(["text"]);
  });

  it("renders flanked emphasis, nested inline code and CJK bold", () => {
    const nodes = parseInline("**用户核心偏好（极重要）**：*注意* `x*y` ~~旧~~");
    expect(types(nodes)).toEqual(["bold", "text", "italic", "text", "code", "text", "strike"]);
    expect(nodes[4]).toEqual({ type: "code", content: "x*y" });
  });

  it("honours backslash escapes and keeps identifiers with underscores literal", () => {
    expect(text(parseInline("\\*not italic\\* pfb_wxapp list_account_traffic_files"))).toBe(
      "*not italic* pfb_wxapp list_account_traffic_files");
  });
});

describe("parseBlocks", () => {
  it("nests indented list items and keeps lazy continuation lines in the item", () => {
    const [list] = parseBlocks("1. first\n   still first\n2. second\n   - child a\n   - child b\n3. third");
    expect(list).toMatchObject({ type: "list", ordered: true, start: 1 });
    if (list.type !== "list") throw new Error("not a list");
    expect(list.items.map((i) => i.content)).toEqual(["first\nstill first", "second", "third"]);
    expect(list.items[1].children[0]).toMatchObject({ type: "list", ordered: false });
  });

  it("recognises task items, ordered start numbers, blockquotes and h5/h6", () => {
    const blocks = parseBlocks("- [x] done\n- [ ] todo\n\n3. three\n4. four\n\n> quoted **text**\n> more\n\n##### five\n###### six");
    expect(blocks.map((b) => b.type)).toEqual(["list", "list", "blockquote", "heading", "heading"]);
    if (blocks[0].type !== "list" || blocks[1].type !== "list") throw new Error("lists expected");
    expect(blocks[0].items.map((i) => i.checked)).toEqual([true, false]);
    expect(blocks[1].start).toBe(3);
  });

  it("parses GFM tables with alignment and pads short rows", () => {
    const [table] = parseBlocks("| 产品 | 价格 | 备注 |\n|:---|---:|:-:|\n| Cursor | $20 | Pro |\n| Claude | $17 |");
    expect(table).toEqual({
      type: "table", align: ["left", "right", "center"], header: ["产品", "价格", "备注"],
      rows: [["Cursor", "$20", "Pro"], ["Claude", "$17", ""]],
    });
  });

  it("does not mistake a lone pipe line or a rule for a table or list", () => {
    expect(parseBlocks("a | b\nplain").map((b) => b.type)).toEqual(["paragraph"]);
    expect(parseBlocks("- - -\n* * *").map((b) => b.type)).toEqual(["hr", "hr"]);
  });

  it("keeps fenced code verbatim, including ~~~ fences", () => {
    const [code] = parseBlocks("~~~sql\nSELECT * FROM t -- **x**\n~~~");
    expect(code).toEqual({ type: "code", lang: "sql", content: "SELECT * FROM t -- **x**" });
  });
});
