import { fetchJSON } from "@/lib/api";

/** A chat from the gateway's channel directory that can be bound to a profile. */
export interface BindableChat {
  platform: string;
  chat_id: string;
  name: string;
  chat_type: string;
}

/** Dashboard-side ``/agent use``: bind a whole chat to a profile, list bindable chats, unbind one row. */
export const profileBindingsApi = {
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
