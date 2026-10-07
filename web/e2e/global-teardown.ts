import { restoreConfigs } from "./config-snapshot";

export default function globalTeardown(): void {
  for (const path of restoreConfigs()) console.log(`[e2e] restored ${path}`);
}
