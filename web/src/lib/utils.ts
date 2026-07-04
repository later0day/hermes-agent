import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Mondwest font only — use on layout shells; do not force normal-case here or `text-display` chrome (Segmented, badges) stops uppercasing. */
export const themedFont = "font-mondwest";

/** Mondwest body copy — sentence-case themed text (not uppercase chrome). */
export const themedBody = "font-mondwest normal-case";

/** Mondwest brand chrome — uppercase section headers and nav labels. */
export const themedChrome = "font-mondwest text-display";

/** Relative time from a Unix epoch timestamp (seconds).
 *  Pass a BCP-47 locale string (e.g. "zh", "zh-hant", "ja") to get
 *  a localised result via Intl.RelativeTimeFormat. Defaults to "en". */
export function timeAgo(ts: number, locale = "en"): string {
  const delta = Date.now() / 1000 - ts;
  return _relativeTime(-delta, locale);
}

/** Relative time from an ISO-8601 timestamp string.
 *  Pass a BCP-47 locale string to get a localised result. */
export function isoTimeAgo(iso: string, locale = "en"): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) return "—";
  return _relativeTime(-delta, locale);
}

/** Internal: format a relative-time delta (negative = past) using Intl. */
function _relativeTime(deltaSeconds: number, locale: string): string {
  const abs = Math.abs(deltaSeconds);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (abs < 60) return rtf.format(Math.round(deltaSeconds), "second");
  if (abs < 3600) return rtf.format(Math.round(deltaSeconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(deltaSeconds / 3600), "hour");
  return rtf.format(Math.round(deltaSeconds / 86400), "day");
}
