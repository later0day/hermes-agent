import { fetchJSON } from "@/lib/api";

export interface WeixinOnboardingStartResponse {
  pairing_id: string;
  status:
    | "starting"
    | "waiting"
    | "connected"
    | "error"
    | "expired"
    | "cancelled";
  scan_state?: "wait" | "scaned";
  qr_payload?: string | null;
  expires_at: string;
  account_id?: string | null;
  error?: string | null;
}

export type WeixinOnboardingStatusResponse = WeixinOnboardingStartResponse;

export interface WeixinOnboardingApplyResponse {
  ok: boolean;
  platform: "weixin";
  account_id?: string | null;
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export const weixinOnboardingApi = {
  start: (body: { profile?: string } = {}) =>
    fetchJSON<WeixinOnboardingStartResponse>(
      "/api/messaging/weixin/onboarding/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  status: (pairingId: string) =>
    fetchJSON<WeixinOnboardingStatusResponse>(
      `/api/messaging/weixin/onboarding/${encodeURIComponent(pairingId)}`,
    ),
  apply: (pairingId: string, body: { profile?: string } = {}) =>
    fetchJSON<WeixinOnboardingApplyResponse>(
      `/api/messaging/weixin/onboarding/${encodeURIComponent(pairingId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  cancel: (pairingId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/messaging/weixin/onboarding/${encodeURIComponent(pairingId)}`,
      { method: "DELETE" },
    ),
};
