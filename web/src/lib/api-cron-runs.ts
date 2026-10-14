import { fetchJSON } from "@/lib/api";

/** A platform a cron job can deliver to (GET /api/cron/delivery-targets). */
export interface CronDeliveryTarget {
  id: string;
  name: string;
  home_target_set: boolean;
  home_env_var: string | null;
}

/** One fire of a cron job: an agent session row, or a script-only output doc (fields optional). */
export interface CronJobRun {
  id: string;
  started_at?: number | null;
  ended_at?: number | null;
  end_reason?: string | null;
  model?: string | null;
  tool_call_count?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  is_active?: boolean;
  title?: string | null;
}

/** Run history of one cron job (GET /api/cron/jobs/{id}/runs), read from the job's owner profile. */
export const cronRunsApi = {
  list: (id: string, profile = "default", limit = 20) =>
    fetchJSON<{ runs: CronJobRun[]; limit: number }>(
      `/api/cron/jobs/${encodeURIComponent(id)}/runs?profile=${encodeURIComponent(profile)}&limit=${limit}`,
    ),
};
