import { snapshotState } from "./state-snapshot";

export default async function globalSetup(): Promise<void> {
  await snapshotState();
}
