import { describe, expect, it, vi } from 'vitest'

import { canUninstallSkill, waitForAction } from './skill-uninstall'

describe('canUninstallSkill', () => {
  it('offers uninstall for hub skills only', () => {
    expect(canUninstallSkill({ provenance: 'hub' })).toBe(true)
    expect(canUninstallSkill({ provenance: 'bundled' })).toBe(false)
    expect(canUninstallSkill({ provenance: 'agent' })).toBe(false)
  })
})

describe('waitForAction', () => {
  it('resolves once the action stops running', async () => {
    const statuses = [true, true, false].map(running => ({ exit_code: running ? null : 0, lines: [], name: 'a', pid: 1, running }))
    const getStatus = vi.fn(async () => statuses.shift()!)
    const done = await waitForAction(getStatus, 'a', { intervalMs: 0 })
    expect(done?.running).toBe(false)
    expect(getStatus).toHaveBeenCalledTimes(3)
  })
})
