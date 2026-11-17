/** Whether the dashboard renders Simplified Chinese (I18nProvider writes the locale to ``<html lang>``).
 *  For pure helpers called from many places without ``t`` (relative times, gateway state copy). */
export function prefersZh(): boolean {
  return typeof document !== "undefined" && document.documentElement.lang === "zh";
}
