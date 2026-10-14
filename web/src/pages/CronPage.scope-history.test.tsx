// @vitest-environment jsdom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

// Cron page parity with the fork: the job list follows the dashboard's global profile switcher, and each job
// has a run-history view over GET /api/cron/jobs/{id}/runs (the backend existed; the web had no UI for it).

const calls = vi.hoisted(() => ({ getCronJobs: [] as string[], getCronJobRuns: [] as [string, string][] }));
const job = vi.hoisted(() => ({
  id: "job-1", name: "Daily report", prompt: "x", schedule: { kind: "interval", minutes: 60, display: "every 60m" },
  schedule_display: "every 60m", enabled: true, state: "scheduled", deliver: "origin", profile: "xcx",
  origin: { platform: "dingtalk", chat_id: "cidA==", chat_name: "北京告警群" },
}));

vi.mock("@/lib/api-cron-runs", () => ({
  cronRunsApi: {
    list: async (id: string, profile: string) => {
      calls.getCronJobRuns.push([id, profile]);
      return { runs: [{ id: "cron_job-1_1", started_at: 1000, ended_at: 1094, end_reason: "cron_complete", model: "kimi-k3", tool_call_count: 2, input_tokens: 10, output_tokens: 5 }], limit: 20 };
    },
  },
}));
vi.mock("@/lib/api", () => {
  const specific: Record<string, (...args: unknown[]) => Promise<unknown>> = {
    getCronJobs: async (profile) => {
      calls.getCronJobs.push(String(profile));
      return [job];
    },
    getCronJobRuns: async (id, profile) => {
      calls.getCronJobRuns.push([String(id), String(profile)]);
      return { runs: [{ id: "cron_job-1_1", started_at: 1000, ended_at: 1094, end_reason: "cron_complete", model: "kimi-k3", tool_call_count: 2, input_tokens: 10, output_tokens: 5 }], limit: 20 };
    },
    getProfiles: async () => ({ profiles: [{ name: "default" }, { name: "xcx" }, { name: "ops" }] }),
    getCronDeliveryTargets: async () => ({ targets: [] }),
    getSkills: async () => [],
    getToolsets: async () => [],
  };
  const api = new Proxy(specific, {
    get: (target, key: string) => target[key] ?? (async () => ({})),
  });
  return { api, setManagementProfile: vi.fn(), getManagementProfile: vi.fn(() => "") };
});
vi.mock("@/plugins", () => ({ PluginSlot: () => null }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | undefined;
let setScope: (name: string) => void = () => undefined;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  calls.getCronJobs.length = 0;
  calls.getCronJobRuns.length = 0;
  localStorage.clear();
});

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

async function renderCron(initialScope: string) {
  const [{ default: CronPage }, { I18nProvider }, { ProfileContext }, { PageHeaderProvider }] = await Promise.all([
    import("./CronPage"), import("@/i18n"), import("@/contexts/profile-context"), import("@/contexts/PageHeaderProvider"),
  ]);
  function Harness() {
    const [profile, setProfile] = useState(initialScope);
    setScope = setProfile;
    return (
      <ProfileContext.Provider value={{ profile, currentProfile: "default", profiles: ["default", "xcx", "ops"], setProfile }}>
        <PageHeaderProvider pluginTabs={[]}><CronPage /></PageHeaderProvider>
      </ProfileContext.Provider>
    );
  }
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(<MemoryRouter><I18nProvider><Harness /></I18nProvider></MemoryRouter>));
  await settle();
  return container;
}

describe("CronPage", () => {
  it("lists the globally scoped profile's jobs and follows the switcher", async () => {
    await renderCron("xcx");
    expect(calls.getCronJobs.at(-1)).toBe("xcx");
    await act(async () => setScope("ops"));
    await settle();
    expect(calls.getCronJobs.at(-1)).toBe("ops");
  });

  it("shows all profiles when no profile is scoped", async () => {
    await renderCron("");
    expect(calls.getCronJobs.at(-1)).toBe("all");
  });

  it("opens a job's run history from its owner profile, in the active locale", async () => {
    localStorage.setItem("hermes-locale", "zh");
    await renderCron("xcx");
    const history = document.querySelector('button[aria-label="运行历史"]') as HTMLButtonElement | null;
    expect(history).not.toBeNull();
    await act(async () => history?.click());
    await settle();
    expect(calls.getCronJobRuns).toEqual([["job-1", "xcx"]]);
    const dialog = document.querySelector('[aria-labelledby="cron-run-history-title"]');
    expect(dialog?.textContent).toContain("已完成");
    expect(dialog?.textContent).toContain("1m 34s");
    expect(dialog?.textContent).toContain("kimi-k3");
    // The card names where the report goes (fork parity: owner/destination on the cron page).
    expect(document.body.textContent).toContain("推送到: dingtalk · 北京告警群");
  });
});
