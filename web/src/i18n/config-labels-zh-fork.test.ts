import { describe, expect, it } from "vitest";

import { getConfigFieldLabel, getConfigSectionLabel } from "./config-labels";
import { ZH_CONFIG_FIELDS, ZH_CONFIG_LEAVES, ZH_CONFIG_SEGMENTS } from "./config-labels-zh-fork";
// Dot-path → category of every field `/api/config/schema` serves. Regenerate from
// hermes_cli.web_server_config.CONFIG_SCHEMA; tests/hermes_cli/test_config_schema_snapshot.py
// fails when it lists a path or category the backend no longer has.
import snapshot from "./config-schema-snapshot.json";

const SCHEMA = snapshot as Record<string, string>;
const PATHS = Object.keys(SCHEMA);

/** The backend's fallback description: the dot-path humanized by Python's str.title(). */
function autoDescription(path: string): string {
  return path
    .replace(/\./g, " → ")
    .replace(/_/g, " ")
    .replace(/[A-Za-z]+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}

const LEAVES = new Set(PATHS.map((p) => p.split(".").pop()!));
const SEGMENTS = new Set([
  ...Object.values(SCHEMA),
  ...PATHS.flatMap((p) => p.split(".").slice(0, -1)),
]);

describe("Config page zh labels", () => {
  it("renders the representative sections and fields in Chinese", () => {
    expect(getConfigSectionLabel("zh", "tool_loop_guardrails")).toBe("工具循环护栏");
    expect(getConfigSectionLabel("zh", "gateway")).toBe("网关");
    expect(getConfigSectionLabel("zh", "cron")).toBe("定时任务");
    expect(getConfigFieldLabel("zh", "max_concurrent_sessions")?.label).toBe("最大并发会话数");
    expect(getConfigFieldLabel("zh", "agent.max_turns")?.label).toBe("最大轮次");
    expect(getConfigFieldLabel("zh", "terminal.timeout")?.label).toBe("命令超时");
    expect(getConfigFieldLabel("zh", "kanban.max_in_progress_per_profile")?.label).toBe(
      "每个 Profile 最大进行中任务数",
    );
  });

  it("translates the backend's auto path description but keeps a hand-written English one", () => {
    const key = "tool_loop_guardrails.warn_after.exact_failure";
    expect(getConfigFieldLabel("zh", key, autoDescription(key))?.description).toBe(
      "工具循环护栏 → 警告阈值 → 完全相同的失败",
    );
    expect(getConfigFieldLabel("zh", "streaming.cursor", "Cursor glyph")?.description).toBeUndefined();
    // Upstream's curated entry wins over the fork overlay.
    expect(getConfigFieldLabel("zh", "display.skin")?.label).toBe("CLI 主题");
  });

  it("gives every schema field a zh label and every section/category a Chinese name", () => {
    expect(PATHS.length).toBeGreaterThan(500);
    const unlabeled = PATHS.filter((p) => getConfigFieldLabel("zh", p)?.label === undefined);
    const untranslatedPaths = PATHS.filter((p) => {
      const d = getConfigFieldLabel("zh", p, autoDescription(p))?.description;
      return d === undefined || d === autoDescription(p);
    });
    const unnamed = [...SEGMENTS].filter((s) => getConfigSectionLabel("zh", s) === undefined);
    expect({ unlabeled, untranslatedPaths, unnamed }).toEqual({
      unlabeled: [],
      untranslatedPaths: [],
      unnamed: [],
    });
  });

  it("leaves English output untouched", () => {
    const changed = PATHS.filter((p) => getConfigFieldLabel("en", p, autoDescription(p)) !== undefined);
    expect(changed).toEqual([]);
    expect(getConfigSectionLabel("en", "tool_loop_guardrails")).toBeUndefined();
  });

  it("has no overlay entry for a config path, leaf or section the schema no longer has", () => {
    expect({
      fields: Object.keys(ZH_CONFIG_FIELDS).filter((k) => !(k in SCHEMA)),
      leaves: Object.keys(ZH_CONFIG_LEAVES).filter((k) => !LEAVES.has(k)),
      segments: Object.keys(ZH_CONFIG_SEGMENTS).filter((k) => !SEGMENTS.has(k)),
    }).toEqual({ fields: [], leaves: [], segments: [] });
  });
});
