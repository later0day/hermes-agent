import { atom, Button, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const state = atom({ phase: 'idle', detail: 'No authorization requested' })
const launcherUrl = '__LAUNCHER_URL__'
const plugin = 'profile-backend-e2e'
const profile = 'work'

async function control(path) {
  const response = await fetch(`${launcherUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.error || `Launcher returned ${response.status}`)
  return body
}

async function run(action) {
  state.set({ phase: 'pending', detail: `${action} pending` })
  try {
    const result = action === 'set'
      ? await host.setProfileBackend({
          plugin,
          profile,
          prepare: () => control('/start'),
        })
      : await host.clearProfileBackend({
          plugin,
          profile,
          beforeApply: () => control('/stop'),
        })
    state.set({
      phase: result.applied ? 'applied' : 'cancelled',
      detail: `${action} ${result.applied ? 'applied' : 'cancelled'}`,
    })
  } catch (error) {
    state.set({ phase: 'error', detail: `${action} error: ${String(error)}` })
    throw error
  }
}

function ProfileBackendSettings() {
  const current = useValue(state)
  const pending = current.phase === 'pending'
  return jsxs('div', {
    children: [
      jsx('p', { 'data-testid': 'profile-backend-e2e-status', children: current.detail }),
      jsx(Button, {
        disabled: pending,
        onClick: () => { void run('set') },
        children: 'Set sandbox backend',
      }),
      jsx(Button, {
        disabled: pending,
        onClick: () => { void run('clear') },
        children: 'Clear sandbox backend',
      }),
    ],
  })
}

export default {
  id: plugin,
  name: 'Profile Backend E2E',
  register(ctx) {
    ctx.registerSettingsPage({
      id: 'profile-backend-e2e',
      title: 'Profile Backend E2E',
      render: ProfileBackendSettings,
    })
  },
}
