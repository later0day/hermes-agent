import { prefersZh } from "./doc-locale";
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

const RELATIVE_COPY = {
  en: { now: "just now", m: "{n}m ago", h: "{n}h ago", yesterday: "yesterday", d: "{n}d ago", unknown: "unknown" },
  zh: { now: "刚刚", m: "{n} 分钟前", h: "{n} 小时前", yesterday: "昨天", d: "{n} 天前", unknown: "未知" },
};

function relative(delta: number, withYesterday: boolean): string {
  const c = RELATIVE_COPY[prefersZh() ? "zh" : "en"];
  const n = (unit: number) => String(Math.floor(delta / unit));
  if (delta < 60) return c.now;
  if (delta < 3600) return c.m.replace("{n}", n(60));
  if (delta < 86400) return c.h.replace("{n}", n(3600));
  if (withYesterday && delta < 172800) return c.yesterday;
  return c.d.replace("{n}", n(86400));
}

/** Format a delta with the caller's BCP-47 locale (negative = past). */
function intlRelative(deltaSeconds: number, locale: string): string {
  const abs = Math.abs(deltaSeconds);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (abs < 60) return rtf.format(Math.round(deltaSeconds), "second");
  if (abs < 3600) return rtf.format(Math.round(deltaSeconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(deltaSeconds / 3600), "hour");
  return rtf.format(Math.round(deltaSeconds / 86400), "day");
}

/** Relative time from Unix epoch seconds. Explicit locales support every dashboard language;
 *  omitted locales retain the compact en/zh copy used by session lists. */
export function timeAgo(ts: number, locale?: string): string {
  const delta = Date.now() / 1000 - ts;
  return locale === undefined ? relative(delta, true) : intlRelative(-delta, locale);
}

/** Relative time from an ISO-8601 timestamp, with the same locale contract as timeAgo. */
export function isoTimeAgo(iso: string, locale?: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) {
    return locale === undefined
      ? RELATIVE_COPY[prefersZh() ? "zh" : "en"].unknown
      : "—";
  }
  return locale === undefined ? relative(delta, false) : intlRelative(-delta, locale);
}
