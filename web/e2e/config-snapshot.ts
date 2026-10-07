// The E2E suite drives the operator's real dashboard, and several ConfigPage tests save
// edits (a number field once left model.context_length: 42 in two profiles, which broke
// every later turn). Snapshot every profile's config.yaml before the run and put the exact
// bytes back afterwards, so no test can leak a config edit however it exits.
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = "http://localhost:9119";
const SNAPSHOT_FILE = join(tmpdir(), "hermes-e2e-config-snapshot.json");

interface ConfigSnapshot {
  path: string;
  existed: boolean;
  text: string;
}

async function sessionToken(): Promise<string> {
  const html = await (await fetch(`${BASE}/`)).text();
  const match = html.match(/window\.__HERMES_SESSION_TOKEN__="([^"]*)"/);
  if (!match) throw new Error("config snapshot: could not read the dashboard session token");
  return match[1];
}

export async function snapshotConfigs(): Promise<void> {
  const headers = { "X-Hermes-Session-Token": await sessionToken() };
  const { profiles } = await (await fetch(`${BASE}/api/profiles`, { headers })).json();
  const snapshots: ConfigSnapshot[] = [];
  for (const { name } of profiles as { name: string }[]) {
    const raw = await fetch(`${BASE}/api/config/raw?profile=${encodeURIComponent(name)}`, { headers });
    const { path } = await raw.json();
    const existed = existsSync(path);
    snapshots.push({ path, existed, text: existed ? readFileSync(path, "utf-8") : "" });
  }
  writeFileSync(SNAPSHOT_FILE, JSON.stringify(snapshots));
}

export function restoreConfigs(): string[] {
  if (!existsSync(SNAPSHOT_FILE)) return [];
  const snapshots: ConfigSnapshot[] = JSON.parse(readFileSync(SNAPSHOT_FILE, "utf-8"));
  const restored: string[] = [];
  for (const { path, existed, text } of snapshots) {
    if (!existed) {
      if (existsSync(path)) {
        rmSync(path);
        restored.push(path);
      }
      continue;
    }
    if (!existsSync(path) || readFileSync(path, "utf-8") !== text) {
      writeFileSync(path, text);
      restored.push(path);
    }
  }
  rmSync(SNAPSHOT_FILE);
  return restored;
}
