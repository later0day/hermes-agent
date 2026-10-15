import { Columns2, Eye, Pencil } from "lucide-react";
import { cn } from "@/lib/utils";

export type MemoryView = "edit" | "preview" | "split";

interface MemoryViewToggleProps {
  value: MemoryView;
  onChange: (view: MemoryView) => void;
  disabled?: boolean;
  copy: { viewEdit: string; viewPreview: string; viewSplit: string; viewLabel: string };
}

const VIEWS = [
  { view: "edit", icon: Pencil, label: "viewEdit" },
  { view: "preview", icon: Eye, label: "viewPreview" },
  { view: "split", icon: Columns2, label: "viewSplit" },
] as const;

/** Segmented edit / preview / split switch. Plain buttons: the UI kit's xs Button squares icon + text
 *  into an icon-only cell, which stacked the eye glyph on top of "预览". */
export function MemoryViewToggle({ value, onChange, disabled, copy }: MemoryViewToggleProps) {
  return (
    <div role="group" aria-label={copy.viewLabel} className="inline-flex border border-border">
      {VIEWS.map(({ view, icon: Icon, label }) => (
        <button
          key={view}
          type="button"
          aria-pressed={value === view}
          disabled={disabled}
          onClick={() => onChange(view)}
          className={cn(
            "flex items-center gap-1 px-2 py-1 text-xs transition-colors disabled:opacity-50",
            "[&:not(:first-child)]:border-l [&:not(:first-child)]:border-border",
            value === view ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground",
            view === "split" && "hidden lg:flex",
          )}
        >
          <Icon className="h-3 w-3" />
          {copy[label]}
        </button>
      ))}
    </div>
  );
}
