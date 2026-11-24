import { type ChildProcess, spawn } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'

export interface DeliveredNotification {
  id: number
  sender: string
  body: unknown[]
}

export interface PrivateNotificationBus {
  address: string
  startService: () => Promise<void>
  notifications: () => DeliveredNotification[]
  debug: () => string
  close: () => Promise<void>
}

function outputOf(child: ChildProcess): string[] {
  const lines: string[] = []
  child.stderr?.on('data', chunk => lines.push(...String(chunk).split('\n').filter(Boolean)))

  return lines
}

async function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }

  const exited = new Promise<void>(resolve => child.once('exit', () => resolve()))
  child.kill('SIGTERM')
  await exited
}

function daemonAddress(daemon: ChildProcess, errors: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error(`dbus-daemon did not print its address\n${errors.join('\n')}`)), 10_000)

    const fail = (error: Error) => {
      clearTimeout(timer)
      reject(error)
    }

    daemon.once('error', fail)
    daemon.once('exit', code => fail(new Error(`dbus-daemon exited with ${code}\n${errors.join('\n')}`)))
    daemon.stdout?.on('data', chunk => {
      output += String(chunk)
      const address = output.split('\n')[0]?.trim()

      if (address) {
        clearTimeout(timer)
        resolve(address)
      }
    })
  })
}

function serviceReady(service: ChildProcess, errors: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`notification service did not start\n${errors.join('\n')}`)), 10_000)
    const fail = (error: Error) => {
      clearTimeout(timer)
      reject(error)
    }

    service.once('error', fail)
    service.once('exit', code => fail(new Error(`notification service exited with ${code}\n${errors.join('\n')}`)))
    service.on('message', message => {
      if ((message as { ready?: boolean })?.ready) {
        clearTimeout(timer)
        resolve()
      }
    })
  })
}

export async function startPrivateNotificationBus(root: string): Promise<PrivateNotificationBus> {
  const daemon = spawn('dbus-daemon', ['--session', '--nofork', '--nopidfile', '--print-address=1'], {
    stdio: ['ignore', 'pipe', 'pipe']
  })
  const daemonErrors = outputOf(daemon)
  const address = await daemonAddress(daemon, daemonErrors)
  const output = path.join(root, 'notifications.jsonl')
  let service: ChildProcess | undefined
  let serviceErrors: string[] = []

  return {
    address,
    startService: async () => {
      if (service) {
        throw new Error('notification service already started')
      }

      service = spawn(
        process.execPath,
        [path.join(import.meta.dirname, 'presence-notifications-service.mjs'), address, output],
        { env: { ...process.env, DBUS_SESSION_BUS_ADDRESS: address }, stdio: ['ignore', 'ignore', 'pipe', 'ipc'] }
      )
      serviceErrors = outputOf(service)
      await serviceReady(service, serviceErrors)
    },
    notifications: () => {
      if (!fs.existsSync(output)) {
        return []
      }

      return fs
        .readFileSync(output, 'utf8')
        .split('\n')
        .filter(Boolean)
        .map(line => JSON.parse(line) as DeliveredNotification)
    },
    debug: () => {
      const calls = fs.existsSync(`${output}.calls`) ? fs.readFileSync(`${output}.calls`, 'utf8') : ''

      return `${calls}${serviceErrors.join('\n')}`
    },
    close: async () => {
      if (service) {
        await stop(service)
      }

      await stop(daemon)
    }
  }
}
