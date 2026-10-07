/**
 * Memory files separate entries with a line holding only `§` (tools/memory_tool_store.py
 * ENTRY_DELIMITER). In the preview each separator becomes a Markdown rule instead of a stray glyph.
 */
export function memoryPreviewMarkdown(raw: string): string {
  return raw
    .split('\n')
    .map(line => (line.trim() === '§' ? '\n---\n' : line))
    .join('\n')
}
