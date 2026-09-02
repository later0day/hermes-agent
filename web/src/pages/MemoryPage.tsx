import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { Brain, Eye, Pencil, RotateCw, Save, User } from "lucide-react";
import { api } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { useProfileScope } from "@/contexts/useProfileScope";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Label } from "@nous-research/ui/ui/components/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";

/* ------------------------------------------------------------------ */
/*  Memory documents                                                   */
/* ------------------------------------------------------------------ */

type MemoryDoc = "MEMORY.md" | "USER.md";

interface DocMeta {
  doc: MemoryDoc;
  icon: typeof Brain;
  title: string;
  // Fallback English strings; localized via the optional `memory` i18n section.
  descriptionFallback: string;
  placeholderFallback: string;
}

const DOCS: DocMeta[] = [
  {
    doc: "MEMORY.md",
    icon: Brain,
    title: "MEMORY.md",
    descriptionFallback: "The agent's own long-term notes and working memory.",
    placeholderFallback: "# What this agent should remember…",
  },
  {
    doc: "USER.md",
    icon: User,
    title: "USER.md",
    descriptionFallback:
      "What the agent knows about you (preferences, facts, context).",
    placeholderFallback: "# What the agent knows about the user…",
  },
];

/* ------------------------------------------------------------------ */
/*  Single memory-document editor card                                 */
/* ------------------------------------------------------------------ */

function MemoryEditor({
  meta,
  profile,
  showToast,
}: {
  meta: DocMeta;
  profile: string;
  showToast: (msg: string, kind: "success" | "error") => void;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [original, setOriginal] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [exists, setExists] = useState(false);
  const [preview, setPreview] = useState(false);

  // Localized copy for this doc; `memory` is an optional i18n section, so full
  // locales that omit it fall back to the English literals baked into DocMeta.
  const mem = t.memory;
  const isUserDoc = meta.doc === "USER.md";
  const description = mem
    ? isUserDoc
      ? mem.userDescription
      : mem.memoryDescription
    : meta.descriptionFallback;
  const placeholder = mem
    ? isUserDoc
      ? mem.userPlaceholder
      : mem.memoryPlaceholder
    : meta.placeholderFallback;
  const emptyLabel = mem?.empty ?? "(empty)";
  const unsavedLabel = mem?.unsavedChanges ?? "Unsaved changes";
  const previewLabel = mem?.preview ?? "Preview";
  const editLabel = mem?.edit ?? "Edit";
  const savedMsg = (mem?.saved ?? "{doc} saved").replace("{doc}", meta.doc);
  const failedMsg = (mem?.failedToSave ?? "Failed to save {doc}").replace(
    "{doc}",
    meta.doc,
  );

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getProfileMemory(profile || "default", meta.doc)
      .then((res) => {
        if (cancelled) return;
        setText(res.content);
        setOriginal(res.content);
        setExists(res.exists);
      })
      .catch(() => !cancelled && showToast(t.common.loading, "error"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [profile, meta.doc, showToast, t]);

  useEffect(() => load(), [load]);

  const dirty = text !== original;
  const Icon = meta.icon;

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.updateProfileMemory(profile || "default", meta.doc, text);
      setOriginal(text);
      setExists(true);
      showToast(savedMsg, "success");
    } catch {
      showToast(failedMsg, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="rounded-none">
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Icon className="h-4 w-4 text-muted-foreground" />
            {meta.title}
            {!loading && !exists && (
              <span className="text-xs font-normal text-muted-foreground">
                {emptyLabel}
              </span>
            )}
          </CardTitle>
          <CardDescription className="text-xs">
            {description}
          </CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            ghost
            size="xs"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => setPreview((p) => !p)}
            disabled={loading}
            prefix={preview ? <Pencil /> : <Eye />}
            aria-label={preview ? editLabel : previewLabel}
          >
            {preview ? editLabel : previewLabel}
          </Button>
          <Button
            ghost
            size="xs"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => load()}
            disabled={loading || saving}
            aria-label={t.common.refresh}
          >
            <RotateCw />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading ? (
          <div className="flex min-h-[280px] items-center justify-center">
            <Spinner />
          </div>
        ) : preview ? (
          <div className="min-h-[280px] border border-input bg-transparent px-3 py-2">
            {text.trim() ? (
              <Markdown content={text} />
            ) : (
              <span className="text-sm text-muted-foreground">{emptyLabel}</span>
            )}
          </div>
        ) : (
          <>
            <Label htmlFor={`memory-editor-${meta.doc}`} className="sr-only">
              {meta.title}
            </Label>
            <textarea
              id={`memory-editor-${meta.doc}`}
              className="flex min-h-[280px] w-full border border-input bg-transparent px-3 py-2 text-sm font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder={placeholder}
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
            />
            <div className="flex items-center justify-end gap-2">
              {dirty && (
                <span className="text-xs text-muted-foreground">
                  {unsavedLabel}
                </span>
              )}
              <Button
                size="sm"
                className="uppercase"
                prefix={<Save />}
                onClick={handleSave}
                disabled={saving || !dirty}
              >
                {saving ? t.common.saving : t.common.save}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  MemoryPage — MEMORY.md + USER.md editors for the scoped profile    */
/* ------------------------------------------------------------------ */

export default function MemoryPage() {
  const { toast, showToast } = useToast();
  const { setAfterTitle, setEnd } = usePageHeader();
  const { profile: selectedProfile } = useProfileScope();

  const scopeLabel = useMemo(
    () => selectedProfile || "default",
    [selectedProfile],
  );

  useLayoutEffect(() => {
    setAfterTitle(
      <span className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
        {scopeLabel}
      </span>,
    );
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [scopeLabel, setAfterTitle, setEnd]);

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 p-4">
      {DOCS.map((meta) => (
        <MemoryEditor
          // Remount editors when the profile changes so state resets cleanly.
          key={`${scopeLabel}:${meta.doc}`}
          meta={meta}
          profile={selectedProfile}
          showToast={showToast}
        />
      ))}
      <Toast toast={toast} />
    </div>
  );
}
