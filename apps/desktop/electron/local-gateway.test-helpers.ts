/**
 * Temp homes for the local-gateway suites that bind real AF_UNIX control sockets.
 *
 * sun_path is 104 bytes on macOS (108 on Linux). macOS has a long per-user
 * os.tmpdir(), and CI on either platform can nest TMPDIR below an already long
 * workspace path. `<home>/gateway.sock` and
 * `<runtime>/hermes-gw-<16 hex>/control.sock` then overflow.
 * Use /tmp on both platforms; never the user's home.
 */

import fs from 'node:fs/promises'
import path from 'node:path'

/** Canonical (realpath'd: the gateway canonicalises profile_id) temp dir with a short prefix. */
export async function shortSocketTmpDir(prefix: string): Promise<string> {
  // no-tmp: ok — AF_UNIX sun_path is too short for a nested CI TMPDIR.
  return fs.realpath(await fs.mkdtemp(path.join('/tmp', prefix)))
}
