import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface MemoryDocTab {
  id: string;
  title: string;
  icon: LucideIcon;
  /** used / limit, or null while loading */
  ratio: number | null;
  dirty: boolean;
}

interface MemoryDocTabsProps {
  tabs: MemoryDocTab[];
  active: string;
  onSelect: (id: string) => void;
  label: string;
  unsavedLabel: string;
}

const tone = (ratio: number) => (ratio > 1 ? "text-destructive" : ratio >= 0.9 ? "text-warning" : "text-muted-foreground");

/** MEMORY.md / USER.md as tabs (one document at a time, full width), each showing how full it is and
 *  whether it holds unsaved edits — the inactive document stays mounted so neither is lost. */
export function MemoryDocTabs({ tabs, active, onSelect, label, unsavedLabel }: MemoryDocTabsProps) {
  return (
    <div role="tablist" aria-label={label} className="flex border-b border-border">
      {tabs.map(({ id, title, icon: Icon, ratio, dirty }) => (
        <button
          key={id}
          id={`memory-tab-${id}`}
          type="button"
          role="tab"
          aria-selected={active === id}
          aria-controls={`memory-panel-${id}`}
          onClick={() => onSelect(id)}
          className={cn(
            "-mb-px flex items-center gap-2 border-b-2 px-4 py-2 text-sm transition-colors",
            active === id ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          <Icon className="h-4 w-4" />
          <span className="font-mono">{title}</span>
          {ratio !== null && <span className={cn("text-xs tabular-nums", tone(ratio))}>{Math.round(ratio * 100)}%</span>}
          {dirty && <span title={unsavedLabel} aria-label={unsavedLabel} className="h-1.5 w-1.5 rounded-full bg-warning" />}
        </button>
      ))}
    </div>
  );
}
