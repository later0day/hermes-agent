/**
 * Markdown → node tree for the dashboard's lightweight renderer (components/Markdown.tsx).
 *
 * Not a full CommonMark parser, but the parts that matter for agent output and memory files follow
 * its rules: emphasis needs left/right-flanking delimiters (so `SELECT * … COUNT(*)` stays literal
 * instead of italicising everything between the stars), lists nest by indentation and keep lazy
 * continuation lines, and GFM tables, blockquotes, strikethrough and task items are recognised.
 */

export type InlineNode =
  | { type: "text"; content: string }
  | { type: "code"; content: string }
  | { type: "bold" | "italic" | "strike"; children: InlineNode[] }
  | { type: "link"; text: string; href: string }
  | { type: "br" };

export type Align = "left" | "center" | "right" | null;

export interface ListItem {
  content: string;
  checked: boolean | null;
  children: BlockNode[];
}

export type BlockNode =
  | { type: "code"; lang: string; content: string }
  | { type: "heading"; level: number; content: string }
  | { type: "hr" }
  | { type: "list"; ordered: boolean; start: number; items: ListItem[] }
  | { type: "blockquote"; children: BlockNode[] }
  | { type: "table"; align: Align[]; header: string[]; rows: string[][] }
  | { type: "paragraph"; content: string };

/* ------------------------------------------------------------------ */
/*  Blocks                                                             */
/* ------------------------------------------------------------------ */

const FENCE = /^ {0,3}(```|~~~)\s*([\w+-]*)/;
const HEADING = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const QUOTE = /^ {0,3}>\s?(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

type Parsed = { block: BlockNode; next: number } | null;
type BlockRule = (lines: string[], i: number) => Parsed;

const indentOf = (line: string) => line.length - line.trimStart().length;
const isBlank = (line: string | undefined) => line === undefined || line.trim() === "";

function parseFence(lines: string[], i: number): Parsed {
  const open = lines[i].match(FENCE);
  if (!open) return null;
  const body: string[] = [];
  let j = i + 1;
  while (j < lines.length && !lines[j].trimStart().startsWith(open[1])) body.push(lines[j++]);
  return { block: { type: "code", lang: open[2] || "", content: body.join("\n") }, next: j + 1 };
}

function parseHeading(lines: string[], i: number): Parsed {
  const m = lines[i].match(HEADING);
  return m ? { block: { type: "heading", level: m[1].length, content: m[2] }, next: i + 1 } : null;
}

function parseHr(lines: string[], i: number): Parsed {
  return HR.test(lines[i]) ? { block: { type: "hr" }, next: i + 1 } : null;
}

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function isTableStart(lines: string[], i: number): boolean {
  return lines[i].includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])
    && lines[i + 1].includes("-");
}

function parseTable(lines: string[], i: number): Parsed {
  if (!isTableStart(lines, i)) return null;
  const header = splitRow(lines[i]);
  const align: Align[] = splitRow(lines[i + 1]).map((cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    return left && right ? "center" : right ? "right" : left ? "left" : null;
  });
  const rows: string[][] = [];
  let j = i + 2;
  while (j < lines.length && !isBlank(lines[j]) && lines[j].includes("|")) {
    const cells = splitRow(lines[j++]);
    rows.push(header.map((_, k) => cells[k] ?? ""));
  }
  return { block: { type: "table", align: header.map((_, k) => align[k] ?? null), header, rows }, next: j };
}

function parseQuote(lines: string[], i: number): Parsed {
  if (!QUOTE.test(lines[i])) return null;
  const body: string[] = [];
  let j = i;
  for (let m = lines[j]?.match(QUOTE); m; m = lines[++j]?.match(QUOTE)) body.push(m[1]);
  return { block: { type: "blockquote", children: parseBlocks(body.join("\n")) }, next: j };
}

function newItem(text: string): ListItem {
  const task = text.match(/^\[([ xX])\]\s+(.*)$/);
  return task
    ? { content: task[2], checked: task[1] !== " ", children: [] }
    : { content: text, checked: null, children: [] };
}

/** One list at ``base`` indentation: same-kind markers at that indent start items, deeper-indented
 *  lines (after a blank too) belong to the current item and are parsed as its nested blocks, and a
 *  plain line right after an item is a lazy continuation of its text. */
function parseList(lines: string[], i: number): Parsed {
  const first = lines[i].match(LIST_ITEM);
  if (!first || HR.test(lines[i])) return null;
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items: ListItem[] = [];
  const nested: string[][] = [];
  let j = i;
  while (j < lines.length) {
    const line = lines[j];
    const m = line.match(LIST_ITEM);
    if (m && m[1].length === base && /\d/.test(m[2]) === ordered && !HR.test(line)) {
      items.push(newItem(m[3]));
      nested.push([]);
      j++;
    } else if (!isBlank(line) && nested[nested.length - 1].length === 0 && !startsBlock(lines, j)) {
      items[items.length - 1].content += `\n${line.trim()}`;  // paragraph continuation, indented or lazy
      j++;
    } else if (!isBlank(line) && indentOf(line) > base) {
      nested[nested.length - 1].push(line);
      j++;
    } else if (isBlank(line) && !isBlank(lines[j + 1]) && indentOf(lines[j + 1]) > base) {
      nested[nested.length - 1].push("");
      j++;
    } else break;
  }
  items.forEach((item, k) => {
    const body = nested[k];
    if (!body.length) return;
    const cut = Math.min(...body.filter((l) => l.trim()).map(indentOf));
    item.children = parseBlocks(body.map((l) => l.slice(cut)).join("\n"));
  });
  const start = ordered ? parseInt(first[2], 10) : 1;
  return { block: { type: "list", ordered, start, items }, next: j };
}

const BLOCK_RULES: BlockRule[] = [parseFence, parseHeading, parseHr, parseTable, parseQuote, parseList];

function startsBlock(lines: string[], i: number): boolean {
  const line = lines[i];
  return FENCE.test(line) || HEADING.test(line) || HR.test(line) || QUOTE.test(line)
    || LIST_ITEM.test(line) || isTableStart(lines, i);
}

export function parseBlocks(text: string): BlockNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: BlockNode[] = [];
  let i = 0;
  while (i < lines.length) {
    if (isBlank(lines[i])) {
      i++;
      continue;
    }
    const parsed = BLOCK_RULES.reduce<Parsed>((found, rule) => found ?? rule(lines, i), null);
    if (parsed) {
      blocks.push(parsed.block);
      i = parsed.next;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && !isBlank(lines[i]) && (para.length === 0 || !startsBlock(lines, i))) {
      para.push(lines[i++]);
    }
    blocks.push({ type: "paragraph", content: para.join("\n") });
  }
  return blocks;
}

/* ------------------------------------------------------------------ */
/*  Inline                                                             */
/* ------------------------------------------------------------------ */

// Priority: escape > code > link > bold > strike > italic > bare URL > line break. Emphasis needs a
// non-space after the opener and before the closer (CommonMark flanking), so a lone ``*`` in prose
// or code-ish text (``SELECT *``, ``COUNT(*)``, ``a * b``) is literal.
const INLINE = new RegExp(
  [
    /\\([\\`*_{}[\]()#+\-.!~|>])/.source,
    /`([^`\n]+)`/.source,
    /\[([^\]\n]+)\]\(([^)\s]+)\)/.source,
    /\*\*(?=[^\s*])([^\n]*?[^\s*])\*\*/.source,
    /~~(?=\S)([^\n]*?\S)~~/.source,
    /(?<!\*)\*(?=[^\s*])([^*\n]*?[^\s*\\])\*(?!\*)/.source,
    /(?<!\*)\*([^\s*])\*(?!\*)/.source,
    /\bhttps?:\/\/[^\s<>)\]]+/.source,
    /\n/.source,
  ].map((source) => `(${source})`).join("|"),
  "g",
);

export function parseInline(text: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  const push = (node: InlineNode) => {
    const last = nodes[nodes.length - 1];
    if (node.type === "text" && last?.type === "text") last.content += node.content;
    else nodes.push(node);
  };
  let lastIndex = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > lastIndex) push({ type: "text", content: text.slice(lastIndex, at) });
    if (m[1]) push({ type: "text", content: m[2] });
    else if (m[3]) push({ type: "code", content: m[4] });
    else if (m[5]) push({ type: "link", text: m[6], href: m[7] });
    else if (m[8]) push({ type: "bold", children: parseInline(m[9]) });
    else if (m[10]) push({ type: "strike", children: parseInline(m[11]) });
    else if (m[12]) push({ type: "italic", children: parseInline(m[13]) });
    else if (m[14]) push({ type: "italic", children: parseInline(m[15]) });
    else if (m[16]) push({ type: "link", text: m[16], href: m[16] });
    else push({ type: "br" });
    lastIndex = at + m[0].length;
  }
  if (lastIndex < text.length) push({ type: "text", content: text.slice(lastIndex) });
  return nodes;
}
