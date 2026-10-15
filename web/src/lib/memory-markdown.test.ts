import { describe, expect, it } from 'vitest'

import { memoryCharCount, splitMemoryEntries } from './memory-markdown'

describe('memory entries and usage', () => {
  it('splits like MemoryStore._parse_entries and counts like the tool (code points, delimiter included)', () => {
    const raw = '  first entry \n§\n\n§\nsecond § entry 👍\n'
    expect(splitMemoryEntries(raw)).toEqual(['first entry', 'second § entry 👍'])
    // "first entry" (11) + "\n§\n" (3) + "second § entry 👍" (16 code points; the emoji is one)
    expect(memoryCharCount(raw)).toBe(30)
    expect(memoryCharCount('')).toBe(0)
  })
})

describe('memoryEntryOffsets', () => {
  it('points at the first character of every non-empty entry', async () => {
    const { memoryEntryOffsets, splitMemoryEntries } = await import('./memory-markdown')
    const raw = '  first entry \n§\n\n§\nsecond § entry\n§\n  third'
    const offsets = memoryEntryOffsets(raw)
    expect(offsets.map((o) => raw.slice(o, o + 5))).toEqual(['first', 'secon', 'third'])
    expect(offsets).toHaveLength(splitMemoryEntries(raw).length)
  })
})
