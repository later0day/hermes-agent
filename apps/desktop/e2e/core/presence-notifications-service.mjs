import * as fs from 'node:fs'

import { createClient } from 'dbus-native'

const SERVICE = 'org.freedesktop.Notifications'
const PATH = '/org/freedesktop/Notifications'
const [address, output] = process.argv.slice(2)

if (!address || !output) {
  throw new Error('usage: presence-notifications-service.mjs <bus-address> <output>')
}

const bus = createClient({ busAddress: address, authMethods: ['EXTERNAL'], direct: true })
let nextId = 0

bus.exportInterface(
  {
    GetCapabilities() {
      return ['body']
    },
    Notify(appName, replacesId, icon, title, body, actions, hints, timeout, message) {
      const id = ++nextId
      fs.appendFileSync(
        output,
        `${JSON.stringify({ id, sender: message.sender, body: [appName, replacesId, icon, title, body, actions, hints, timeout] })}\n`
      )

      return id
    },
    CloseNotification() {
      return null
    },
    GetServerInformation() {
      return ['Hermes E2E', 'Hermes', '1', '1.2']
    }
  },
  PATH,
  {
    name: SERVICE,
    methods: {
      GetCapabilities: ['', 'as'],
      Notify: ['susssasa{sv}i', 'u'],
      CloseNotification: ['u', ''],
      GetServerInformation: ['', 'ssss']
    },
    signals: {}
  }
)

bus.connection.on('error', error => {
  console.error(error)
  process.exitCode = 1
})

bus.name = await bus.invokeDbus({ member: 'Hello' })
const request = await bus.invokeDbus({ member: 'RequestName', signature: 'su', body: [SERVICE, 0] })

if (request !== 1) {
  throw new Error(`RequestName returned ${request}`)
}

process.send?.({ ready: true })
