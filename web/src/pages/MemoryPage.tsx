import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { Brain, Pencil, X, Save } from "lucide-react";
import { api, getManagementProfile } from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Segmented, FilterGroup } from "@nous-research/ui/ui/components/segmented";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { Markdown } from "@/components/Markdown";
import { PluginSlot } from "@/plugins";

type Tab = "memory" | "user" | "soul";

const TABS: { id: Tab; labelKey: keyof NonNullable<ReturnType<typeof useI18n>["t"]["memory"]> }[] = [
  { id: "memory", labelKey: "tabMemory" },
  { id: "user", labelKey: "tabUser" },
  { id: "soul", labelKey: "tabSoul" },
];

function useMemoryFile(tab: Tab) {
  const [content, setContent] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchContent = useCallback(() => {
    const profile = getManagementProfile();
    setLoading(true);
    setError(null);

    const promise =
      tab === "soul"
        ? api.getProfileSoul(profile).then((r) => r.content ?? "")
        : api
            .getProfileMemoryFile(profile, tab === "memory" ? "MEMORY.md" : "USER.md")
            .then((r) => r.content ?? "");

    promise
      .then((c) => setContent(c))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [tab]);

  useEffect(() => {
    fetchContent();
  }, [fetchContent]);

  return { content, setContent, loading, error, refresh: fetchContent };
}

export default function MemoryPage() {
  const { t } = useI18n();
  const { setAfterTitle } = usePageHeader();
  const { toast, showToast } = useToast();

  const [tab, setTab] = useState<Tab>("memory");
  const { content, setContent, loading, error } = useMemoryFile(tab);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  // When tab changes, exit edit mode
  useEffect(() => {
    setEditing(false);
    setDraft("");
  }, [tab]);

  const startEdit = useCallback(() => {
    setDraft(content);
    setEditing(true);
  }, [content]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setDraft("");
  }, []);

  const saveEdit = useCallback(async () => {
    const profile = getManagementProfile();
    setSaving(true);
    try {
      if (tab === "soul") {
        await api.updateProfileSoul(profile, draft);
      } else {
        await api.updateProfileMemoryFile(profile, tab === "memory" ? "MEMORY.md" : "USER.md", draft);
      }
      setContent(draft);
      setEditing(false);
      setDraft("");
      showToast(t.memory?.saveSuccess ?? "Saved ✓", "success");
    } catch (e) {
      showToast(`${t.memory?.saveFailed ?? "Save failed"}: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  }, [tab, draft, setContent, showToast, t.memory]);

  useLayoutEffect(() => {
    setAfterTitle(
      editing ? (
        <span className="flex items-center gap-1.5">
          <Button
            ghost
            size="sm"
            onClick={cancelEdit}
            className="text-muted-foreground hover:text-foreground gap-1"
          >
            <X className="h-3.5 w-3.5" />
            {t.memory?.cancel ?? "Cancel"}
          </Button>
          <Button
            size="sm"
            onClick={saveEdit}
            disabled={saving}
            className="gap-1"
          >
            {saving ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
            {t.memory?.save ?? "Save"}
          </Button>
        </span>
      ) : (
        <Button
          ghost
          size="sm"
          onClick={startEdit}
          disabled={loading}
          className="gap-1 text-muted-foreground hover:text-foreground"
        >
          <Pencil className="h-3.5 w-3.5" />
          {t.memory?.edit ?? "Edit"}
        </Button>
      ),
    );
    return () => setAfterTitle(null);
  }, [editing, loading, saving, startEdit, cancelEdit, saveEdit, setAfterTitle, t.memory]);

  const tabOptions = TABS.map((tb) => ({
    value: tb.id,
    label: t.memory?.[tb.labelKey] ?? tb.id,
  }));

  return (
    <div className="flex min-w-0 max-w-full flex-col gap-4">
      <PluginSlot name="memory:top" />
      <Toast toast={toast} />

      <div className="flex min-w-0 max-w-full flex-wrap items-center gap-3">
        <FilterGroup
          label=""
          className="flex min-w-0 w-full flex-col items-start gap-1.5 sm:w-auto sm:flex-row sm:items-center"
        >
          <Segmented
            className="w-fit max-w-full flex-wrap justify-start self-start"
            value={tab}
            onChange={(v) => setTab(v as Tab)}
            options={tabOptions}
          />
        </FilterGroup>
      </div>

      <Card className="min-w-0 max-w-full overflow-hidden">
        <CardHeader className="py-3 px-4">
          <CardTitle className="text-sm flex items-center gap-2">
            <Brain className="h-4 w-4" />
            {tab === "memory" ? "MEMORY.md" : tab === "user" ? "USER.md" : "SOUL.md"}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {error && (
            <div className="bg-destructive/10 border-b border-destructive/20 p-3">
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-16">
              <Spinner className="text-xl text-primary" />
            </div>
          ) : editing ? (
            <textarea
              className="w-full min-h-[60vh] resize-y p-4 font-mono-ui text-sm leading-6 bg-transparent border-0 outline-none focus:ring-0 text-foreground placeholder:text-muted-foreground"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t.memory?.empty ?? "No content yet. Start typing to add content."}
              autoFocus
            />
          ) : content ? (
            <div className="p-4 min-h-[200px] prose prose-sm max-w-none dark:prose-invert">
              <Markdown content={content} />
            </div>
          ) : (
            <p className="text-muted-foreground text-center py-16 text-sm">
              {t.memory?.empty ?? "No content yet."}
            </p>
          )}
        </CardContent>
      </Card>

      <PluginSlot name="memory:bottom" />
    </div>
  );
}
