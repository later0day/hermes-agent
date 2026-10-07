// The E2E suite drives the operator's real dashboard, and many tests save what they edit:
// ConfigPage once left model.context_length: 42 in two profiles, the MCP page replaced a real
// server with /bin/echo, the hooks page stacked `echo e2e-test-hook` entries, and the Keys page
// overwrote a real credential in a profile's .env. Snapshot every profile's mutable state before
// the run and put the exact bytes back afterwards, however a test exits. Copies live in a 0700
// temp dir (they include .env secrets) that teardown deletes.
import {
  chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const BASE = "http://localhost:9119";
const POINTER = join(tmpdir(), "hermes-e2e-state-snapshot.path");
// Per-profile files the dashboard writes, relative to the profile home.
const TRACKED = ["config.yaml", ".env", join("cron", "jobs.json")];

interface Entry {
  path: string;
  copy: string | null; // null: the file did not exist before the run
}

interface Manifest {
  profiles: string[];
  entries: Entry[];
}

async function sessionToken(): Promise<string> {
  const html = await (await fetch(`${BASE}/`)).text();
  const match = html.match(/window\.__HERMES_SESSION_TOKEN__="([^"]*)"/);
  if (!match) throw new Error("state snapshot: could not read the dashboard session token");
  return match[1];
}

async function profileHomes(): Promise<Map<string, string>> {
  const headers = { "X-Hermes-Session-Token": await sessionToken() };
  const { profiles } = await (await fetch(`${BASE}/api/profiles`, { headers })).json();
  const homes = new Map<string, string>();
  for (const { name } of profiles as { name: string }[]) {
    const raw = await fetch(`${BASE}/api/config/raw?profile=${encodeURIComponent(name)}`, { headers });
    homes.set(name, dirname((await raw.json()).path));
  }
  return homes;
}

export async function snapshotState(): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "hermes-e2e-state-"));
  chmodSync(dir, 0o700);
  const homes = await profileHomes();
  const entries: Entry[] = [];
  let n = 0;
  for (const home of homes.values()) {
    for (const rel of TRACKED) {
      const path = join(home, rel);
      if (!existsSync(path)) {
        entries.push({ path, copy: null });
        continue;
      }
      const copy = join(dir, String(n++));
      copyFileSync(path, copy);
      chmodSync(copy, 0o600);
      entries.push({ path, copy });
    }
  }
  const manifest: Manifest = { profiles: [...homes.keys()], entries };
  writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest), { mode: 0o600 });
  writeFileSync(POINTER, dir, { mode: 0o600 });
}

export async function restoreState(): Promise<{ restored: string[]; newProfiles: string[] }> {
  if (!existsSync(POINTER)) return { restored: [], newProfiles: [] };
  const dir = readFileSync(POINTER, "utf-8");
  const manifest: Manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf-8"));
  const restored: string[] = [];
  for (const { path, copy } of manifest.entries) {
    if (copy === null) {
      if (existsSync(path)) {
        rmSync(path);
        restored.push(path);
      }
      continue;
    }
    const original = readFileSync(copy);
    if (!existsSync(path) || !readFileSync(path).equals(original)) {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, original);
      restored.push(path);
    }
  }
  let newProfiles: string[] = [];
  try {
    newProfiles = [...(await profileHomes()).keys()].filter((name) => !manifest.profiles.includes(name));
  } catch {
    // Dashboard already gone: the file restore above is what matters.
  }
  rmSync(dir, { recursive: true, force: true });
  rmSync(POINTER);
  return { restored, newProfiles };
}
