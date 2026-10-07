// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionSearchModal } from './SessionSearchModal'

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const { searchSessions, getSessions } = vi.hoisted(() => ({ searchSessions: vi.fn(), getSessions: vi.fn() }))

vi.mock('@/lib/api', () => ({ api: { searchSessions, getSessions } }))
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { sessions: { noMatch: 'No match' } } }) }))

// Reads the resumed session exactly the way ChatPage does.
function ChatStub() {
  const [params] = useSearchParams()
  return <div data-testid="resumed">{params.get('resume')}</div>
}

describe('SessionSearchModal', () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    getSessions.mockResolvedValue({ sessions: [] })
    searchSessions.mockResolvedValue({
      results: [
        { session_id: 'a&b c', snippet: 'first hit', source: 'cli', session_started: 1 },
        { session_id: 'a&b c', snippet: 'second hit', source: 'cli', session_started: 1 }
      ]
    })
  })

  afterEach(() => {
    act(() => root.unmount())
    host.remove()
  })

  it('lists each session once and opens the picked one in chat', async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<SessionSearchModal open onClose={() => {}} />} />
            <Route path="/chat" element={<ChatStub />} />
          </Routes>
        </MemoryRouter>
      )
    })
    const input = host.querySelector('input')!
    await act(async () => {
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
      setValue.call(input, 'hit')
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await new Promise(resolve => setTimeout(resolve, 300))
    })

    const rows = [...host.querySelectorAll('button')].filter(b => b.textContent?.includes('hit'))
    expect(rows).toHaveLength(1)

    await act(async () => rows[0].click())
    expect(host.querySelector('[data-testid="resumed"]')?.textContent).toBe('a&b c')
  })
})
