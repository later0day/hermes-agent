import { Markdown } from "@/components/Markdown";
import { codePointLength } from "@/lib/memory-markdown";

interface MemoryEntryListProps {
  entries: string[];
  emptyLabel: string;
  charsLabel: string; // "{n} chars"
}

/** Preview: one card per memory entry (the unit the agent adds, replaces and removes), numbered and sized,
 *  instead of one long document with `§` turned into rules. */
export function MemoryEntryList({ entries, emptyLabel, charsLabel }: MemoryEntryListProps) {
  if (entries.length === 0) {
    return <p className="px-1 py-2 text-sm text-muted-foreground">{emptyLabel}</p>;
  }
  return (
    <ol className="space-y-2">
      {entries.map((entry, i) => (
        <li key={i} data-memory-entry className="border border-border bg-background/40">
          <div className="flex items-center justify-between border-b border-border/60 px-3 py-1 text-[11px] text-muted-foreground">
            <span className="font-mono">#{i + 1}</span>
            <span className="tabular-nums">{charsLabel.replace("{n}", String(codePointLength(entry)))}</span>
          </div>
          <div className="px-3 py-2">
            <Markdown content={entry} />
          </div>
        </li>
      ))}
    </ol>
  );
}
