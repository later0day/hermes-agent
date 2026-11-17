/** Mermaid's built-in theme that reads on a dashboard background: `dark` on dark
 *  grounds, `default` on light ones (Pure Ink Light, user themes). */
export function mermaidThemeFor(backgroundHex: string): "dark" | "default" {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(backgroundHex.trim());
  if (!m) return "dark";
  const hex = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "default" : "dark";
}
