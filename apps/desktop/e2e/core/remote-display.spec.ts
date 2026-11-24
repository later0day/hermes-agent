import { expect, test } from '@playwright/test'

import {
  coreAppEnv,
  createCoreSandbox,
  launchCoreApp,
  sandboxProcesses,
  waitForInteractive,
  writeProviderHome
} from './harness'
import { startScriptedProvider } from './provider'

interface DisplayCase {
  name: string
  sshVariable?: 'SSH_CONNECTION' | 'SSH_CLIENT' | 'SSH_TTY'
  gpuOverride?: '0' | '1'
  reason: string | null
  compositingDisabled: boolean
  gpuDisabled: boolean
}

const cases: DisplayCase[] = [
  {
    name: 'local display keeps GPU compositing',
    reason: null,
    compositingDisabled: false,
    gpuDisabled: false
  },
  ...(['SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'] as const).map(sshVariable => ({
    name: `${sshVariable} selects software compositing`,
    sshVariable,
    reason: 'ssh-session',
    compositingDisabled: true,
    gpuDisabled: true
  })),
  {
    name: 'explicit GPU override off wins over inherited SSH',
    sshVariable: 'SSH_CONNECTION',
    gpuOverride: '0',
    reason: null,
    compositingDisabled: false,
    gpuDisabled: false
  },
  {
    name: 'explicit GPU override on fully disables GPU',
    gpuOverride: '1',
    reason: 'override (HERMES_DESKTOP_DISABLE_GPU)',
    compositingDisabled: true,
    gpuDisabled: true
  }
]

for (const displayCase of cases) {
  test(displayCase.name, async () => {
    test.skip(process.platform !== 'linux', 'Linux display classification')
    const provider = await startScriptedProvider()
    const sandbox = createCoreSandbox(`remote-display-${displayCase.name.replace(/[^a-z0-9]+/gi, '-')}`)
    writeProviderHome(sandbox.hermesHome, provider.url)
    const env = coreAppEnv(sandbox)
    delete env.SSH_CONNECTION
    delete env.SSH_CLIENT
    delete env.SSH_TTY

    if (displayCase.sshVariable) {env[displayCase.sshVariable] = 'e2e-ssh-marker'}

    if (displayCase.gpuOverride) {env.HERMES_DESKTOP_DISABLE_GPU = displayCase.gpuOverride}
    let live: Awaited<ReturnType<typeof launchCoreApp>> | null = null

    try {
      live = await launchCoreApp(env, { forceSoftwareRendering: false })
      await waitForInteractive(live.app, live.page)

      const reason = await live.page.evaluate(() => {
        const bridge = (window as unknown as {
          hermesDesktop?: { getRemoteDisplayReason?: () => string | null }
        }).hermesDesktop

        return bridge?.getRemoteDisplayReason?.()
      })

      const switches = await live.app.evaluate(({ app }) => ({
        compositing: app.commandLine.hasSwitch('disable-gpu-compositing'),
        gpu: app.commandLine.hasSwitch('disable-gpu')
      }))

      expect(reason).toBe(displayCase.reason)
      expect(switches).toEqual({
        compositing: displayCase.compositingDisabled,
        gpu: displayCase.gpuDisabled
      })
    } finally {
      if (live) {await live.app.close().catch(() => undefined)}

      for (const proc of sandboxProcesses(sandbox)) {
        try {
          process.kill(proc.pid, 'SIGKILL')
        } catch {
          // already gone
        }
      }

      await provider.close()
      sandbox.cleanup()
    }
  })
}
