/** Memory files separate entries with a line holding only `§` (tools/memory_tool_store.py
 *  ENTRY_DELIMITER = "\n§\n"). */
const ENTRY_DELIMITER = '\n§\n'

/** The entries the memory tool sees: split on the delimiter, stripped, empties dropped
 *  (MemoryStore._parse_entries). */
export function splitMemoryEntries(raw: string): string[] {
  return raw
    .replace(/\r\n?/g, '\n')
    .split(ENTRY_DELIMITER)
    .map(entry => entry.trim())
    .filter(Boolean)
}

/** Code points, like Python ``len`` — a UTF-16 ``.length`` would count an emoji twice. */
export function codePointLength(text: string): number {
  return Array.from(text).length
}

/** Characters the memory tool counts against the store's limit (entries re-joined by the delimiter),
 *  so the live meter matches what the agent will be told when it tries to add an entry. */
export function memoryCharCount(raw: string): number {
  return codePointLength(splitMemoryEntries(raw).join(ENTRY_DELIMITER))
}

/** Character offset in ``raw`` where each entry of ``splitMemoryEntries(raw)`` begins (after its
 *  leading whitespace), so an editor can locate entries in its own text. */
export function memoryEntryOffsets(raw: string): number[] {
  const text = raw.replace(/\r\n?/g, '\n')
  const offsets: number[] = []
  let start = 0
  for (const chunk of text.split(ENTRY_DELIMITER)) {
    const lead = chunk.length - chunk.trimStart().length
    if (chunk.trim()) offsets.push(start + lead)
    start += chunk.length + ENTRY_DELIMITER.length
  }
  return offsets
}
