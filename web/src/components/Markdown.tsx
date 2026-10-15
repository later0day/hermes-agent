import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { parseBlocks, parseInline, type BlockNode, type InlineNode } from "@/lib/markdown-parse";
import { mermaidThemeFor } from "@/lib/mermaid-theme";
import { useTheme } from "@/themes/context";

/**
 * Lightweight markdown renderer for LLM output.
 * Handles code blocks (+ mermaid), headings, lists (nested, task items), blockquotes, GFM tables,
 * rules, inline code, links, bold/italic/strikethrough — parsing lives in lib/markdown-parse.ts.
 *
 * `streaming` renders a blinking caret at the tail of the last block so it
 * appears to hug the final character instead of wrapping onto a new line
 * after a block element (paragraph/list/code/…).
 */
export function Markdown({
  content,
  highlightTerms,
  streaming,
}: {
  content: string;
  highlightTerms?: string[];
  streaming?: boolean;
}) {
  const blocks = useMemo(() => parseBlocks(content), [content]);
  const caret = streaming ? <StreamingCaret /> : null;

  return (
    <div className="text-sm text-foreground leading-relaxed space-y-2">
      {blocks.map((block, i) => (
        <Block
          key={i}
          block={block}
          highlightTerms={highlightTerms}
          caret={caret && i === blocks.length - 1 ? caret : null}
        />
      ))}
      {blocks.length === 0 && caret}
    </div>
  );
}

function StreamingCaret() {
  return (
    <span
      aria-hidden
      className="inline-block w-[0.5em] h-[1em] ml-0.5 align-[-0.15em] bg-foreground/50 animate-pulse"
    />
  );
}

/* ------------------------------------------------------------------ */
/*  Mermaid diagrams                                                   */
/* ------------------------------------------------------------------ */

type MermaidApi = typeof import("mermaid")["default"];

let mermaidPromise: Promise<MermaidApi> | null = null;

/**
 * Load + initialize mermaid on first use only. Mermaid is a large dependency
 * (~900 KB gzip), so it is dynamically imported the first time a diagram
 * actually needs to render instead of being pulled into the main chat bundle.
 */
function loadMermaid(): Promise<MermaidApi> {
  if (!mermaidPromise) {
    mermaidPromise = import("mermaid").then(({ default: mermaid }) => mermaid);
  }
  return mermaidPromise;
}

let mermaidSeq = 0;

/**
 * Renders a ```mermaid fenced block as an SVG diagram. While a message is
 * still streaming the fence body is often syntactically incomplete, so a
 * failed render silently falls back to showing the raw source in a <pre>
 * rather than throwing / flashing an error box.
 */
function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const diagramTheme = mermaidThemeFor(useTheme().theme.palette.background.hex);

  useEffect(() => {
    let cancelled = false;
    const el = ref.current;
    if (!el) return;
    const source = code.trim();
    if (!source) return;

    const id = `mermaid-${mermaidSeq++}`;
    loadMermaid()
      .then((mermaid) => {
        // initialize() is global; set the theme for this render so a theme switch redraws in it.
        mermaid.initialize({ startOnLoad: false, theme: diagramTheme, securityLevel: "strict" });
        return mermaid.render(id, source);
      })
      .then(({ svg }) => {
        if (cancelled || !ref.current) return;
        ref.current.innerHTML = svg;
        setFailed(false);
      })
      .catch(() => {
        if (cancelled) return;
        setFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [code, diagramTheme]);

  if (failed) {
    return (
      <pre className="bg-secondary/60 border border-border px-3 py-2.5 text-xs font-mono leading-relaxed overflow-x-auto">
        <code>{code}</code>
      </pre>
    );
  }

  return (
    <div
      ref={ref}
      className="mermaid-diagram flex justify-center overflow-x-auto py-2"
    />
  );
}

/* ------------------------------------------------------------------ */
/*  Block renderer                                                     */
/* ------------------------------------------------------------------ */

const HEADING_SIZES = ["text-base font-bold", "text-sm font-bold", "text-sm font-semibold", "text-sm font-medium",
  "text-sm font-medium text-muted-foreground", "text-xs font-medium uppercase tracking-wide text-muted-foreground"];
const ALIGN_CLASS = { left: "text-left", center: "text-center", right: "text-right" } as const;

function Blocks({ blocks, highlightTerms }: { blocks: BlockNode[]; highlightTerms?: string[] }) {
  return (
    <div className="space-y-2">
      {blocks.map((block, i) => <Block key={i} block={block} highlightTerms={highlightTerms} />)}
    </div>
  );
}

function ListBlock({ block, highlightTerms, caret }: {
  block: Extract<BlockNode, { type: "list" }>; highlightTerms?: string[]; caret?: ReactNode;
}) {
  const Tag = block.ordered ? "ol" : "ul";
  const last = block.items.length - 1;
  const tasks = block.items.some((item) => item.checked !== null);
  return (
    <Tag start={block.ordered && block.start !== 1 ? block.start : undefined}
      className={`space-y-0.5 pl-5 text-sm ${block.ordered ? "list-decimal" : tasks ? "list-none -ml-5" : "list-disc"}`}>
      {block.items.map((item, i) => (
        <li key={i}>
          {item.checked !== null && (
            <span aria-hidden className={`mr-1.5 inline-block w-4 text-center ${item.checked ? "text-success" : "text-muted-foreground"}`}>
              {item.checked ? "☑" : "☐"}
            </span>
          )}
          <span className={item.checked ? "text-muted-foreground line-through" : undefined}>
            <InlineContent text={item.content} highlightTerms={highlightTerms} />
          </span>
          {item.children.length > 0 && (
            <div className="mt-1"><Blocks blocks={item.children} highlightTerms={highlightTerms} /></div>
          )}
          {i === last ? caret : null}
        </li>
      ))}
    </Tag>
  );
}

function TableBlock({ block, highlightTerms }: {
  block: Extract<BlockNode, { type: "table" }>; highlightTerms?: string[];
}) {
  const cell = (text: string, k: number) => (
    <InlineContent key={k} text={text} highlightTerms={highlightTerms} />
  );
  const alignOf = (k: number) => (block.align[k] ? ALIGN_CLASS[block.align[k]!] : "text-left");
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-border">
            {block.header.map((h, k) => (
              <th key={k} className={`px-2 py-1.5 font-semibold ${alignOf(k)}`}>{cell(h, k)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, r) => (
            <tr key={r} className="border-b border-border/50 last:border-0">
              {row.map((c, k) => <td key={k} className={`px-2 py-1.5 align-top ${alignOf(k)}`}>{cell(c, k)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Block({
  block,
  highlightTerms,
  caret,
}: {
  block: BlockNode;
  highlightTerms?: string[];
  caret?: ReactNode;
}) {
  switch (block.type) {
    case "code":
      if (block.lang === "mermaid") {
        return <MermaidBlock code={block.content} />;
      }
      return (
        <pre className="bg-secondary/60 border border-border px-3 py-2.5 text-xs font-mono leading-relaxed overflow-x-auto">
          <code>
            {block.content}
            {caret}
          </code>
        </pre>
      );

    case "heading": {
      const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      return (
        <Tag className={HEADING_SIZES[block.level - 1]}>
          <InlineContent text={block.content} highlightTerms={highlightTerms} />
          {caret}
        </Tag>
      );
    }

    case "hr":
      return (
        <>
          <hr className="border-border" />
          {caret}
        </>
      );

    case "list":
      return <ListBlock block={block} highlightTerms={highlightTerms} caret={caret} />;

    case "blockquote":
      return (
        <blockquote className="border-l-2 border-primary/40 pl-3 text-muted-foreground">
          <Blocks blocks={block.children} highlightTerms={highlightTerms} />
          {caret}
        </blockquote>
      );

    case "table":
      return (
        <>
          <TableBlock block={block} highlightTerms={highlightTerms} />
          {caret}
        </>
      );

    case "paragraph":
      return (
        <p>
          <InlineContent text={block.content} highlightTerms={highlightTerms} />
          {caret}
        </p>
      );
  }
}

/* ------------------------------------------------------------------ */
/*  Inline renderer                                                    */
/* ------------------------------------------------------------------ */

const EMPHASIS_TAGS = { bold: "strong", italic: "em", strike: "del" } as const;
const EMPHASIS_CLASS = { bold: "font-semibold", italic: undefined, strike: "text-muted-foreground" } as const;

function InlineNodes({ nodes, highlightTerms }: { nodes: InlineNode[]; highlightTerms?: string[] }) {
  return (
    <>
      {nodes.map((node, i) => {
        switch (node.type) {
          case "text":
            return (
              <HighlightedText
                key={i}
                text={node.content}
                terms={highlightTerms}
              />
            );
          case "code":
            return (
              <code
                key={i}
                className="bg-secondary/60 px-1.5 py-0.5 text-xs font-mono text-primary/90"
              >
                {node.content}
              </code>
            );
          case "bold":
          case "italic":
          case "strike": {
            const Tag = EMPHASIS_TAGS[node.type];
            return (
              <Tag key={i} className={EMPHASIS_CLASS[node.type]}>
                <InlineNodes nodes={node.children} highlightTerms={highlightTerms} />
              </Tag>
            );
          }
          case "link": {
            // Security: only render http(s)/mailto links. Other schemes
            // (javascript:, data:, vbscript:) are dropped to plain text so a
            // crafted link in agent/message content can't execute on click.
            const href = node.href.trim();
            if (!/^(https?:|mailto:)/i.test(href)) {
              return (
                <HighlightedText
                  key={i}
                  text={node.text}
                  terms={highlightTerms}
                />
              );
            }
            return (
              <a
                key={i}
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline underline-offset-2 decoration-primary/30 hover:decoration-primary/60 transition-colors"
              >
                {node.text}
              </a>
            );
          }
          case "br":
            return <br key={i} />;
        }
      })}
    </>
  );
}

function InlineContent({
  text,
  highlightTerms,
}: {
  text: string;
  highlightTerms?: string[];
}) {
  const nodes = useMemo(() => parseInline(text), [text]);
  return <InlineNodes nodes={nodes} highlightTerms={highlightTerms} />;
}

/** Highlight search terms within a plain text string. */
function HighlightedText({ text, terms }: { text: string; terms?: string[] }) {
  if (!terms || terms.length === 0) return <>{text}</>;

  // Build a regex that matches any of the search terms (case-insensitive)
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const regex = new RegExp(`(${escaped.join("|")})`, "gi");
  const parts = text.split(regex);

  return (
    <>
      {parts.map((part, i) =>
        regex.test(part) ? (
          <mark key={i} className="bg-warning/30 text-warning px-0.5">
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}
