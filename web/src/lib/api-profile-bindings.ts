import { fetchJSON } from "@/lib/api";

/** A chat from the gateway's channel directory that can be bound to a profile. */
export interface BindableChat {
  platform: string;
  chat_id: string;
  name: string;
  chat_type: string;
}

export interface ProfileStaticRoute {
  name: string;
  platform: string;
  guild_id: string | null;
  chat_id: string | null;
  thread_id: string | null;
  enabled: boolean;
}

export interface ProfileDynamicBinding {
  source_binding_key: string;
  platform: string;
  scope: string;
  chat_id: string;
  agent_id: string;
  has_fallback_webhook: boolean;
  created_at: number;
  created_by_name: string | null;
  updated_at: number;
  updated_by_name: string | null;
}

export interface ProfileBindingSummary {
  profile: string;
  static: ProfileStaticRoute[];
  dynamic: ProfileDynamicBinding[];
}

/** Dashboard-side ``/agent use``: bind a whole chat to a profile, list bindable chats, unbind one row. */
export const profileBindingsApi = {
  summaries: () => fetchJSON<{ bindings: ProfileBindingSummary[] }>("/api/profiles/bindings"),
  chats: () => fetchJSON<{ chats: BindableChat[] }>("/api/profiles/binding-chats"),
  bind: (profile: string, chat: BindableChat) =>
    fetchJSON<{ ok: boolean; source_binding_key: string }>(
      `/api/profiles/${encodeURIComponent(profile)}/bindings`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: chat.platform, chat_id: chat.chat_id, chat_type: chat.chat_type, chat_name: chat.name }),
      },
    ),
  unbind: (sourceBindingKey: string) =>
    fetchJSON<{ ok: boolean }>(`/api/profiles/bindings?key=${encodeURIComponent(sourceBindingKey)}`, {
      method: "DELETE",
    }),
};
