import { restoreState } from "./state-snapshot";

export default async function globalTeardown(): Promise<void> {
  const { restored, newProfiles } = await restoreState();
  for (const path of restored) console.log(`[e2e] restored ${path}`);
  if (newProfiles.length) console.log(`[e2e] profiles created during the run and left behind: ${newProfiles.join(", ")}`);
}
