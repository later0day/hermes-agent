import { useEffect, type RefObject } from "react";
import { memoryEntryOffsets } from "@/lib/memory-markdown";
import { syncScrollTop } from "@/lib/scroll-sync";

const MIRRORED = [
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "fontFamily", "fontSize", "fontWeight",
  "fontStyle", "letterSpacing", "lineHeight", "textTransform", "wordSpacing", "tabSize", "textIndent",
] as const;

/** Scroll offset at which each memory entry starts inside the textarea. Wrapped lines make line counting
 *  useless, so the text is laid out in an off-screen mirror with the textarea's font, padding and content
 *  width, with a marker before each entry. */
function editorAnchors(area: HTMLTextAreaElement, text: string): number[] {
  const style = getComputedStyle(area);
  const mirror = document.createElement("div");
  for (const prop of MIRRORED) mirror.style[prop] = style[prop];
  const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  Object.assign(mirror.style, {
    position: "absolute", visibility: "hidden", top: "0", left: "-100000px", boxSizing: "content-box",
    width: `${area.clientWidth - padX}px`, whiteSpace: "pre-wrap", overflowWrap: "break-word",
  });
  const markers: HTMLSpanElement[] = [];
  let last = 0;
  for (const offset of memoryEntryOffsets(text)) {
    mirror.append(text.slice(last, offset));
    markers.push(mirror.appendChild(document.createElement("span")));
    last = offset;
  }
  mirror.append(text.slice(last));
  document.body.append(mirror);
  const padTop = parseFloat(style.paddingTop);
  const anchors = markers.map((marker) => marker.offsetTop - padTop);
  mirror.remove();
  return anchors;
}

/** Scroll offset at which each entry card starts inside the scrolling pane. Cards are tagged
 *  ``data-memory-entry``: a plain ``ol > li`` also matched list items inside an entry's own Markdown. */
function previewAnchors(pane: HTMLElement): number[] {
  const top = pane.getBoundingClientRect().top - pane.scrollTop;
  return [...pane.querySelectorAll("[data-memory-entry]")].map((card) => card.getBoundingClientRect().top - top);
}

const maxScroll = (el: HTMLElement) => el.scrollHeight - el.clientHeight;

/** Keep the memory editor and its preview on the same entry while either is scrolled (split view). */
export function useSplitScrollSync(
  editorRef: RefObject<HTMLTextAreaElement | null>,
  previewRef: RefObject<HTMLElement | null>,
  text: string,
  enabled: boolean,
) {
  useEffect(() => {
    const area = editorRef.current;
    const pane = previewRef.current;
    if (!enabled || !area || !pane) return;
    let anchors = { editor: editorAnchors(area, text), preview: previewAnchors(pane) };
    const measure = () => {
      anchors = { editor: editorAnchors(area, text), preview: previewAnchors(pane) };
    };
    // The pane we just scrolled programmatically: its own scroll event must not echo back.
    let echo: HTMLElement | null = null;
    const follow = (src: HTMLElement, dst: HTMLElement, from: number[], to: number[]) => {
      if (echo === src) {
        echo = null;
        return;
      }
      const top = syncScrollTop(from, to, src.scrollTop, maxScroll(src), maxScroll(dst));
      if (Math.abs(dst.scrollTop - top) > 1) {
        echo = dst;
        dst.scrollTop = top;
      }
    };
    const onEditor = () => follow(area, pane, anchors.editor, anchors.preview);
    const onPreview = () => follow(pane, area, anchors.preview, anchors.editor);
    area.addEventListener("scroll", onEditor, { passive: true });
    pane.addEventListener("scroll", onPreview, { passive: true });
    const resize = new ResizeObserver(measure);
    resize.observe(area);
    resize.observe(pane);
    if (pane.firstElementChild) resize.observe(pane.firstElementChild);
    return () => {
      area.removeEventListener("scroll", onEditor);
      pane.removeEventListener("scroll", onPreview);
      resize.disconnect();
    };
  }, [editorRef, previewRef, text, enabled]);
}
