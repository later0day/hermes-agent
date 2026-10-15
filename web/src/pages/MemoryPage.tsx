import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { Brain, RotateCw, Save, Undo2, User } from "lucide-react";
import { memoryApi, type MemoryDoc } from "@/lib/api-memory";
import { loadErrorCopy } from "@/lib/load-error-copy";
import { memoryCharCount, splitMemoryEntries } from "@/lib/memory-markdown";
import { readMemoryDraft, writeMemoryDraft } from "@/lib/memory-draft";
import { MemoryEntryList } from "@/components/memory/MemoryEntryList";
import { MemoryUsageBar } from "@/components/memory/MemoryUsageBar";
import { MemoryViewToggle, type MemoryView } from "@/components/memory/MemoryViewToggle";
import { useProfileScope } from "@/contexts/useProfileScope";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n, type Translations } from "@/i18n";
import { localeSection } from "@/i18n/section";
import { cn } from "@/lib/utils";
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

interface DocMeta {
  doc: MemoryDoc;
  icon: typeof Brain;
  title: string;
  // Fallback English strings; localized via the optional `memory` i18n section.
  descriptionFallback: string;
  placeholderFallback: string;
  defaultLimit: number; // tools/memory_tool_store.py defaults, until the API reports the profile's own
}

const DOCS: DocMeta[] = [
  {
    doc: "MEMORY.md",
    icon: Brain,
    title: "MEMORY.md",
    descriptionFallback: "The agent's own long-term notes and working memory.",
    placeholderFallback: "# What this agent should remember…",
    defaultLimit: 2200,
  },
  {
    doc: "USER.md",
    icon: User,
    title: "USER.md",
    descriptionFallback:
      "What the agent knows about you (preferences, facts, context).",
    placeholderFallback: "# What the agent knows about the user…",
    defaultLimit: 1375,
  },
];

/** Localized copy for one document. ``memory`` is an optional i18n section, so a locale that omits
 *  it falls back to the English literals (the per-doc ones baked into DocMeta). */
function memoryDocCopy(mem: Translations["memory"], meta: DocMeta) {
  const user = meta.doc === "USER.md";
  const fill = (template: string) => template.replace("{doc}", meta.doc);
  return {
    description: mem ? (user ? mem.userDescription : mem.memoryDescription) : meta.descriptionFallback,
    placeholder: mem ? (user ? mem.userPlaceholder : mem.memoryPlaceholder) : meta.placeholderFallback,
    emptyLabel: mem?.empty ?? "(empty)",
    unsavedLabel: mem?.unsavedChanges ?? "Unsaved changes",
    savedMsg: fill(mem?.saved ?? "{doc} saved"),
    failedMsg: fill(mem?.failedToSave ?? "Failed to save {doc}"),
  };
}

const VIEW_KEY = "hermes-memory-view";

function initialView(): MemoryView {
  const stored = localStorage.getItem(VIEW_KEY);
  if (stored === "edit" || stored === "preview" || stored === "split") return stored;
  return window.matchMedia?.("(min-width: 1024px)").matches ? "split" : "preview";
}

/* ------------------------------------------------------------------ */
/*  Single memory-document card                                        */
/* ------------------------------------------------------------------ */

function useMemoryDoc(profile: string, doc: MemoryDoc, onLoadError: () => void) {
  const [text, setText] = useState("");
  const [original, setOriginal] = useState("");
  const [loading, setLoading] = useState(true);
  const [exists, setExists] = useState(false);
  const [limit, setLimit] = useState<number | null>(null);
  const [draftRestored, setDraftRestored] = useState(false);

  const load = useCallback((keepDraft: boolean) => {
    let cancelled = false;
    memoryApi
      .get(profile, doc)
      .then((res) => {
        if (cancelled) return;
        const draft = keepDraft ? readMemoryDraft(profile, doc) : null;
        if (!keepDraft) writeMemoryDraft(profile, doc, null);
        setOriginal(res.content);
        setText(draft ?? res.content);
        setDraftRestored(draft !== null && draft !== res.content);
        setExists(res.exists);
        setLimit(res.char_limit ?? null);
      })
      .catch(() => !cancelled && onLoadError())
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [profile, doc, onLoadError]);

  useEffect(() => load(true), [load]);

  const edit = (next: string) => {
    setText(next);
    writeMemoryDraft(profile, doc, next === original ? null : next);
  };
  const reload = () => {
    setLoading(true);
    load(false);
  };
  const markSaved = () => {
    setOriginal(text);
    setExists(true);
    setDraftRestored(false);
    writeMemoryDraft(profile, doc, null);
  };
  return { text, original, loading, exists, limit, draftRestored, reload, edit, markSaved };
}

function MemoryEditor({
  meta,
  profile,
  view,
  showToast,
}: {
  meta: DocMeta;
  profile: string;
  view: MemoryView;
  showToast: (msg: string, kind: "success" | "error") => void;
}) {
  const { t } = useI18n();
  const ui = useMemo(() => localeSection(t, "memoryUi"), [t]);
  const copy = memoryDocCopy(t.memory, meta);
  const onLoadError = useCallback(
    () => showToast(loadErrorCopy(t.common, meta.doc).title, "error"), [showToast, t, meta.doc]);
  const state = useMemoryDoc(profile, meta.doc, onLoadError);
  const [saving, setSaving] = useState(false);
  const { text, loading } = state;
  const dirty = text !== state.original;
  const entries = useMemo(() => splitMemoryEntries(text), [text]);
  const Icon = meta.icon;

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await memoryApi.update(profile, meta.doc, text);
      state.markSaved();
      showToast(copy.savedMsg, "success");
    } catch {
      showToast(copy.failedMsg, "error");
    } finally {
      setSaving(false);
    }
  };

  const editor = (
    <div className="space-y-1">
      <Label htmlFor={`memory-editor-${meta.doc}`} className="sr-only">{meta.title}</Label>
      <textarea
        id={`memory-editor-${meta.doc}`}
        className={cn(
          "flex w-full resize-y border border-input bg-transparent px-3 py-2 text-sm font-mono leading-relaxed shadow-sm",
          "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          view === "split" ? "min-h-[460px]" : "min-h-[320px]",
        )}
        placeholder={copy.placeholder}
        value={text}
        onChange={(e) => state.edit(e.target.value)}
        spellCheck={false}
      />
    </div>
  );
  const preview = (
    <div className={cn(view === "split" && "max-h-[460px] overflow-y-auto pr-1")}>
      <MemoryEntryList entries={entries} emptyLabel={copy.emptyLabel} charsLabel={ui.entryChars} />
    </div>
  );

  return (
    <Card className="rounded-none">
      <CardHeader className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Icon className="h-4 w-4 text-muted-foreground" />
              {meta.title}
              {!loading && !state.exists && (
                <span className="text-xs font-normal text-muted-foreground">{copy.emptyLabel}</span>
              )}
            </CardTitle>
            <CardDescription className="text-xs">{copy.description}</CardDescription>
          </div>
          <Button ghost size="xs" className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={state.reload} disabled={loading || saving} title={ui.refresh} aria-label={ui.refresh}>
            <RotateCw />
          </Button>
        </div>
        {!loading && (
          <MemoryUsageBar used={memoryCharCount(text)} limit={state.limit ?? meta.defaultLimit}
            entries={entries.length} copy={ui} />
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {loading ? (
          <div className="flex min-h-[320px] items-center justify-center"><Spinner /></div>
        ) : (
          <>
            {state.draftRestored && dirty && <p className="text-xs text-warning">{ui.draftRestored}</p>}
            <div className={cn(view === "split" && "grid gap-3 lg:grid-cols-2")}>
              {view !== "preview" && editor}
              {view !== "edit" && preview}
            </div>
            {(view !== "preview" || dirty) && <div className="flex items-center justify-end gap-2">
              {dirty && <span className="text-xs text-warning">{copy.unsavedLabel}</span>}
              {dirty && (
                <Button ghost size="sm" prefix={<Undo2 />} onClick={() => state.edit(state.original)} disabled={saving}>
                  {ui.discard}
                </Button>
              )}
              <Button size="sm" className="uppercase" prefix={<Save />} onClick={handleSave} disabled={saving || !dirty}>
                {saving ? t.common.saving : t.common.save}
              </Button>
            </div>}
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
  const { t } = useI18n();
  const ui = useMemo(() => localeSection(t, "memoryUi"), [t]);
  const { toast, showToast } = useToast();
  const { setAfterTitle, setEnd } = usePageHeader();
  const { profile: selectedProfile } = useProfileScope();
  const [view, setView] = useState<MemoryView>(initialView);

  const scopeLabel = selectedProfile || "default";

  const chooseView = useCallback((next: MemoryView) => {
    setView(next);
    localStorage.setItem(VIEW_KEY, next);
  }, []);

  useLayoutEffect(() => {
    setAfterTitle(
      <span className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
        {scopeLabel}
      </span>,
    );
    setEnd(<MemoryViewToggle value={view} onChange={chooseView} copy={ui} />);
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [scopeLabel, setAfterTitle, setEnd, view, chooseView, ui]);

  return (
    <div className={cn("mx-auto w-full space-y-4 p-4", view === "split" ? "max-w-6xl" : "max-w-4xl")}>
      {DOCS.map((meta) => (
        <MemoryEditor
          // Remount per profile so state resets; an unsaved edit comes back from its session draft.
          key={`${scopeLabel}:${meta.doc}`}
          meta={meta}
          profile={scopeLabel}
          view={view}
          showToast={showToast}
        />
      ))}
      <Toast toast={toast} />
    </div>
  );
}
