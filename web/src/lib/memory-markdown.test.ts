import { describe, expect, it } from 'vitest'

import { memoryPreviewMarkdown } from './memory-markdown'

describe('memoryPreviewMarkdown', () => {
  it('turns entry separators into rules and leaves other § alone', () => {
    const preview = memoryPreviewMarkdown('first entry\n§\nsecond § entry')
    expect(preview).not.toMatch(/^§$/m)
    expect(preview).toContain('---')
    expect(preview).toContain('second § entry')
  })
})
