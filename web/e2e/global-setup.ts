import { snapshotConfigs } from "./config-snapshot";

export default async function globalSetup(): Promise<void> {
  await snapshotConfigs();
}
