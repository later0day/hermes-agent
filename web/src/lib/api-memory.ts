import { fetchJSON } from "@/lib/api";

export type MemoryDoc = "MEMORY.md" | "USER.md";

const memoryUrl = (profile: string, doc: MemoryDoc) =>
  `/api/profiles/${encodeURIComponent(profile)}/memory/${encodeURIComponent(doc)}`;

/** A profile's curated memory files, for the dashboard Memory page. */
export const memoryApi = {
  get: (profile: string, doc: MemoryDoc) =>
    fetchJSON<{ content: string; exists: boolean }>(memoryUrl(profile, doc)),
  update: (profile: string, doc: MemoryDoc, content: string) =>
    fetchJSON<{ ok: boolean }>(memoryUrl(profile, doc), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
};
