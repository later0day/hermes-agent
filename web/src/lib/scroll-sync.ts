/**
 * Anchor-based scroll sync between two panes showing the same items at different heights (the memory
 * editor's wrapped text vs. its preview cards). ``anchors[i]`` is the scroll offset at which item ``i``
 * starts in a pane; between anchors (and after the last, up to the pane's max scroll) the position is
 * interpolated, so both panes keep the same item — and the same fraction of it — at the top. A plain
 * percentage sync drifts because the panes' item heights differ.
 */
export function syncScrollTop(srcAnchors: number[], dstAnchors: number[], srcTop: number, srcMax: number,
  dstMax: number): number {
  const n = Math.min(srcAnchors.length, dstAnchors.length);
  const src = [0, ...srcAnchors.slice(0, n).map((a) => Math.min(a, srcMax)), Math.max(srcMax, 0)];
  const dst = [0, ...dstAnchors.slice(0, n).map((a) => Math.min(a, dstMax)), Math.max(dstMax, 0)];
  const top = Math.max(0, Math.min(srcTop, srcMax));
  for (let k = 0; k < src.length - 1; k++) {
    if (top <= src[k + 1]) {
      const span = src[k + 1] - src[k];
      const frac = span > 0 ? (top - src[k]) / span : 0;
      return Math.round(dst[k] + frac * (dst[k + 1] - dst[k]));
    }
  }
  return Math.round(dstMax);
}
