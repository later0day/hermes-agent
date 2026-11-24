import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { type ElectronApplication, expect, type Page } from '@playwright/test'

import {
  backendProcesses,
  coreAppEnv,
  createCoreSandbox,
  launchCoreApp,
  recordWebSockets,
  send,
  storedSessionForMarker,
  waitForInteractive,
  writeProviderHome,
} from './core/harness'
import { type ScriptedProvider, startScriptedProvider } from './core/provider'
import { type ProfileBackendLauncher, startProfileBackendLauncher } from './fixtures/profile-backend-authorization/launcher'
import { installErrorBannerGuard, test } from './test'

const PLUGIN = 'profile-backend-authorization'
const RUNTIME_PLUGIN = 'profile-backend-e2e'
const PROFILE = 'work'
const CATALOG_FEED = 'https://nousresearch.github.io/hermes-agent/docs/api/plugins.json'
const FIXTURE = path.join(import.meta.dirname, 'fixtures', 'profile-backend-authorization')

interface NativeCall {
  message: string
  detail: string
  buttons: string[]
  response: number
}

interface ProfileBackendBridge {
  confirmSetProfileBackend(input: { plugin: string; profile: string }): Promise<{
    confirmed: boolean
    grant?: string
  }>
  getConnectionConfig(profile: string): Promise<{
    launcher: null | { plugin: string }
    mode: string
    remoteUrl: string
  }>
  setProfileBackend(input: {
    grant?: string
    plugin: string
    profile: string
    token: string
    url: string
  }): Promise<{ applied: boolean }>
}

async function queueNativeAnswers(app: ElectronApplication, ...responses: number[]): Promise<void> {
  await app.evaluate(({ dialog }, answers) => {
    interface DialogState {
      answers: number[]
      calls: NativeCall[]
    }
    const holder = globalThis as typeof globalThis & { __profileBackendE2EDialogs?: DialogState }

    if (!holder.__profileBackendE2EDialogs) {
      const state: DialogState = { answers: [], calls: [] }
      holder.__profileBackendE2EDialogs = state

      ;(dialog as unknown as { showMessageBox: (...args: unknown[]) => Promise<unknown> }).showMessageBox =
        async (...args: unknown[]) => {
          const options = (args.length === 1 ? args[0] : args[1]) as {
            message: string
            detail?: string
            buttons?: string[]
          }

          const response = state.answers.shift()

          if (response === undefined) {throw new Error(`Unexpected native dialog: ${options.message}`)}
          state.calls.push({
            message: options.message,
            detail: options.detail || '',
            buttons: options.buttons || [],
            response,
          })

          return { response, checkboxChecked: false }
        }
    }

    holder.__profileBackendE2EDialogs.answers.push(...answers)
  }, responses)
}

async function nativeCalls(app: ElectronApplication): Promise<NativeCall[]> {
  return app.evaluate(() => {
    const holder = globalThis as typeof globalThis & {
      __profileBackendE2EDialogs?: { calls: NativeCall[] }
    }

    return holder.__profileBackendE2EDialogs?.calls || []
  })
}

async function openPluginSettings(page: Page) {
  await page.evaluate(() => {
    window.location.hash = '#/settings?tab=plugins&plugin=profile-backend-e2e&ppage=profile-backend-e2e'
  })

  return page.getByTestId('profile-backend-e2e-status')
}

async function routeFor(page: Page, profile: string) {
  return page.evaluate(async (target) => {
    const desktop = (window as unknown as { hermesDesktop: ProfileBackendBridge }).hermesDesktop
    const entry = await desktop.getConnectionConfig(target)

    return {
      mode: entry?.mode || 'local',
      url: entry?.remoteUrl || null,
      launcher: entry?.launcher || null,
    }
  }, profile)
}

function providerSaw(provider: ScriptedProvider, marker: string): boolean {
  return provider.completions.some((completion) => JSON.stringify(completion.body).includes(marker))
}

function buildCatalogRepository(root: string, launcherUrl: string): string {
  const repo = path.join(root, 'profile-backend-catalog')
  const pluginDir = path.join(repo, PLUGIN)
  fs.mkdirSync(path.join(pluginDir, 'desktop'), { recursive: true })
  fs.copyFileSync(path.join(FIXTURE, 'plugin.yaml'), path.join(pluginDir, 'plugin.yaml'))

  const pluginSource = fs.readFileSync(path.join(FIXTURE, 'desktop', 'plugin.js'), 'utf8')
    .replaceAll('__LAUNCHER_URL__', launcherUrl)

  fs.writeFileSync(path.join(pluginDir, 'desktop', 'plugin.js'), pluginSource)
  execFileSync('git', ['init', '--quiet', repo])
  execFileSync('git', ['-C', repo, 'add', '.'])
  execFileSync('git', [
    '-C', repo, '-c', 'user.email=e2e@example.invalid', '-c', 'user.name=Hermes E2E',
    'commit', '--quiet', '-m', 'Profile backend authorization fixture',
  ])

  return execFileSync('git', ['-C', repo, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
}

function workSquare(page: Page, sandboxed: boolean) {
  return page.getByRole('group', { name: 'Profiles' }).getByRole('button', {
    name: sandboxed ? 'work — Sandboxed' : 'work',
    exact: true,
  })
}

test('profile backend launch and switch require native authorization', async () => {
  const local = createCoreSandbox('profile-backend-client')
  const launched = createCoreSandbox('profile-backend-launched')
  const localProvider = await startScriptedProvider()
  const launchedProvider = await startScriptedProvider()
  let launcher!: ProfileBackendLauncher
  let app!: ElectronApplication

  try {
    writeProviderHome(local.hermesHome, localProvider.url)
    writeProviderHome(path.join(local.hermesHome, 'profiles', PROFILE), localProvider.url)
    launcher = await startProfileBackendLauncher(launched, launchedProvider.url)
    const commit = buildCatalogRepository(local.root, launcher.url)

    let page: Page

    ;({ app, page } = await launchCoreApp(coreAppEnv(local)))
    installErrorBannerGuard(page)
    const sockets = recordWebSockets(page)
    await waitForInteractive(app, page)

    await page.context().route(CATALOG_FEED, (route) => route.fulfill({
      json: [{
        category: 'desktop',
        description: 'Profile backend authorization E2E fixture',
        maintainer: 'NousResearch',
        name: PLUGIN,
        repo: pathToFileURL(path.join(local.root, 'profile-backend-catalog')).href,
        sha: commit,
        subdir: PLUGIN,
        stars: 1,
        tier: 'official',
        updated: '2026-01-01T00:00:00Z',
        version: '0.1.0',
        platforms: ['desktop'],
      }],
    }))
    await page.evaluate(() => { window.location.hash = '#/capabilities?tab=plugins' })
    const search = page.getByPlaceholder(/search plugins/i).first()
    await search.fill(PLUGIN)
    await page.getByRole('button', { name: `Add ${PLUGIN}`, exact: true }).click()
    const installDialog = page.getByRole('dialog', { name: PLUGIN })
    await installDialog.getByRole('button', { name: 'Install', exact: true }).click()
    await installDialog.waitFor({ state: 'detached' })
    await expect(page.getByRole('switch', { name: 'Profile Backend E2E', exact: true }))
      .toHaveAttribute('aria-checked', 'true')

    await test.step('set Cancel does not prepare, launch, route, or badge the profile', async () => {
      const status = await openPluginSettings(page)
      await expect(status).toBeVisible()
      await expect(workSquare(page, false)).toBeVisible()
      await queueNativeAnswers(app, 1)
      await page.getByRole('button', { name: 'Set sandbox backend', exact: true }).click()
      await expect(status).toHaveText('set cancelled')
      expect(launcher.prepareCalls).toBe(0)
      expect(launcher.pid()).toBeUndefined()
      expect(backendProcesses(launched)).toHaveLength(0)
      expect(await routeFor(page, PROFILE)).toMatchObject({ mode: 'local', launcher: null })
      await expect(workSquare(page, true)).toHaveCount(0)
      const calls = await nativeCalls(app)
      expect(calls).toHaveLength(1)
      expect(calls[0]).toMatchObject({
        message: 'Let the profile-backend-e2e plugin run the “work” profile?',
        response: 1,
      })
      expect(calls[0].buttons).toEqual(['Switch to profile-backend-e2e', 'Cancel'])
    })

    await test.step('set Accept prepares once, launches one backend, and chats only through it', async () => {
      const status = await openPluginSettings(page)
      await queueNativeAnswers(app, 0)
      await page.getByRole('button', { name: 'Set sandbox backend', exact: true }).click()
      await expect(status).toHaveText('set applied')
      expect(launcher.prepareCalls).toBe(1)
      expect(backendProcesses(launched)).toHaveLength(1)
      expect(backendProcesses(launched)[0].pid).toBe(launcher.pid())
      expect(await routeFor(page, PROFILE)).toMatchObject({
        mode: 'remote',
        url: launcher.backendUrl,
        launcher: { plugin: RUNTIME_PLUGIN },
      })
      await expect(workSquare(page, true)).toBeVisible()
      const calls = await nativeCalls(app)
      expect(calls).toHaveLength(2)
      expect(calls[1]).toMatchObject({
        message: 'Let the profile-backend-e2e plugin run the “work” profile?',
        response: 0,
      })

      const marker = 'U1-sandbox'
      const reply = 'unique-reply-from-launched-profile-backend-7f3a'
      launchedProvider.script(marker, [{ text: [reply] }])
      await page.evaluate(() => { window.location.hash = '#/' })
      await workSquare(page, true).click()
      await expect(workSquare(page, true)).toHaveAttribute('aria-pressed', 'true')
      await send(page, marker, 'Enter', sockets)
      await expect(page.getByText(reply, { exact: true })).toBeVisible()
      expect(providerSaw(launchedProvider, marker)).toBe(true)
      expect(providerSaw(localProvider, marker)).toBe(false)
      expect(storedSessionForMarker(launched, PROFILE, marker)).not.toBeNull()
      expect(storedSessionForMarker(local, PROFILE, marker)).toBeNull()
      const launchedPort = new URL(launcher.backendUrl).port
      expect(sockets.sockets.some((socket) => new URL(socket.url).port === launchedPort)).toBe(true)
    })

    await test.step('clear Cancel does not call beforeApply or stop the backend', async () => {
      const status = await openPluginSettings(page)
      const pid = launcher.pid()
      await queueNativeAnswers(app, 1)
      await page.getByRole('button', { name: 'Clear sandbox backend', exact: true }).click()
      await expect(status).toHaveText('clear cancelled')
      expect(launcher.beforeApplyCalls).toBe(0)
      expect(launcher.pid()).toBe(pid)
      expect(backendProcesses(launched).map((process) => process.pid)).toEqual([pid])
      expect(await routeFor(page, PROFILE)).toMatchObject({
        mode: 'remote',
        url: launcher.backendUrl,
        launcher: { plugin: RUNTIME_PLUGIN },
      })
      await expect(workSquare(page, true)).toBeVisible()
      const calls = await nativeCalls(app)
      expect(calls).toHaveLength(3)
      expect(calls[2]).toMatchObject({
        message: 'Stop the profile-backend-e2e plugin from running the “work” profile?',
        response: 1,
      })
      expect(calls[2].buttons).toEqual(['Switch back', 'Cancel'])
    })

    await test.step('clear Accept stops the backend and local work chat still works', async () => {
      const status = await openPluginSettings(page)
      await queueNativeAnswers(app, 0)
      await page.getByRole('button', { name: 'Clear sandbox backend', exact: true }).click()
      await expect(status).toHaveText('clear applied')
      expect(launcher.beforeApplyCalls).toBe(1)
      expect(launcher.pid()).toBeUndefined()
      expect(backendProcesses(launched)).toHaveLength(0)
      expect(await routeFor(page, PROFILE)).toMatchObject({ mode: 'local', launcher: null })
      await expect(workSquare(page, true)).toHaveCount(0)
      await expect(workSquare(page, false)).toBeVisible()
      const calls = await nativeCalls(app)
      expect(calls).toHaveLength(4)
      expect(calls[3]).toMatchObject({
        message: 'Stop the profile-backend-e2e plugin from running the “work” profile?',
        response: 0,
      })

      const marker = 'U3-local'
      const reply = 'unique-reply-from-local-work-profile-91bc'
      localProvider.script(marker, [{ text: [reply] }])
      await page.evaluate(() => { window.location.hash = '#/' })
      await expect(workSquare(page, false)).toHaveAttribute('aria-pressed', 'true')
      await send(page, marker, 'Enter', sockets)
      await expect(page.getByText(reply, { exact: true })).toBeVisible()
      expect(providerSaw(localProvider, marker)).toBe(true)
      expect(providerSaw(launchedProvider, marker)).toBe(false)
      expect(storedSessionForMarker(local, PROFILE, marker)).not.toBeNull()
      expect(storedSessionForMarker(launched, PROFILE, marker)).toBeNull()
    })

    await test.step('real IPC rejects non-loopback launches without another prompt', async () => {
      const before = (await nativeCalls(app)).length
      await expect(page.evaluate(() => {
        const desktop = (window as unknown as { hermesDesktop: ProfileBackendBridge }).hermesDesktop

        return desktop.setProfileBackend({
          plugin: 'profile-backend-e2e',
          profile: 'work',
          url: 'http://192.0.2.10:8642',
          token: 'not-loopback',
        })
      })).rejects.toThrow(/this computer/)
      expect((await nativeCalls(app))).toHaveLength(before)
      expect(launcher.prepareCalls).toBe(1)
      expect(await routeFor(page, PROFILE)).toMatchObject({ mode: 'local', launcher: null })
    })

    await test.step('a grant cannot be redeemed by a different plugin', async () => {
      await queueNativeAnswers(app, 0, 1)

      const result = await page.evaluate(async () => {
        const desktop = (window as unknown as { hermesDesktop: ProfileBackendBridge }).hermesDesktop

        const confirmed = await desktop.confirmSetProfileBackend({
          plugin: 'profile-backend-e2e',
          profile: 'work',
        })

        if (!confirmed.confirmed || !confirmed.grant) {throw new Error('Expected a confirmed set grant')}

        return desktop.setProfileBackend({
          plugin: 'wrong-plugin',
          profile: 'work',
          grant: confirmed.grant,
          url: 'http://127.0.0.1:1',
          token: 'wrong-plugin-must-not-connect',
        })
      })

      expect(result).toEqual({ applied: false })
      const calls = await nativeCalls(app)
      expect(calls.slice(-2).map((call) => call.response)).toEqual([0, 1])
      expect(calls.at(-1)?.message).toContain('wrong-plugin')
      expect(launcher.prepareCalls).toBe(1)
      expect(launcher.beforeApplyCalls).toBe(1)
      expect(backendProcesses(launched)).toHaveLength(0)
      expect(await routeFor(page, PROFILE)).toMatchObject({ mode: 'local', launcher: null })
    })
  } finally {
    try {
      await app?.close()
    } finally {
      try {
        await launcher?.close()
      } finally {
        try {
          await Promise.all([localProvider.close(), launchedProvider.close()])
        } finally {
          launched.cleanup()
          local.cleanup()
        }
      }
    }
  }
})
