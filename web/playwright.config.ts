import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: "http://localhost:9119",
    headless: true,
    screenshot: "only-on-failure",
    video: "off",
  },
  reporter: [["list"]],
  // Run sequentially — single dashboard, avoid race conditions
  workers: 1,
});
