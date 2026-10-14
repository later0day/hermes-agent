import type { CronJob } from "@/lib/api";
import { cronDeliveryTarget, cronOriginLabel } from "@/lib/cron-job";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";

/** Where a cron job's report goes, and the chat it came from when that differs (cron job card row). */
export function CronJobDeliveryInfo({ job }: { job: CronJob }) {
  const { t } = useI18n();
  const s = localeSection(t, "cronPageX");
  const target = cronDeliveryTarget(job);
  const origin = cronOriginLabel(job);
  return (
    <>
      <span title={target ?? undefined}>
        {s.deliversTo}: {target ?? s.localOnly}
      </span>
      {origin && <span>{s.createdIn}: {origin}</span>}
    </>
  );
}
