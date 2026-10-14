import { useEffect, useMemo, useState } from "react";
import { X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { api, type CronJobRun } from "@/lib/api";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";
import { cn, themedBody } from "@/lib/utils";

interface CronRunHistoryDialogProps {
  jobId: string;
  jobTitle: string;
  profile: string;
  onClose: () => void;
}

type RunsState = { runs: CronJobRun[]; error: string | null; loaded: boolean };

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

/** Recent fires of one cron job (GET /api/cron/jobs/{id}/runs): when, how long, how it ended. */
export function CronRunHistoryDialog({ jobId, jobTitle, profile, onClose }: CronRunHistoryDialogProps) {
  const { t } = useI18n();
  const s = useMemo(() => localeSection(t, "cronRunHistory"), [t]);
  const modalRef = useModalBehavior({ open: true, onClose });
  const [state, setState] = useState<RunsState>({ runs: [], error: null, loaded: false });

  useEffect(() => {
    let cancelled = false;
    api
      .getCronJobRuns(jobId, profile)
      .then((res) => !cancelled && setState({ runs: res.runs, error: null, loaded: true }))
      .catch((e: unknown) => !cancelled && setState({ runs: [], error: String(e instanceof Error ? e.message : e), loaded: true }));
    return () => {
      cancelled = true;
    };
  }, [jobId, profile]);

  const outcome = (run: CronJobRun): string => {
    if (run.is_active) return s.running;
    if (!run.ended_at) return s.unknownOutcome;
    return run.end_reason === "cron_complete" ? s.completed : (run.end_reason ?? s.unknownOutcome);
  };

  return (
    <div
      ref={modalRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="cron-run-history-title"
    >
      <div className={cn(themedBody, "relative w-full max-w-3xl max-h-[90vh] border border-border bg-card shadow-2xl flex flex-col")}>
        <Button ghost size="icon" onClick={onClose} aria-label={t.common.close}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground">
          <X />
        </Button>
        <header className="p-5 pb-3 border-b border-border">
          <h2 id="cron-run-history-title" className="font-mondwest text-display text-base tracking-wider">
            {s.title}
          </h2>
          <p className="mt-1 text-xs text-muted-foreground break-words">{jobTitle}</p>
        </header>
        <div className="min-h-0 overflow-y-auto p-5">
          {!state.loaded && <p className="text-sm text-muted-foreground">{t.common.loading}</p>}
          {state.error && <p className="text-sm text-destructive">{s.loadFailed.replace("{error}", state.error)}</p>}
          {state.loaded && !state.error && state.runs.length === 0 && (
            <p className="text-sm text-muted-foreground">{s.empty}</p>
          )}
          {state.runs.length > 0 && (
            <table className="w-full text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3 font-normal">{s.startedAt}</th>
                  <th className="py-1 pr-3 font-normal">{s.duration}</th>
                  <th className="py-1 pr-3 font-normal">{s.outcome}</th>
                  <th className="py-1 pr-3 font-normal">{s.model}</th>
                  <th className="py-1 pr-3 font-normal">{s.toolCalls}</th>
                  <th className="py-1 font-normal">{s.tokens}</th>
                </tr>
              </thead>
              <tbody>
                {state.runs.map((run) => (
                  <tr key={run.id} className="border-t border-border align-top" title={run.id}>
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {run.started_at ? new Date(run.started_at * 1000).toLocaleString() : "—"}
                    </td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {run.started_at && run.ended_at ? formatDuration(run.ended_at - run.started_at) : "—"}
                    </td>
                    <td className={cn("py-1.5 pr-3", run.is_active ? "text-warning" : run.end_reason === "cron_complete" ? "text-success" : "")}>
                      {outcome(run)}
                    </td>
                    <td className="py-1.5 pr-3 break-all">{run.model ?? "—"}</td>
                    <td className="py-1.5 pr-3">{run.tool_call_count ?? "—"}</td>
                    <td className="py-1.5">
                      {run.input_tokens != null ? `${run.input_tokens} / ${run.output_tokens ?? 0}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
