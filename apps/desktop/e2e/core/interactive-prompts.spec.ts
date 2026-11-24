/**
 * C20 core: blocking interactive prompts round-trip through the real chain.
 *
 * Real Electron + real `hermes serve`, approvals in manual mode; only the LLM
 * is faked. For each prompt kind the invariant is end to end, not "a card
 * rendered":
 *  - clarify: exactly one card; the choice the user clicks is exactly what
 *    the model receives in the tool result on its next request; the turn
 *    completes; transcript oracle holds.
 *  - approval (run once): exactly one approval card; the gated command has
 *    NOT run before the click and HAS run after it (observable side effect);
 *    the turn completes; transcript oracle holds.
 *  - approval (deny): the command never runs; the turn still completes (no
 *    wedged busy state); transcript oracle holds.
 *  - folder/site: a real plugin tool waits on the generic gateway queue; the
 *    card's target and answer round-trip without renderer-request injection.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

import { expect, type Locator, type Page, test } from '@playwright/test'

import {
  composer,
  coreAppEnv,
  createCoreSandbox,
  currentSessionId,
  launchCoreApp,
  recordWebSockets,
  send,
  waitForInteractive,
  writeProviderHome
} from './harness'
import { assertTranscriptOracle, installDuplicateSampler, type OracleTarget } from './oracle'
import { shellQuote } from './process-census'
import { type RecordedCompletion, startScriptedProvider } from './provider'

const nonce = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')

const U = (n: number) => `U${n}-${nonce}`
const A = (n: number) => `A${n}-${nonce}`
const AI = (n: number) => `A${n}i-${nonce}`

function viewport(page: Page) {
  return page.locator('[data-slot="aui_thread-viewport"]').filter({ visible: true }).first()
}

/** Text of the tool results the model received in `completion`'s request. */
function toolResults(completion: RecordedCompletion | undefined): string {
  const messages: any[] = completion?.body?.messages ?? []

  return messages
    .filter(m => m?.role === 'tool')
    .map(m => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content)))
    .join('\n')
}

test('clarify and approval prompts round-trip exactly once', async () => {
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox('prompts')
  writeProviderHome(sandbox.hermesHome, provider.url, '', 'manual')
  // Keep this explicit gateway home distinct from the sandbox account default:
  // launchd belongs to the real UID, not HOME, and must never select its bare service.
  const accountHome = path.join(sandbox.root, 'account-home')
  fs.mkdirSync(accountHome)
  const { app, page } = await launchCoreApp(coreAppEnv(sandbox, { HOME: accountHome }))
  const ws = recordWebSockets(page)
  const session: OracleTarget = { sessionId: '', expectUserMarkers: [] }

  const finished = (marker: string, step = 0) =>
    expect
      .poll(() => provider.completions.some(c => c.marker === marker && c.step === step && c.finished), {
        message: `provider finished ${marker} step ${step}`
      })
      .toBe(true)

  const completion = (marker: string, step: number) =>
    provider.completions.find(c => c.marker === marker && c.step === step)

  try {
    await waitForInteractive(app, page)
    await installDuplicateSampler(page)

    await test.step('clarify: the clicked choice is exactly what the model receives', async () => {
      const question = `Q1-${nonce} which drink`
      const pick = `mate-${nonce}`
      const other = `chai-${nonce}`
      provider.script(U(1), [
        {
          text: [`${AI(1)} `, 'asking'],
          toolCalls: [{ name: 'clarify', args: { questions: [{ question, choices: [other, pick] }] } }]
        },
        { text: [`${A(1)} `, 'noted ', 'your ', 'choice'] }
      ])
      await send(page, `${U(1)} ask me`, 'Enter', ws)
      const openForms = page.locator('form[data-clarify-choices], form[data-clarify-batch]').filter({ visible: true })
      await expect(openForms).toHaveCount(1)
      await expect(viewport(page).getByText(question)).toHaveCount(1)

      // Switching away must not settle a question whose tool is still waiting.
      const waitingSession = await currentSessionId(page)
      await page.evaluate(() => { window.location.hash = '#/' })
      await expect.poll(() => currentSessionId(page)).toBe('')
      await page.evaluate(id => { window.location.hash = `#/${id}` }, waitingSession)
      await expect.poll(() => currentSessionId(page)).toBe(waitingSession)
      await expect(openForms).toHaveCount(1)
      await expect(viewport(page).getByText(question)).toHaveCount(1)

      await openForms.getByRole('button', { name: new RegExp(pick) }).click()
      const submit = openForms.locator('button[type="submit"]')
      await expect(submit).toBeEnabled()
      await submit.click()
      await finished(U(1), 1)
      const received = toolResults(completion(U(1), 1))
      expect(received, 'model received the clicked choice').toContain(pick)
      expect(received, 'model did not receive the other choice as the answer').not.toMatch(
        new RegExp(`answer[^\\n]*${other}`, 'i')
      )
      await expect(
        page.locator('form[data-clarify-choices], form[data-clarify-batch]').filter({ visible: true })
      ).toHaveCount(0)
      await expect(page.locator('[data-clarify-settled]').filter({ visible: true })).toHaveCount(1)
      await expect.poll(() => currentSessionId(page)).not.toBe('')
      session.sessionId = await currentSessionId(page)
      session.expectUserMarkers.push(U(1))
      await assertTranscriptOracle(page, ws, provider, session, 'clarify round trip')
      await expect(viewport(page).getByText(question)).toHaveCount(1)
    })

    await test.step('approval (run once): the command runs only after the click', async () => {
      const victim = path.join(sandbox.root, `victim-run-${nonce}`)
      fs.mkdirSync(victim)
      provider.script(U(2), [
        {
          text: [`${AI(2)} `, 'needs ', 'approval'],
          toolCalls: [{ name: 'terminal', args: { command: `rm -rf ${shellQuote(victim)}` } }]
        },
        { text: [`${A(2)} `, 'deleted ', 'it'] }
      ])
      await send(page, `${U(2)} delete run dir`, 'Enter', ws)
      const run = page.locator('[data-approval-run]').filter({ visible: true })
      await expect(run).toHaveCount(1)
      expect(fs.existsSync(victim), 'gated command must not run before approval').toBe(true)
      await run.click()
      await finished(U(2), 1)
      expect(fs.existsSync(victim), 'approved command ran').toBe(false)
      await expect(page.locator('[data-approval-run]').filter({ visible: true })).toHaveCount(0)
      session.expectUserMarkers.push(U(2))
      await assertTranscriptOracle(page, ws, provider, session, 'approval run once')
    })

    await test.step('approval (deny): the command never runs, the turn still completes', async () => {
      const victim = path.join(sandbox.root, `victim-deny-${nonce}`)
      fs.mkdirSync(victim)
      provider.script(U(3), [
        {
          text: [`${AI(3)} `, 'needs ', 'approval'],
          toolCalls: [{ name: 'terminal', args: { command: `rm -rf ${shellQuote(victim)}` } }]
        },
        { text: [`${A(3)} `, 'left ', 'it ', 'alone'] }
      ])
      await send(page, `${U(3)} delete deny dir`, 'Enter', ws)
      const deny = page.locator('[data-approval-deny]').filter({ visible: true })
      await expect(deny).toHaveCount(1)
      await deny.click()
      await finished(U(3), 1)
      expect(fs.existsSync(victim), 'denied command never ran').toBe(true)
      expect(toolResults(completion(U(3), 1)), 'the denial reached the model as the tool result').not.toBe('')
      await expect(page.locator('[data-approval-deny]').filter({ visible: true })).toHaveCount(0)
      session.expectUserMarkers.push(U(3))
      await assertTranscriptOracle(page, ws, provider, session, 'approval deny')
    })
  } finally {
    await app.close().catch(() => undefined)
    await provider.close()
    sandbox.cleanup()
  }
})

test('folder and site cards resolve the real gateway approval queue', async () => {
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox('approval-kinds')
  const pluginDir = path.join(sandbox.hermesHome, 'plugins', 'approval-kinds-e2e')
  fs.mkdirSync(pluginDir, { recursive: true })
  fs.copyFileSync(path.join(import.meta.dirname, 'approval-kinds-plugin.py'), path.join(pluginDir, '__init__.py'))
  fs.copyFileSync(path.join(import.meta.dirname, 'approval-kinds-plugin.yaml'), path.join(pluginDir, 'plugin.yaml'))
  writeProviderHome(sandbox.hermesHome, provider.url, 'plugins:\n  enabled:\n    - approval-kinds-e2e\n', 'manual')
  const accountHome = path.join(sandbox.root, 'account-home')
  fs.mkdirSync(accountHome)
  const { app, page } = await launchCoreApp(coreAppEnv(sandbox, { HOME: accountHome }))
  const ws = recordWebSockets(page)
  const session: OracleTarget = { sessionId: '', expectUserMarkers: [] }

  const finished = (marker: string, step = 0) =>
    expect
      .poll(() => provider.completions.some(c => c.marker === marker && c.step === step && c.finished), {
        message: `provider finished ${marker} step ${step}`
      })
      .toBe(true)

  const completion = (marker: string, step: number) =>
    provider.completions.find(c => c.marker === marker && c.step === step)

  const decision = (marker: string) => {
    const messages: any[] = completion(marker, 1)?.body?.messages ?? []
    const result = messages.find(m => m?.role === 'tool' && m?.tool_call_id === `call_core_${marker}_0_0`)
    expect(result, 'the current approval tool result reached the model').toBeDefined()
    return JSON.parse(typeof result.content === 'string' ? result.content : JSON.stringify(result.content))
  }

  const enterLeavesPending = async (marker: string, card: Locator) => {
    const box = composer(page)
    await box.click()
    await expect(box).toHaveText('')
    await box.press('Enter')
    // Flush key handlers and their React work before checking that no answer escaped.
    await page.evaluate(
      () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    )
    await expect(card).toHaveCount(1)
    expect(completion(marker, 1), 'global Enter did not finish the gated tool').toBeUndefined()
  }

  const expectNoCommandRun = async (card: Locator) => {
    await expect(card.locator('[data-approval-run]')).toHaveCount(0)
    await expect(card.getByRole('button', { name: /^Run/ })).toHaveCount(0)
  }

  try {
    await waitForInteractive(app, page)
    await installDuplicateSampler(page)

    await test.step('read-only folder: Enter is inert, route re-entry preserves it, Allow returns once', async () => {
      const marker = U(4)
      const folder = `/srv/Project  Files-${nonce}`
      const risk = `The agent can read but not change this folder (${nonce}).`
      provider.script(marker, [
        {
          text: [`${AI(4)} `, 'needs ', 'folder ', 'access'],
          toolCalls: [
            { name: 'approval_kinds_e2e', args: { kind: 'access', access: 'read', path: folder, risk } }
          ]
        },
        { text: [`${A(4)} `, 'folder ', 'allowed'] }
      ])
      await send(page, `${marker} request a read-only folder`, 'Enter', ws)
      const card = page.locator('[data-approval-kind="access"]').filter({ visible: true })
      await expect(card).toHaveCount(1)
      await expect(page.locator('[data-approval-kind]').filter({ visible: true })).toHaveCount(1)
      const target = card.locator('[data-slot="tool-approval-target"]')
      await expect(target.locator('span.font-mono')).toHaveText(folder)
      await expect(target).toContainText('Read only')
      await expect(target).not.toContainText('Read and write')
      await expect(card.locator('[data-slot="tool-approval-risk"]')).toHaveText(risk)
      await expectNoCommandRun(card)

      await enterLeavesPending(marker, card)
      const waitingSession = await currentSessionId(page)
      await page.evaluate(() => { window.location.hash = '#/' })
      await expect.poll(() => currentSessionId(page)).toBe('')
      await page.evaluate(id => { window.location.hash = `#/${id}` }, waitingSession)
      await expect.poll(() => currentSessionId(page)).toBe(waitingSession)
      await expect(card).toHaveCount(1)
      await expect(target.locator('span.font-mono')).toHaveText(folder)
      expect(completion(marker, 1), 'route re-entry did not answer the queue entry').toBeUndefined()

      await card.getByRole('button', { name: 'Allow', exact: true }).click()
      await finished(marker, 1)
      expect(decision(marker), 'the real queue decision reached the scripted model').toEqual({
        choice: 'once',
        kind: 'access',
        reason: null,
        resolved: true
      })
      await expect(card).toHaveCount(0)
      session.sessionId = waitingSession
      session.expectUserMarkers.push(marker)
      await assertTranscriptOracle(page, ws, provider, session, 'read-only folder approval')
    })

    await test.step('read-write folder: Deny returns the trimmed reason', async () => {
      const marker = U(5)
      const folder = `/srv/Shared  Output-${nonce}`
      const risk = `The agent can change and delete files in this folder (${nonce}).`
      const reason = `Not this folder ${nonce}`
      provider.script(marker, [
        {
          text: [`${AI(5)} `, 'needs ', 'write ', 'access'],
          toolCalls: [
            { name: 'approval_kinds_e2e', args: { kind: 'access', access: 'read_write', path: folder, risk } }
          ]
        },
        { text: [`${A(5)} `, 'folder ', 'denied'] }
      ])
      await send(page, `${marker} request a read-write folder`, 'Enter', ws)
      const card = page.locator('[data-approval-kind="access"]').filter({ visible: true })
      await expect(card).toHaveCount(1)
      await expect(page.locator('[data-approval-kind]').filter({ visible: true })).toHaveCount(1)
      const target = card.locator('[data-slot="tool-approval-target"]')
      await expect(target.locator('span.font-mono')).toHaveText(folder)
      await expect(target).toContainText('Read and write')
      await expect(target).not.toContainText('Read only')
      await expect(card.locator('[data-slot="tool-approval-risk"]')).toHaveText(risk)
      await expectNoCommandRun(card)

      await card.getByRole('button', { name: 'Deny', exact: true }).click()
      const reasonInput = card.getByRole('textbox', { name: 'Reason for denying' })
      await reasonInput.fill(`  ${reason}  `)
      await reasonInput.press('Enter')
      await finished(marker, 1)
      expect(decision(marker), 'the denial and trimmed reason reached the scripted model').toEqual({
        choice: 'deny',
        kind: 'access',
        reason,
        resolved: true
      })
      await expect(card).toHaveCount(0)
      session.expectUserMarkers.push(marker)
      await assertTranscriptOracle(page, ws, provider, session, 'read-write folder denial')
    })

    await test.step('site: exact host and risk, Enter is inert, Allow returns once', async () => {
      const marker = U(6)
      const host = `downloads-${nonce}.example`
      const risk = `Any program in the sandbox can reach this host (${nonce}).`
      provider.script(marker, [
        {
          text: [`${AI(6)} `, 'needs ', 'site ', 'access'],
          toolCalls: [{ name: 'approval_kinds_e2e', args: { kind: 'network', host, risk } }]
        },
        { text: [`${A(6)} `, 'site ', 'allowed'] }
      ])
      await send(page, `${marker} request a site`, 'Enter', ws)
      const card = page.locator('[data-approval-kind="network"]').filter({ visible: true })
      await expect(card).toHaveCount(1)
      await expect(page.locator('[data-approval-kind]').filter({ visible: true })).toHaveCount(1)
      await expect(card.locator('[data-slot="tool-approval-target"] span.font-mono')).toHaveText(host)
      await expect(card.locator('[data-slot="tool-approval-risk"]')).toHaveText(risk)
      await expectNoCommandRun(card)

      await enterLeavesPending(marker, card)
      await card.getByRole('button', { name: 'Allow', exact: true }).click()
      await finished(marker, 1)
      expect(decision(marker), 'the real site queue decision reached the scripted model').toEqual({
        choice: 'once',
        kind: 'network',
        reason: null,
        resolved: true
      })
      await expect(card).toHaveCount(0)
      session.expectUserMarkers.push(marker)
      await assertTranscriptOracle(page, ws, provider, session, 'site approval')
    })
  } finally {
    await app.close().catch(() => undefined)
    await provider.close()
    sandbox.cleanup()
  }
})
