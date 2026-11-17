import type { ActionStatusResponse, SkillInfo } from '@/lib/api'

/** Only hub-installed skills can be uninstalled (`hermes skills uninstall` refuses bundled and
 *  agent-created ones), so only they get a delete control. ``provenance`` is "bundled" | "hub" |
 *  "agent" in the dashboard's skill listing (hermes_cli/web_routers/skills.py). */
export function canUninstallSkill(skill: Partial<SkillInfo> & { provenance?: string }): boolean {
  return skill.provenance === 'hub'
}

/** Poll a spawned action until it exits (or `attempts` run out); resolves its last status. */
export async function waitForAction(
  getStatus: (name: string) => Promise<ActionStatusResponse>,
  name: string,
  { attempts = 50, intervalMs = 1200 }: { attempts?: number; intervalMs?: number } = {}
): Promise<ActionStatusResponse | null> {
  let last: ActionStatusResponse | null = null
  for (let i = 0; i < attempts; i++) {
    last = await getStatus(name)
    if (!last.running) return last
    await new Promise(resolve => setTimeout(resolve, intervalMs))
  }
  return last
}
