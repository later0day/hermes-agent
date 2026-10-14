import { describe, expect, it } from "vitest";

import { cronDeliveryTarget, cronOriginLabel } from "./cron-job";

const origin = { platform: "dingtalk", chat_id: "cidA==", chat_name: "Ops alerts" };

describe("cron delivery target", () => {
  it("names the creating chat for deliver=origin (the default)", () => {
    expect(cronDeliveryTarget({ deliver: "origin", origin })).toBe("dingtalk · Ops alerts");
    expect(cronDeliveryTarget({ deliver: null, origin })).toBe("dingtalk · Ops alerts");
    expect(cronOriginLabel({ deliver: "origin", origin })).toBeNull();
  });

  it("lists explicit targets, naming the one that is the creating chat", () => {
    expect(cronDeliveryTarget({ deliver: "dingtalk:cidA==,telegram:42", origin })).toBe("dingtalk · Ops alerts, telegram · 42");
    expect(cronDeliveryTarget({ deliver: "telegram", origin })).toBe("telegram");
    expect(cronOriginLabel({ deliver: "telegram", origin })).toBe("dingtalk · Ops alerts");
  });

  it("is null when the report stays local or there is no origin to deliver to", () => {
    expect(cronDeliveryTarget({ deliver: "local", origin })).toBeNull();
    expect(cronDeliveryTarget({ deliver: "origin", origin: null })).toBeNull();
  });
});
