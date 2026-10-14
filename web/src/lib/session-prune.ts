import { en } from '@/i18n/en'
import { localeSection } from '@/i18n/section'

interface SessionPruneLabels {
  prunedOne: string
  prunedMany: string
  pruneSkippedOne: string
  pruneSkippedMany: string
}

interface SessionPruneResult {
  removed: number
  skipped_open: number
}

export function formatSessionPruneResult(
  result: SessionPruneResult,
  labels: SessionPruneLabels = localeSection(en, 'sessionsPageX'),
): string {
  const removed = (result.removed === 1 ? labels.prunedOne : labels.prunedMany).replace(
    '{count}',
    String(result.removed),
  )
  if (!result.skipped_open) return removed

  return (result.skipped_open === 1 ? labels.pruneSkippedOne : labels.pruneSkippedMany)
    .replace('{removed}', removed)
    .replace('{count}', String(result.skipped_open))
}
