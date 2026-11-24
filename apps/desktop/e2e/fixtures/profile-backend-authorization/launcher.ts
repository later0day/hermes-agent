import { type ChildProcess, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'

import { coreAppEnv, type CoreSandbox, freePort, REPO_ROOT, writeProviderHome } from '../../core/harness'

export interface ProfileBackendLauncher {
  readonly url: string
  readonly backendUrl: string
  readonly token: string
  readonly prepareCalls: number
  readonly beforeApplyCalls: number
  pid(): number | undefined
  logTail(): string
  close(): Promise<void>
}

function alive(child: ChildProcess | undefined): child is ChildProcess {
  return Boolean(child && child.exitCode === null && child.signalCode === null)
}

function signalProcessGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  if (!alive(child)) {return}

  try {
    if (process.platform !== 'win32' && child.pid) {process.kill(-child.pid, signal)}
    else {child.kill(signal)}
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ESRCH') {throw error}
  }
}

async function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (!alive(child)) {return true}

  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off('exit', onExit)
      resolve(false)
    }, timeoutMs)

    const onExit = () => {
      clearTimeout(timer)
      resolve(true)
    }

    child.once('exit', onExit)
  })
}

export async function startProfileBackendLauncher(
  sandbox: CoreSandbox,
  providerUrl: string,
): Promise<ProfileBackendLauncher> {
  const port = await freePort()
  const controlPort = await freePort()
  const token = `profile-backend-e2e-${randomBytes(16).toString('hex')}`
  const backendUrl = `http://127.0.0.1:${port}`
  let child: ChildProcess | undefined
  let log = ''
  let prepareCalls = 0
  let beforeApplyCalls = 0
  let stopping: Promise<void> | undefined

  const stopBackend = () => {
    if (stopping) {return stopping}
    stopping = (async () => {
      if (!alive(child)) {return}
      const running = child
      signalProcessGroup(running, 'SIGTERM')

      if (await waitForExit(running, 15_000)) {return}
      signalProcessGroup(running, 'SIGKILL')

      if (!(await waitForExit(running, 5_000))) {
        throw new Error(`Launched backend ${running.pid} did not exit\n${log.slice(-5000)}`)
      }
    })()

    return stopping
  }

  const startBackend = async () => {
    if (alive(child)) {return}
    writeProviderHome(path.join(sandbox.hermesHome, 'profiles', 'work'), providerUrl)
    await fs.promises.writeFile(
      path.join(sandbox.hermesHome, '.env'),
      `HERMES_DASHBOARD_SESSION_TOKEN=${token}\n`,
    )

    const python = process.env.HERMES_E2E_PYTHON || path.join(REPO_ROOT, '.venv', 'bin', 'python')
    const env = coreAppEnv(sandbox)
    child = spawn(python, [
      '-m', 'hermes_cli.main', '--profile', 'work', 'serve',
      '--host', '127.0.0.1', '--port', String(port), '--skip-build',
    ], {
      cwd: REPO_ROOT,
      env: {
        ...env,
        PYTHONPATH: [REPO_ROOT, env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        HERMES_DASHBOARD_SESSION_TOKEN: token,
      },
      detached: process.platform !== 'win32',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    child.stdout?.on('data', (chunk) => { log += String(chunk) })
    child.stderr?.on('data', (chunk) => { log += String(chunk) })

    const deadline = Date.now() + 120_000
    let lastError = 'not ready'

    while (Date.now() < deadline) {
      if (!alive(child)) {throw new Error(`Launched backend exited before readiness\n${log.slice(-8000)}`)}

      try {
        const response = await fetch(`${backendUrl}/api/status`, {
          headers: { Authorization: `Bearer ${token}` },
        })

        if (response.ok) {return}
        lastError = `HTTP ${response.status}`
      } catch (error) {
        lastError = String(error)
      }

      await new Promise((resolve) => setTimeout(resolve, 200))
    }

    throw new Error(`Launched backend readiness timed out: ${lastError}\n${log.slice(-8000)}`)
  }

  const server = http.createServer((req, res) => {
    void (async () => {
      req.resume()
      res.setHeader('Access-Control-Allow-Origin', '*')
      res.setHeader('Content-Type', 'application/json')

      try {
        if (req.method !== 'POST' || (req.url !== '/start' && req.url !== '/stop')) {
          res.writeHead(404).end(JSON.stringify({ error: 'Unknown launcher endpoint' }))

          return
        }

        if (req.url === '/start') {
          prepareCalls++
          await startBackend()
          res.writeHead(200).end(JSON.stringify({ url: backendUrl, token }))
        } else {
          beforeApplyCalls++
          await stopBackend()
          res.writeHead(200).end(JSON.stringify({ stopped: true }))
        }
      } catch (error) {
        res.writeHead(500).end(JSON.stringify({ error: String(error) }))
      }
    })()
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(controlPort, '127.0.0.1', () => {
      server.off('error', reject)
      resolve()
    })
  })

  return {
    url: `http://127.0.0.1:${controlPort}`,
    backendUrl,
    token,
    get prepareCalls() { return prepareCalls },
    get beforeApplyCalls() { return beforeApplyCalls },
    pid: () => alive(child) ? child.pid : undefined,
    logTail: () => log.slice(-8000),
    close: async () => {
      await stopBackend()
      server.closeAllConnections()
      await new Promise<void>((resolve) => server.close(() => resolve()))
    },
  }
}
