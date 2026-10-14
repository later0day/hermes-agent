import { en } from "./en";
import type { Translations } from "./types";

/** Top-level namespaces a locale may omit (declared `name?:` in types.ts). */
export type OptionalSection = {
  [K in keyof Translations]-?: undefined extends Translations[K] ? K : never;
}[keyof Translations];

/** A page's optional string namespace with English filling every key the active locale lacks, so pages
 *  read `s.key` without per-call `?? "English"` fallbacks. Namespaces are flat string maps. */
export function localeSection<K extends OptionalSection>(t: Translations, key: K): NonNullable<Translations[K]> {
  return { ...(en[key] as object), ...((t[key] as object | undefined) ?? {}) } as NonNullable<Translations[K]>;
}
