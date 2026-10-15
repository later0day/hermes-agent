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
