// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { gatewayStateDescription, sharedGatewayRestartedMessage } from "./shared-gateway";
import { isoTimeAgo, timeAgo } from "./utils";

// Helpers rendered on every page without `t` follow the dashboard language (I18nProvider sets <html lang>).
afterEach(() => {
  document.documentElement.lang = "";
});

describe("language-following helpers", () => {
  it("render Chinese when the dashboard is in Simplified Chinese", () => {
    document.documentElement.lang = "zh";
    const now = Date.now() / 1000;
    expect(timeAgo(now - 10)).toBe("刚刚");
    expect(timeAgo(now - 5 * 60)).toBe("5 分钟前");
    expect(timeAgo(now - 100_000)).toBe("昨天");
    expect(isoTimeAgo(new Date(Date.now() - 3 * 86_400_000).toISOString())).toBe("3 天前");
    expect(gatewayStateDescription("running", true)).toBe("运行中 —— 消息频道在线");
    expect(gatewayStateDescription("degraded", false)).toContain("看门狗");
    expect(sharedGatewayRestartedMessage(3)).toBe("共享网关已重启（3 个机器人）");
  });

  it("stay English otherwise", () => {
    document.documentElement.lang = "en";
    expect(timeAgo(Date.now() / 1000 - 10)).toBe("just now");
    expect(gatewayStateDescription("running", true)).toBe("Running — messaging channels are online");
    expect(gatewayStateDescription("exited", undefined)).toBe("Stopped — messaging channels are offline");
  });
});
