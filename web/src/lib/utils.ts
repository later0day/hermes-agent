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

/** Relative time from a Unix epoch timestamp (seconds), in the dashboard's language (en/zh). */
export function timeAgo(ts: number): string {
  return relative(Date.now() / 1000 - ts, true);
}

/** Relative time from an ISO-8601 timestamp string, in the dashboard's language (en/zh). */
export function isoTimeAgo(iso: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) return RELATIVE_COPY[prefersZh() ? "zh" : "en"].unknown;
  return relative(delta, false);
}
