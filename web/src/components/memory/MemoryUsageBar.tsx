import { cn } from "@/lib/utils";

interface MemoryUsageBarProps {
  used: number;
  limit: number;
  entries: number;
  copy: { usage: string; usageNear: string; usageOver: string; entries: string };
}

const NEAR = 0.9;

/** How full a memory store is, measured the way the memory tool measures it: past the limit the agent's
 *  `memory add` is refused, which was invisible on this page before (xcx sat at 98% on 2026-10-15). */
export function MemoryUsageBar({ used, limit, entries, copy }: MemoryUsageBarProps) {
  const ratio = limit > 0 ? used / limit : 0;
  const level = ratio > 1 ? "over" : ratio >= NEAR ? "near" : "ok";
  const tone = { ok: "bg-primary/70", near: "bg-warning", over: "bg-destructive" }[level];
  const text = { ok: "text-muted-foreground", near: "text-warning", over: "text-destructive" }[level];
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className={cn("tabular-nums", text)}>
          {copy.usage.replace("{used}", String(used)).replace("{limit}", String(limit))}
          <span className="text-muted-foreground"> · {copy.entries.replace("{n}", String(entries))}</span>
        </span>
        <span className={cn("tabular-nums", text)}>{Math.round(ratio * 100)}%</span>
      </div>
      <div className="h-1 w-full bg-secondary" role="meter" aria-valuemin={0} aria-valuemax={limit} aria-valuenow={used}>
        <div className={cn("h-full transition-[width]", tone)} style={{ width: `${Math.min(ratio, 1) * 100}%` }} />
      </div>
      {level !== "ok" && <p className={cn("text-xs", text)}>{level === "over" ? copy.usageOver : copy.usageNear}</p>}
    </div>
  );
}
