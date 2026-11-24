import { expect, test } from '@playwright/test'

import {
  appLogTail,
  coreAppEnv,
  createCoreSandbox,
  currentSessionId,
  launchCoreApp,
  recordWebSockets,
  send,
  waitForInteractive,
  writeProviderHome,
  type WsRecorder
} from './harness'
import { startPrivateNotificationBus } from './presence-notifications-dbus'
import { startScriptedProvider } from './provider'

test.skip(process.platform !== 'linux', 'Linux page presence and native notifications')

function activities(ws: WsRecorder) {
  return ws.sent.filter(frame => frame.method === 'client.activity')
}

test('desktop identity and page activity follow the runtime session across routes', async () => {
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox('presence')
  writeProviderHome(sandbox.hermesHome, provider.url)
  const { app, page } = await launchCoreApp(coreAppEnv(sandbox))
  const ws = recordWebSockets(page)

  try {
    await waitForInteractive(app, page)
    await expect
      .poll(() => ws.sent.some(frame => frame.method === 'client.capabilities' && frame.params?.client))
      .toBe(true)
    const hello = ws.sent.find(frame => frame.method === 'client.capabilities' && frame.params?.client)
    expect(hello?.params.client).toMatchObject({ kind: 'desktop', name: 'hermes-desktop' })

    const marker = `U1-${Math.random().toString(36).slice(2, 8)}`
    provider.script(marker, [{ text: ['presence reply'] }])
    await send(page, `${marker} open a runtime session`, 'Enter', ws)
    await expect
      .poll(() => ws.sent.some(frame => frame.method === 'prompt.submit' && String(frame.params?.text).includes(marker)))
      .toBe(true)
    const submit = ws.sent.find(
      frame => frame.method === 'prompt.submit' && String(frame.params?.text).includes(marker)
    )
    const runtimeSessionId = submit?.params?.session_id
    expect(typeof runtimeSessionId).toBe('string')
    expect(runtimeSessionId).not.toBe('')
    expect(await currentSessionId(page)).not.toBe('')

    await expect
      .poll(() => activities(ws).some(frame => frame.params?.sessions?.[0] === runtimeSessionId))
      .toBe(true)
    const claim = activities(ws).find(frame => frame.params?.sessions?.[0] === runtimeSessionId)!
    expect(claim.params.sessions).toEqual([runtimeSessionId])
    expect(claim.params.basis).toBe('page')
    expect(Number.isFinite(claim.params.idle_seconds)).toBe(true)
    expect(claim.params.idle_seconds).toBeGreaterThanOrEqual(0)
    expect(claim.params.idle_seconds).toBeLessThan(60)
    expect(claim.params.can_alert).toBe(true)

    await page.getByRole('button', { name: 'Open settings', exact: true }).click()
    await expect.poll(() => activities(ws).some(frame => frame.params?.sessions?.length === 0)).toBe(true)
    const withdrawal = activities(ws).findLast(frame => frame.params?.sessions?.length === 0)!
    expect(withdrawal.params.sessions).toEqual([])
    expect(withdrawal.params.basis).toBe('page')

    const beforeReturn = activities(ws).length
    await page.getByRole('button', { name: 'Close settings', exact: true }).click()
    await expect.poll(() => activities(ws).length).toBeGreaterThan(beforeReturn)
    const reclaim = activities(ws).at(-1)!
    expect(reclaim.params.sessions).toEqual([runtimeSessionId])
    expect(reclaim.params.basis).toBe('page')
    expect(reclaim.params.can_alert).toBe(true)
    expect(activities(ws).flatMap(frame => frame.params?.sessions ?? [])).toEqual(
      activities(ws).flatMap(frame => frame.params?.sessions ?? []).map(() => runtimeSessionId)
    )
  } finally {
    await app.close().catch(() => undefined)
    await provider.close()
    sandbox.cleanup()
  }
})

test('native notification support tracks a private bus owner and Settings delivers Notify', async () => {
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox('notifications')
  writeProviderHome(sandbox.hermesHome, provider.url)
  const bus = await startPrivateNotificationBus(sandbox.root)
  const { app, page } = await launchCoreApp(coreAppEnv(sandbox, { DBUS_SESSION_BUS_ADDRESS: bus.address }))

  try {
    await waitForInteractive(app, page)
    const supported = () => page.evaluate(() => (window as any).hermesDesktop?.notifySupported?.() ?? false)
    expect(await supported()).toBe(false)

    await bus.startService()
    await expect.poll(supported).toBe(true)

    await page.evaluate(() => {
      window.location.hash = '#/settings?tab=notifications'
    })
    const testButton = page.getByRole('button', { name: 'Send test notification', exact: true })
    await expect(testButton).toBeVisible()
    await testButton.click()

    try {
      await expect.poll(() => bus.notifications().length, { timeout: 10_000 }).toBe(1)
    } catch (error) {
      throw new Error(`${String(error)}\nD-Bus service:\n${bus.debug()}\nElectron:\n${appLogTail(app)}`)
    }
    const [delivered] = bus.notifications()
    expect(delivered.id).toBe(1)
    expect(delivered.sender).toMatch(/^:\d+\.\d+$/)
    expect(delivered.body?.[0]).toBe('Hermes')
    expect(delivered.body?.[3]).toBe('Hermes')
    expect(delivered.body?.[4]).toBe('Notifications are working.')
    await expect(page.getByText(/^Test sent\./)).toBeVisible()
  } finally {
    await app.close().catch(() => undefined)
    await bus.close()
    await provider.close()
    sandbox.cleanup()
  }
})
