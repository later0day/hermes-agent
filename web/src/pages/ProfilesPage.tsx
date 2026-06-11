import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  ChevronDown,
  Pencil,
  Plus,
  RefreshCw,
  Terminal,
  Trash2,
  Users,
  X,
} from "lucide-react";
import spinners from "unicode-animations";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { ProfileInfo } from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import type {
  AgentAuditEvent,
  ProfileDetails,
  SourceBindingInfo,
} from "@/lib/api";

// Mirrors hermes_cli/profiles.py::_PROFILE_ID_RE so we can reject obviously
// invalid names (uppercase, spaces, …) before round-tripping a doomed POST.
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
type MemoryFileName = "MEMORY.md" | "USER.md";

type MemoryEditorState = {
  profile: string;
  file: MemoryFileName;
  content: string;
  loading: boolean;
  saving: boolean;
};

type SkillEditorState = {
  profile: string;
  skill: string;
  content: string;
  loading: boolean;
  saving: boolean;
};

function formatUnixSeconds(ts?: number | null): string {
  if (!ts) return "-";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function formatEpochMs(ts?: number | null): string {
  if (!ts) return "-";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function bindingTarget(binding: SourceBindingInfo) {
  const target = binding.target_summary;
  const fallback = binding.fallback_target ?? {};
  const read = (key: string) =>
    typeof fallback[key] === "string" ? String(fallback[key]) : "";
  return {
    platform: target?.platform || read("platform") || "unknown",
    label: target?.label || read("chat_name") || read("chat_id") || binding.source_binding_key,
    scope: target?.scope || read("chat_type") || "source",
    chatId: target?.chat_id || read("chat_id") || "",
  };
}

function webhookTone(binding: SourceBindingInfo): "success" | "destructive" | "outline" | "warning" {
  const status = binding.webhook_status;
  if (!status?.configured) return "outline";
  if (status.expired || status.state === "expired") return "destructive";
  if (status.kind === "temporary") return "warning";
  return "success";
}

function webhookLabel(binding: SourceBindingInfo): string {
  const status = binding.webhook_status;
  if (!status?.configured) return "未配置";
  if (status.expired || status.state === "expired") return "已过期";
  if (status.kind === "temporary") return "已配置 · 临时";
  return "已配置 · 长期";
}

function webhookNote(binding: SourceBindingInfo): string {
  const status = binding.webhook_status;
  if (!status?.configured) return "需要在 IM 中执行 /agent webhook";
  if (status.kind === "temporary" && status.expires_at) {
    return `有效至 ${formatEpochMs(status.expires_at)}`;
  }
  return "token 已脱敏";
}

function profileWebhookSummary(profile: ProfileInfo): string {
  const summary = profile.binding_summary;
  if (!summary || summary.total === 0) return "Webhook: 0/0";
  const expired = summary.webhook_expired > 0 ? `, ${summary.webhook_expired} expired` : "";
  return `Webhook: ${summary.webhook_configured}/${summary.total}${expired}`;
}

/** Braille unicode spinner (`unicode-animations`); static first frame when reduced motion is preferred. */
function ProfilesLoadingSpinner() {
  const { frames, interval } = spinners.braille;
  const [frameIndex, setFrameIndex] = useState(0);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }
    const id = window.setInterval(
      () => setFrameIndex((i) => (i + 1) % frames.length),
      interval,
    );
    return () => window.clearInterval(id);
  }, [frames.length, interval]);

  return (
    <span
      aria-hidden
      className="inline-block select-none font-mono text-xl leading-none text-muted-foreground"
    >
      {frames[frameIndex]}
    </span>
  );
}

function ProfileManagementModal({
  children,
  description,
  onClose,
  title,
  wide = false,
}: {
  children: ReactNode;
  description?: string;
  onClose: () => void;
  title: string;
  wide?: boolean;
}) {
  const modalRef = useModalBehavior({ open: true, onClose });

  return createPortal(
    <div
      ref={modalRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="profile-management-modal-title"
    >
      <div
        className={
          "relative flex max-h-[86vh] w-full flex-col border border-border bg-card shadow-2xl " +
          (wide ? "max-w-6xl" : "max-w-3xl")
        }
      >
        <Button
          ghost
          size="icon"
          onClick={onClose}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          aria-label="Close"
        >
          <X />
        </Button>
        <header className="border-b border-border p-5 pb-3 pr-12">
          <h2
            id="profile-management-modal-title"
            className="font-display text-base uppercase tracking-wider"
          >
            {title}
          </h2>
          {description && (
            <p className="mt-1 text-xs normal-case tracking-normal text-muted-foreground">
              {description}
            </p>
          )}
        </header>
        <div className="min-h-0 flex-1 overflow-auto p-5">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailsOpenFor, setDetailsOpenFor] = useState<string | null>(null);
  const [detailsByProfile, setDetailsByProfile] = useState<Record<string, ProfileDetails>>({});
  const [detailsLoadingFor, setDetailsLoadingFor] = useState<string | null>(null);
  const [modelPickerFor, setModelPickerFor] = useState<string | null>(null);
  const { toast, showToast } = useToast();
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  // Create modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [cloneFromDefault, setCloneFromDefault] = useState(true);
  const [cloneSource, setCloneSource] = useState("default");
  const [creating, setCreating] = useState(false);
  const [profileAction, setProfileAction] = useState<string | null>(null);
  const [skillPickerFor, setSkillPickerFor] = useState<string | null>(null);
  const [defaultSkills, setDefaultSkills] = useState<string[]>([]);
  const [defaultSkillsLoading, setDefaultSkillsLoading] = useState(false);
  const [skillSearchByProfile, setSkillSearchByProfile] = useState<Record<string, string>>({});
  const [selectedSkillsByProfile, setSelectedSkillsByProfile] = useState<Record<string, string[]>>({});
  const [auditModalOpen, setAuditModalOpen] = useState(false);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const [templateCandidate, setTemplateCandidate] = useState("default");
  const [templateDraftByProfile, setTemplateDraftByProfile] = useState<Record<string, string>>({});
  const [auditFilter, setAuditFilter] = useState("__all__");
  const [auditLimit, setAuditLimit] = useState(25);
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditEvents, setAuditEvents] = useState<AgentAuditEvent[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditExpandedKey, setAuditExpandedKey] = useState<string | null>(null);
  const [memoryEditor, setMemoryEditor] = useState<MemoryEditorState | null>(null);
  const [skillEditor, setSkillEditor] = useState<SkillEditorState | null>(null);
  const closeCreateModal = useCallback(() => setCreateModalOpen(false), []);
  const createModalRef = useModalBehavior({
    open: createModalOpen,
    onClose: closeCreateModal,
  });

  // Inline rename state
  const [renamingFrom, setRenamingFrom] = useState<string | null>(null);
  const [renameTo, setRenameTo] = useState("");

  // Inline SOUL editor state
  const [editingSoulFor, setEditingSoulFor] = useState<string | null>(null);
  const [soulText, setSoulText] = useState("");
  const [soulSaving, setSoulSaving] = useState(false);
  // Tracks the latest SOUL request so out-of-order responses don't overwrite
  // newer state when the user switches profiles or closes the editor.
  const activeSoulRequest = useRef<string | null>(null);

  const load = useCallback(() => {
    api
      .getProfiles()
      .then((res) => setProfiles(res.profiles))
      .catch((e) => showToast(`${t.status.error}: ${e}`, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t.status.error]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (profiles.length === 0) return;
    if (!profiles.some((profile) => profile.name === templateCandidate)) {
      setTemplateCandidate(profiles[0].name);
    }
  }, [profiles, templateCandidate]);

  const loadAudit = useCallback(
    async (nextOffset: number) => {
      setAuditLoading(true);
      try {
        const profile = auditFilter === "__all__" ? undefined : auditFilter;
        const result = await api.getAgentAudit(profile, auditLimit, nextOffset);
        setAuditEvents(result.events);
        setAuditOffset(result.offset);
      } catch (e) {
        showToast(`${t.status.error}: ${e}`, "error");
      } finally {
        setAuditLoading(false);
      }
    },
    [auditFilter, auditLimit, showToast, t.status.error],
  );

  useEffect(() => {
    if (auditModalOpen) loadAudit(0);
  }, [auditModalOpen, loadAudit]);

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      showToast(t.profiles.nameRequired, "error");
      return;
    }
    if (!PROFILE_NAME_RE.test(name)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    setCreating(true);
    try {
      await api.createProfile({
        name,
        clone_from_default: cloneFromDefault && cloneSource === "default",
        clone_from: cloneFromDefault && cloneSource !== "default" ? cloneSource : undefined,
        no_skills: true,
      });
      showToast(`${t.profiles.created}: ${name}`, "success");
      setNewName("");
      setCloneSource("default");
      setCreateModalOpen(false);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const handleRenameSubmit = async () => {
    if (!renamingFrom) return;
    const target = renameTo.trim();
    if (!target || target === renamingFrom) {
      setRenamingFrom(null);
      setRenameTo("");
      return;
    }
    if (!PROFILE_NAME_RE.test(target)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    try {
      await api.renameProfile(renamingFrom, target);
      showToast(
        `${t.profiles.renamed}: ${renamingFrom} → ${target}`,
        "success",
      );
      setRenamingFrom(null);
      setRenameTo("");
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const openSoulEditor = useCallback(
    async (name: string) => {
      if (editingSoulFor === name) {
        activeSoulRequest.current = null;
        setEditingSoulFor(null);
        return;
      }
      setEditingSoulFor(name);
      setSoulText("");
      activeSoulRequest.current = name;
      try {
        const soul = await api.getProfileSoul(name);
        if (activeSoulRequest.current === name) {
          setSoulText(soul.content);
        }
      } catch (e) {
        if (activeSoulRequest.current === name) {
          showToast(`${t.status.error}: ${e}`, "error");
        }
      }
    },
    [editingSoulFor, showToast, t.status.error],
  );

  const handleSaveSoul = async (name: string) => {
    setSoulSaving(true);
    try {
      await api.updateProfileSoul(name, soulText);
      showToast(`${t.profiles.soulSaved}: ${name}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setSoulSaving(false);
    }
  };

  const openDetails = useCallback(
    async (name: string) => {
      if (detailsOpenFor === name) {
        setDetailsOpenFor(null);
        return;
      }
      setDetailsOpenFor(name);
      if (detailsByProfile[name]) return;
      setDetailsLoadingFor(name);
      try {
        const details = await api.getProfileDetails(name);
        setDetailsByProfile((prev) => ({ ...prev, [name]: details }));
      } catch (e) {
        showToast(`${t.status.error}: ${e}`, "error");
      } finally {
        setDetailsLoadingFor(null);
      }
    },
    [detailsByProfile, detailsOpenFor, showToast, t.status.error],
  );

  const refreshProfileDetails = useCallback(async (name: string) => {
    const details = await api.getProfileDetails(name);
    setDetailsByProfile((prev) => ({ ...prev, [name]: details }));
  }, []);

  const handleToggleTemplate = async (name: string, next: boolean) => {
    setProfileAction(`template:${name}`);
    try {
      await api.setProfileTemplate(name, next);
      showToast(`${name}: template ${next ? "enabled" : "disabled"}`, "success");
      await refreshProfileDetails(name);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setProfileAction(null);
    }
  };

  const handleSaveProfileMetadata = async (
    name: string,
    metadata: { description?: string; description_auto?: boolean; template?: boolean },
  ) => {
    setProfileAction(`metadata:${name}`);
    try {
      await api.updateProfileMetadata(name, metadata);
      showToast(`${name}: metadata saved`, "success");
      await refreshProfileDetails(name);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setProfileAction(null);
    }
  };

  const openCreateFromTemplate = (name: string) => {
    setCloneFromDefault(true);
    setCloneSource(name);
    setNewName("");
    setCreateModalOpen(true);
  };

  const handleAutoDescribe = async (name: string) => {
    setProfileAction(`describe:${name}`);
    try {
      const result = await api.describeProfile(name, false);
      if (!result.ok) {
        showToast(`${name}: ${result.reason || "description not changed"}`, "error");
        return;
      }
      showToast(`${name}: description updated`, "success");
      await refreshProfileDetails(name);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setProfileAction(null);
    }
  };

  const ensureDefaultSkills = useCallback(async () => {
    if (defaultSkills.length > 0) return defaultSkills;
    setDefaultSkillsLoading(true);
    try {
      const result = await api.getProfileSkills("default");
      setDefaultSkills(result.skills.names);
      return result.skills.names;
    } finally {
      setDefaultSkillsLoading(false);
    }
  }, [defaultSkills]);

  const handleOpenSkillPicker = async (name: string) => {
    if (skillPickerFor === name) {
      setSkillPickerFor(null);
      return;
    }
    setSkillPickerFor(name);
    try {
      await ensureDefaultSkills();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const toggleSkillSelection = (profile: string, skill: string, checked: boolean) => {
    setSelectedSkillsByProfile((prev) => {
      const next = new Set(prev[profile] ?? []);
      if (checked) next.add(skill);
      else next.delete(skill);
      return { ...prev, [profile]: [...next].sort() };
    });
  };

  const handleCopySkills = async (name: string, sourceProfile = "default", skills?: string[]) => {
    const selected = skills ?? selectedSkillsByProfile[name] ?? [];
    if (selected.length === 0) {
      showToast("Select at least one skill to copy.", "error");
      return;
    }
    setProfileAction(`skills:${name}`);
    try {
      const result = await api.copyProfileSkills(name, sourceProfile, selected);
      showToast(
        `Copied ${result.copied_skills.length} skill(s) from ${result.source_profile}: ${result.skills.count} installed`,
        "success",
      );
      setSelectedSkillsByProfile((prev) => ({ ...prev, [name]: [] }));
      await refreshProfileDetails(name);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setProfileAction(null);
    }
  };

  const openMemoryEditor = async (profile: string, file: MemoryFileName) => {
    if (memoryEditor?.profile === profile && memoryEditor.file === file) {
      setMemoryEditor(null);
      return;
    }
    setMemoryEditor({ profile, file, content: "", loading: true, saving: false });
    try {
      const result = await api.getProfileMemoryFile(profile, file);
      setMemoryEditor({
        profile,
        file,
        content: result.content,
        loading: false,
        saving: false,
      });
    } catch (e) {
      setMemoryEditor(null);
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const handleSaveMemory = async () => {
    if (!memoryEditor) return;
    const current = memoryEditor;
    setMemoryEditor({ ...current, saving: true });
    try {
      await api.updateProfileMemoryFile(current.profile, current.file, current.content);
      showToast(`${current.profile}: ${current.file} saved`, "success");
      await refreshProfileDetails(current.profile);
      await loadAudit(0);
      setMemoryEditor({ ...current, saving: false });
    } catch (e) {
      setMemoryEditor({ ...current, saving: false });
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const openSkillEditor = async (profile: string, skill: string) => {
    if (skillEditor?.profile === profile && skillEditor.skill === skill) {
      setSkillEditor(null);
      return;
    }
    setSkillEditor({ profile, skill, content: "", loading: true, saving: false });
    try {
      const result = await api.getProfileSkillManifest(profile, skill);
      setSkillEditor({
        profile,
        skill,
        content: result.content,
        loading: false,
        saving: false,
      });
    } catch (e) {
      setSkillEditor(null);
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const handleSaveSkillManifest = async () => {
    if (!skillEditor) return;
    const current = skillEditor;
    setSkillEditor({ ...current, saving: true });
    try {
      await api.updateProfileSkillManifest(current.profile, current.skill, current.content);
      showToast(`${current.profile}: ${current.skill} saved`, "success");
      await refreshProfileDetails(current.profile);
      await loadAudit(0);
      setSkillEditor({ ...current, saving: false });
    } catch (e) {
      setSkillEditor({ ...current, saving: false });
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const handleDeleteProfileSkill = async (name: string, skill: string) => {
    if (!window.confirm(`删除 ${name} 里的 skill「${skill}」？只会影响这个 Agent。`)) {
      return;
    }
    setProfileAction(`skill-delete:${name}:${skill}`);
    try {
      const result = await api.deleteProfileSkill(name, skill);
      showToast(`已删除 ${result.deleted_skill}: ${result.skills.count} installed`, "success");
      if (skillEditor?.profile === name && skillEditor.skill === skill) {
        setSkillEditor(null);
      }
      await refreshProfileDetails(name);
      load();
      await loadAudit(0);
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setProfileAction(null);
    }
  };

  const handleCopyTerminalCommand = async (name: string) => {
    let cmd: string;
    try {
      const res = await api.getProfileSetupCommand(name);
      cmd = res.command;
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(cmd);
      showToast(`${t.profiles.commandCopied}: ${cmd}`, "success");
    } catch {
      showToast(`${t.profiles.copyFailed}: ${cmd}`, "error");
    }
  };

  const profileDelete = useConfirmDelete<string>({
    onDelete: useCallback(
      async (name: string) => {
        try {
          await api.deleteProfile(name);
          showToast(`${t.profiles.deleted}: ${name}`, "success");
          load();
          await loadAudit(0);
        } catch (e) {
          showToast(`${t.status.error}: ${e}`, "error");
          throw e;
        }
      },
      [load, loadAudit, showToast, t.profiles.deleted, t.status.error],
    ),
  });

  const pendingName = profileDelete.pendingId;

  // Keep heavyweight management surfaces behind explicit actions so the
  // profile list remains the primary view.
  useLayoutEffect(() => {
    setEnd(
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button size="sm" outlined onClick={() => setAuditModalOpen(true)}>
          Audit
        </Button>
        <Button size="sm" outlined onClick={() => setTemplateModalOpen(true)}>
          Templates
        </Button>
        <Button size="sm" onClick={() => setCreateModalOpen(true)}>
          <Plus className="h-3 w-3" />
          {t.common.create}
        </Button>
      </div>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t.common.create, loading]);

  if (loading) {
    return (
      <div
        aria-busy="true"
        aria-live="polite"
        className="flex items-center justify-center py-24"
      >
        <span className="sr-only">{t.common.loading}</span>

        <ProfilesLoadingSpinner />
      </div>
    );
  }

  const templates = profiles.filter((profile) => profile.template);
  const candidateProfiles = profiles.filter((profile) => !profile.template);
  const selectedTemplateCandidate =
    candidateProfiles.find((profile) => profile.name === templateCandidate) ??
    candidateProfiles[0] ??
    profiles[0];

  return (
    <div className="flex flex-col gap-6 normal-case">
      <Toast toast={toast} />

      <DeleteConfirmDialog
        open={profileDelete.isOpen}
        onCancel={profileDelete.cancel}
        onConfirm={profileDelete.confirm}
        title={t.profiles.confirmDeleteTitle}
        description={
          pendingName
            ? t.profiles.confirmDeleteMessage.replace("{name}", pendingName)
            : t.profiles.confirmDeleteMessage
        }
        loading={profileDelete.isDeleting}
      />

      {/* Create profile modal */}
      {createModalOpen && (
        <div
          ref={createModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
          onClick={(e) =>
            e.target === e.currentTarget && setCreateModalOpen(false)
          }
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-profile-title"
        >
          <div className="relative flex w-full max-w-md flex-col border border-border bg-card shadow-2xl">
            <Button
              ghost
              size="icon"
              onClick={() => setCreateModalOpen(false)}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="create-profile-title"
                className="font-display text-base uppercase tracking-wider"
              >
                {t.profiles.newProfile}
              </h2>
            </header>

            <div className="p-5 grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="profile-name">{t.profiles.name}</Label>
                <Input
                  id="profile-name"
                  autoFocus
                  placeholder={t.profiles.namePlaceholder}
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                  }}
                  aria-invalid={
                    newName.trim() !== "" &&
                    !PROFILE_NAME_RE.test(newName.trim())
                  }
                />
                <p className="text-xs text-muted-foreground">
                  {t.profiles.nameRule}
                </p>
              </div>

              <div className="flex items-center gap-2.5">
                <Checkbox
                  checked={cloneFromDefault}
                  id="clone-from-default"
                  onCheckedChange={(checked) =>
                    setCloneFromDefault(checked === true)
                  }
                />

                <Label
                  className="font-sans normal-case tracking-normal text-sm cursor-pointer"
                  htmlFor="clone-from-default"
                >
                  {t.profiles.cloneFromDefault}
                </Label>
              </div>

              {cloneFromDefault && (
                <div className="grid gap-2">
                  <Label htmlFor="clone-source">克隆来源</Label>
                  <Select
                    id="clone-source"
                    value={cloneSource}
                    onValueChange={setCloneSource}
                  >
                    {profiles.map((profile) => (
                      <SelectOption key={profile.name} value={profile.name}>
                        {profile.name}
                        {profile.template ? " (template)" : ""}
                      </SelectOption>
                    ))}
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    只复制 config、SOUL 和必要的记忆文件；新 Agent 默认不携带 skills，也不会复制 .env 密钥。需要技能时，在详情里的 Skills 区域从 default 多选复制。
                  </p>
                </div>
              )}

              <div className="flex justify-end">
                <Button
                  size="sm"
                  onClick={handleCreate}
                  disabled={creating}
                >
                  <Plus className="h-3 w-3" />
                  {creating ? t.common.creating : t.common.create}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {auditModalOpen && (
        <ProfileManagementModal
          title="Audit log"
          description="Full paginated audit feed; values are served through the existing redaction helper."
          onClose={() => setAuditModalOpen(false)}
          wide
        >
          <div className="grid gap-3">
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_8rem_auto]">
              <Select
                value={auditFilter}
                onValueChange={(value) => {
                  setAuditFilter(value);
                  setAuditExpandedKey(null);
                }}
              >
                <SelectOption value="__all__">All profiles</SelectOption>
                {profiles.map((profile) => (
                  <SelectOption key={profile.name} value={profile.name}>
                    {profile.name}
                  </SelectOption>
                ))}
              </Select>
              <Select
                value={String(auditLimit)}
                onValueChange={(value) => setAuditLimit(Number(value))}
              >
                <SelectOption value="10">10 rows</SelectOption>
                <SelectOption value="25">25 rows</SelectOption>
                <SelectOption value="50">50 rows</SelectOption>
                <SelectOption value="100">100 rows</SelectOption>
              </Select>
              <Button
                size="sm"
                outlined
                disabled={auditLoading}
                onClick={() => loadAudit(auditOffset)}
              >
                <RefreshCw className="h-3 w-3" />
                {auditLoading ? "Loading" : "Refresh"}
              </Button>
            </div>

            <div className="max-h-[56vh] overflow-auto border border-border/60 bg-muted/10">
              {auditEvents.length === 0 ? (
                <div className="p-3 text-sm text-muted-foreground">
                  {auditLoading ? "Loading audit events..." : "No audit events found."}
                </div>
              ) : (
                <div className="divide-y divide-border/60">
                  {auditEvents.map((event, index) => {
                    const key = `${event.ts || "no-ts"}:${event.action || "event"}:${auditOffset + index}`;
                    const expanded = auditExpandedKey === key;
                    return (
                      <div key={key} className="grid gap-2 p-3 text-xs">
                        <div className="grid gap-2 md:grid-cols-[9.5rem_1fr_9rem_7rem_auto] md:items-center">
                          <span className="font-mono text-muted-foreground">
                            {event.ts || "(no timestamp)"}
                          </span>
                          <span className="font-semibold text-foreground">
                            {event.action || "(unknown action)"}
                          </span>
                          <span className="truncate font-mono">
                            {event.profile_name || "(global)"}
                          </span>
                          <span className="truncate">
                            {String(event.actor_user_name || event.actor_user_id || "unknown")}
                          </span>
                          <Button
                            size="sm"
                            outlined
                            onClick={() => setAuditExpandedKey(expanded ? null : key)}
                          >
                            {expanded ? "Hide" : "View"}
                          </Button>
                        </div>
                        {expanded && (
                          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words border border-border/60 bg-background/50 p-2 font-mono text-[11px] text-muted-foreground">
                            {JSON.stringify(
                              {
                                source: event.source ?? null,
                                before: event.before ?? null,
                                after: event.after ?? null,
                                extra: event.extra ?? null,
                              },
                              null,
                              2,
                            )}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>
                Offset {auditOffset}; showing {auditEvents.length}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  outlined
                  disabled={auditLoading || auditOffset === 0}
                  onClick={() => loadAudit(Math.max(0, auditOffset - auditLimit))}
                >
                  Prev
                </Button>
                <Button
                  size="sm"
                  outlined
                  disabled={auditLoading || auditEvents.length < auditLimit}
                  onClick={() => loadAudit(auditOffset + auditLimit)}
                >
                  Next
                </Button>
              </div>
            </div>
          </div>
        </ProfileManagementModal>
      )}

      {templateModalOpen && (
        <ProfileManagementModal
          title="Template manager"
          description="Templates are normal profiles with metadata; creating from one still skips .env and skills by default."
          onClose={() => setTemplateModalOpen(false)}
          wide
        >
          <div className="grid gap-3">
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
              <Select value={selectedTemplateCandidate?.name ?? "default"} onValueChange={setTemplateCandidate}>
                {candidateProfiles.length === 0 ? (
                  <SelectOption value={selectedTemplateCandidate?.name ?? "default"}>
                    {selectedTemplateCandidate?.name ?? "default"}
                  </SelectOption>
                ) : (
                  candidateProfiles.map((profile) => (
                    <SelectOption key={profile.name} value={profile.name}>
                      {profile.name}
                    </SelectOption>
                  ))
                )}
              </Select>
              <Button
                size="sm"
                outlined
                disabled={
                  !selectedTemplateCandidate ||
                  selectedTemplateCandidate.template ||
                  profileAction === `template:${selectedTemplateCandidate.name}`
                }
                onClick={() =>
                  selectedTemplateCandidate &&
                  handleToggleTemplate(selectedTemplateCandidate.name, true)
                }
              >
                Mark template
              </Button>
            </div>

            <div className="max-h-[56vh] overflow-auto border border-border/60 bg-muted/10">
              {templates.length === 0 ? (
                <div className="p-3 text-sm text-muted-foreground">
                  No templates yet. Mark an existing profile as a template first.
                </div>
              ) : (
                <div className="divide-y divide-border/60">
                  {templates.map((template) => {
                    const draft =
                      templateDraftByProfile[template.name] ??
                      template.description ??
                      "";
                    return (
                      <div key={template.name} className="grid gap-2 p-3 text-xs">
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="truncate font-semibold text-foreground">
                              {template.name}
                            </div>
                            <div className="truncate text-muted-foreground">
                              {template.model || "(model unset)"}
                              {template.provider ? ` (${template.provider})` : ""}
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <Button
                              size="sm"
                              outlined
                              onClick={() => openCreateFromTemplate(template.name)}
                            >
                              Use
                            </Button>
                            <Button
                              size="sm"
                              outlined
                              disabled={profileAction === `template:${template.name}`}
                              onClick={() => handleToggleTemplate(template.name, false)}
                            >
                              Unset
                            </Button>
                          </div>
                        </div>
                        <textarea
                          className="min-h-[5.5rem] w-full border border-input bg-background/40 px-2 py-1 font-mono text-[11px] text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          placeholder="Template description for routing and clone decisions."
                          value={draft}
                          onChange={(e) =>
                            setTemplateDraftByProfile((prev) => ({
                              ...prev,
                              [template.name]: e.target.value,
                            }))
                          }
                        />
                        <div className="flex justify-end">
                          <Button
                            size="sm"
                            disabled={profileAction === `metadata:${template.name}`}
                            onClick={() =>
                              handleSaveProfileMetadata(template.name, {
                                description: draft,
                                description_auto: false,
                                template: true,
                              })
                            }
                          >
                            Save metadata
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </ProfileManagementModal>
      )}

      {/* List */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Users className="h-4 w-4" />
          {t.profiles.allProfiles} ({profiles.length})
        </H2>

        {profiles.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {t.profiles.noProfiles}
            </CardContent>
          </Card>
        )}

        {profiles.map((p) => {
          const isRenaming = renamingFrom === p.name;
          const isEditingSoul = editingSoulFor === p.name;
          const details = detailsByProfile[p.name];
          const detailSkills = details?.skills.names ?? [];
          const detailMemoryPreviews = details?.memory.previews ?? [];
          const detailAuditEvents = details?.audit.events ?? [];
          const detailBindings = details?.bindings ?? [];
          const detailWebhookConfigured = detailBindings.filter(
            (binding) => binding.webhook_status?.configured,
          ).length;
          const detailWebhookExpired = detailBindings.filter(
            (binding) => binding.webhook_status?.expired,
          ).length;
          const editableMemoryPreviews = detailMemoryPreviews.filter(
            (preview) => preview.name === "MEMORY.md" || preview.name === "USER.md",
          );
          return (
            <Card key={p.name}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    {isRenaming ? (
                      <Input
                        autoFocus
                        value={renameTo}
                        onChange={(e) => setRenameTo(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRenameSubmit();
                          if (e.key === "Escape") setRenamingFrom(null);
                        }}
                        aria-invalid={
                          renameTo.trim() !== "" &&
                          renameTo.trim() !== p.name &&
                          !PROFILE_NAME_RE.test(renameTo.trim())
                        }
                        className="max-w-xs"
                      />
                    ) : (
                      <span className="font-medium text-sm truncate">
                        {p.name}
                      </span>
                    )}
                    {p.is_default && (
                      <Badge tone="secondary">{t.profiles.defaultBadge}</Badge>
                    )}
                    {p.has_env && (
                      <Badge tone="outline">{t.profiles.hasEnv}</Badge>
                    )}
                    {p.template && (
                      <Badge tone="outline">template</Badge>
                    )}
                  </div>
                  {isRenaming &&
                    (() => {
                      const trimmed = renameTo.trim();
                      const invalid =
                        trimmed !== "" &&
                        trimmed !== p.name &&
                        !PROFILE_NAME_RE.test(trimmed);
                      return (
                        <p
                          className={
                            "text-xs mb-1 " +
                            (invalid
                              ? "text-destructive"
                              : "text-muted-foreground")
                          }
                        >
                          {invalid
                            ? `${t.profiles.invalidName}: ${t.profiles.nameRule}`
                            : t.profiles.nameRule}
                        </p>
                      );
                    })()}
                  <div className="flex items-center gap-4 text-xs text-muted-foreground flex-wrap">
                    {p.model && (
                      <span>
                        {t.profiles.model}: {p.model}
                        {p.provider ? ` (${p.provider})` : ""}
                      </span>
                    )}
                    <span>
                      {t.profiles.skills}: {p.skill_count}
                    </span>
                    <span>
                      IM: {p.binding_count}
                    </span>
                    {p.binding_count > 0 && (
                      <span>
                        {profileWebhookSummary(p)}
                      </span>
                    )}
                    <span className="font-mono truncate max-w-[28rem]">
                      {p.path}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {isRenaming ? (
                    <>
                      <Button size="sm" onClick={handleRenameSubmit}>
                        {t.common.save}
                      </Button>
                      <Button
                        size="sm"
                        ghost
                        onClick={() => setRenamingFrom(null)}
                      >
                        {t.common.cancel}
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        ghost
                        size="icon"
                        title="Details"
                        aria-label="Details"
                        onClick={() => openDetails(p.name)}
                      >
                        <ChevronDown className="h-4 w-4" />
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        title="Change model"
                        aria-label="Change model"
                        onClick={() => setModelPickerFor(p.name)}
                      >
                        <span aria-hidden className="text-xs font-bold">
                          M
                        </span>
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        title={t.profiles.editSoul}
                        aria-label={t.profiles.editSoul}
                        onClick={() => openSoulEditor(p.name)}
                      >
                        {isEditingSoul ? (
                          <ChevronDown className="h-4 w-4" />
                        ) : (
                          <span aria-hidden className="text-xs font-bold">
                            S
                          </span>
                        )}
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        title={t.profiles.openInTerminal}
                        aria-label={t.profiles.openInTerminal}
                        onClick={() => handleCopyTerminalCommand(p.name)}
                      >
                        <Terminal className="h-4 w-4" />
                      </Button>
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.profiles.rename}
                          aria-label={t.profiles.rename}
                          onClick={() => {
                            setRenamingFrom(p.name);
                            setRenameTo(p.name);
                          }}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                      )}
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.common.delete}
                          aria-label={t.common.delete}
                          onClick={() => profileDelete.requestDelete(p.name)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </CardContent>

              {detailsOpenFor === p.name && (
                <div className="border-t border-border px-4 pb-4 pt-3">
                  {detailsLoadingFor === p.name && (
                    <div className="py-4 text-sm text-muted-foreground">
                      {t.common.loading}
                    </div>
                  )}
                  {details && (
                    <div className="grid gap-3 text-xs text-muted-foreground md:grid-cols-2">
                      <div className="border border-border/60 bg-muted/10 p-3">
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <span className="text-[10px] font-semibold uppercase tracking-wider text-foreground">
                            Summary
                          </span>
                          <div className="flex items-center gap-1">
                            <Button
                              size="sm"
                              outlined
                              disabled={profileAction === `describe:${p.name}`}
                              onClick={() => handleAutoDescribe(p.name)}
                            >
                              Auto describe
                            </Button>
                            <Button
                              size="sm"
                              outlined
                              disabled={profileAction === `template:${p.name}`}
                              onClick={() => handleToggleTemplate(p.name, !details.profile.template)}
                            >
                              {details.profile.template ? "Unset template" : "Mark template"}
                            </Button>
                          </div>
                        </div>
                        <div>Model: {details.model.model || "(unset)"}</div>
                        <div>Provider: {details.model.provider || "(unset)"}</div>
                        <div>Description: {details.profile.description || "(unset)"}</div>
                        <div>Health: {details.health.status}</div>
                        <div>Bindings: {detailBindings.length}</div>
                        <div>
                          Webhook: {detailWebhookConfigured}/{detailBindings.length}
                          {detailWebhookExpired > 0 ? ` (${detailWebhookExpired} expired)` : ""}
                        </div>
                        <div>Template: {details.profile.template ? "yes" : "no"}</div>
                      </div>

                      <div className="border border-border/60 bg-muted/10 p-3">
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-foreground">
                          Kanban / Cron
                        </div>
                        <div>Kanban active: {details.kanban.active}</div>
                        <div>Kanban total: {details.kanban.total}</div>
                        <div>Cron owner jobs: {details.cron.owner_job_count}</div>
                        <div className="mt-2 truncate font-mono">workspace: {details.paths.workspace}</div>
                        <div className="truncate font-mono">scripts: {details.paths.scripts}</div>
                      </div>

                      <div className="border border-border/60 bg-muted/10 p-3">
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <span className="text-[10px] font-semibold uppercase tracking-wider text-foreground">
                            Skills
                          </span>
                          {!p.is_default && (
                            <Button
                              size="sm"
                              outlined
                              disabled={defaultSkillsLoading && skillPickerFor === p.name}
                              onClick={() => handleOpenSkillPicker(p.name)}
                            >
                              {skillPickerFor === p.name ? "Close picker" : "Copy skills"}
                            </Button>
                          )}
                        </div>
                        <div>Installed: {details.skills.count}</div>
                        {detailSkills.length > 0 ? (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {(p.is_default ? detailSkills.slice(0, 8) : detailSkills).map((skill) => (
                              <span key={skill} className="inline-flex items-center gap-0.5">
                                <Badge tone="outline" className="text-[10px]">
                                  {skill}
                                </Badge>
                                {!p.is_default && (
                                  <>
                                    <Button
                                      ghost
                                      size="icon"
                                      className="h-5 w-5 text-muted-foreground hover:text-foreground"
                                      title={`编辑 skill ${skill}`}
                                      aria-label={`编辑 skill ${skill}`}
                                      onClick={() => openSkillEditor(p.name, skill)}
                                    >
                                      <Pencil className="h-3 w-3" />
                                    </Button>
                                    <Button
                                      ghost
                                      size="icon"
                                      className="h-5 w-5 text-muted-foreground hover:text-destructive"
                                      title={`删除 skill ${skill}`}
                                      aria-label={`删除 skill ${skill}`}
                                      disabled={profileAction === `skill-delete:${p.name}:${skill}`}
                                      onClick={() => handleDeleteProfileSkill(p.name, skill)}
                                    >
                                      <Trash2 className="h-3 w-3" />
                                    </Button>
                                  </>
                                )}
                              </span>
                            ))}
                            {details.skills.truncated && (
                              <Badge tone="outline" className="text-[10px]">more...</Badge>
                            )}
                          </div>
                        ) : (
                          <div className="text-muted-foreground">No profile-local skills found.</div>
                        )}

                        {skillPickerFor === p.name && (
                          <div className="mt-3 grid gap-2 border-t border-border/60 pt-3">
                            <Input
                              placeholder="Search default skills..."
                              value={skillSearchByProfile[p.name] ?? ""}
                              onChange={(e) =>
                                setSkillSearchByProfile((prev) => ({
                                  ...prev,
                                  [p.name]: e.target.value,
                                }))
                              }
                            />
                            <div className="max-h-56 overflow-auto border border-border/60 bg-background/40 p-2">
                              {defaultSkillsLoading ? (
                                <div className="text-muted-foreground">Loading default skills...</div>
                              ) : defaultSkills.length === 0 ? (
                                <div className="text-muted-foreground">No default skills found.</div>
                              ) : (
                                <div className="grid gap-1.5">
                                  {defaultSkills
                                    .filter((skill) => {
                                      const q = (skillSearchByProfile[p.name] ?? "").trim().toLowerCase();
                                      return !q || skill.toLowerCase().includes(q);
                                    })
                                    .map((skill) => {
                                      const installed = detailSkills.includes(skill);
                                      const selected = (selectedSkillsByProfile[p.name] ?? []).includes(skill);
                                      return (
                                        <label
                                          key={skill}
                                          className="flex items-center gap-2 text-xs normal-case"
                                        >
                                          <Checkbox
                                            checked={installed || selected}
                                            disabled={installed}
                                            onCheckedChange={(checked) =>
                                              toggleSkillSelection(p.name, skill, checked === true)
                                            }
                                          />
                                          <span className="truncate font-mono">
                                            {skill}
                                            {installed ? " (installed)" : ""}
                                          </span>
                                        </label>
                                      );
                                    })}
                                </div>
                              )}
                            </div>
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-xs text-muted-foreground">
                                {(selectedSkillsByProfile[p.name] ?? []).length} selected
                              </span>
                              <Button
                                size="sm"
                                disabled={
                                  profileAction === `skills:${p.name}` ||
                                  (selectedSkillsByProfile[p.name] ?? []).length === 0
                                }
                                onClick={() => handleCopySkills(p.name, "default")}
                              >
                                Copy selected
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>

                      <div className="border border-border/60 bg-muted/10 p-3">
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-foreground">
                          Memory
                        </div>
                        <div>Provider: {details.memory.provider || "(default)"}</div>
                        <div>Memory files: {details.memory.memory_file_count}</div>
                        <div>State DB: {details.memory.state_db_exists ? "present" : "missing"}</div>
                        <div className="truncate font-mono">state: {details.memory.state_db}</div>
                        <div className="mt-3 flex flex-wrap gap-1">
                          {editableMemoryPreviews.length > 0 ? (
                            editableMemoryPreviews.map((preview) => (
                              <Button
                                key={preview.name}
                                size="sm"
                                outlined
                                className="h-auto px-2 py-1 text-[10px]"
                                title={`Preview and edit ${preview.name}`}
                                onClick={() => openMemoryEditor(p.name, preview.name as MemoryFileName)}
                              >
                                {preview.name}: {preview.exists ? `${preview.bytes} bytes` : "missing"}
                              </Button>
                            ))
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              Memory preview requires a dashboard restart if this stays empty.
                            </span>
                          )}
                        </div>
                      </div>

                      {detailAuditEvents.length > 0 && (
                        <div className="border border-border/60 bg-muted/10 p-3 md:col-span-2">
                          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-foreground">
                            Audit
                          </div>
                          <div className="grid gap-1">
                            {detailAuditEvents.slice(0, 5).map((event, index) => (
                              <div key={`${event.ts || index}:${event.action || "event"}`} className="truncate">
                                <span className="font-mono">{event.ts || "(no timestamp)"}</span>
                                {" "}
                                <span>{event.action || "(unknown action)"}</span>
                                {event.actor_user_name || event.actor_user_id ? (
                                  <span> by {String(event.actor_user_name || event.actor_user_id)}</span>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      <div className="border border-border/60 bg-muted/10 p-3 md:col-span-2">
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <span className="text-[10px] font-semibold uppercase tracking-wider text-foreground">
                            IM Bindings
                          </span>
                          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                            Webhook {detailWebhookConfigured}/{detailBindings.length}
                            {detailWebhookExpired > 0 ? ` · ${detailWebhookExpired} expired` : ""}
                          </span>
                        </div>
                        {detailBindings.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full min-w-[760px] border-collapse text-left text-xs">
                              <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                                <tr className="border-b border-border/60">
                                  <th className="py-2 pr-3 font-semibold">平台</th>
                                  <th className="py-2 pr-3 font-semibold">群 / 会话</th>
                                  <th className="py-2 pr-3 font-semibold">绑定范围</th>
                                  <th className="py-2 pr-3 font-semibold">Webhook</th>
                                  <th className="py-2 font-semibold">更新</th>
                                </tr>
                              </thead>
                              <tbody>
                                {detailBindings.map((binding) => {
                                  const target = bindingTarget(binding);
                                  return (
                                    <tr
                                      key={binding.source_binding_key}
                                      className="border-b border-border/40 last:border-0"
                                    >
                                      <td className="py-2 pr-3 align-top">
                                        <Badge tone="outline" className="text-[10px]">
                                          {target.platform}
                                        </Badge>
                                      </td>
                                      <td className="py-2 pr-3 align-top">
                                        <div className="font-medium text-foreground">
                                          {target.label}
                                        </div>
                                        <div className="max-w-[18rem] truncate font-mono text-[10px] text-muted-foreground">
                                          {target.chatId || binding.source_binding_key}
                                        </div>
                                      </td>
                                      <td className="py-2 pr-3 align-top">
                                        <div>{target.scope}</div>
                                        <div className="max-w-[16rem] truncate font-mono text-[10px] text-muted-foreground">
                                          {binding.agent_id}
                                        </div>
                                      </td>
                                      <td className="py-2 pr-3 align-top">
                                        <Badge tone={webhookTone(binding)} className="text-[10px]">
                                          {webhookLabel(binding)}
                                        </Badge>
                                        <div className="mt-1 text-[10px] text-muted-foreground">
                                          {webhookNote(binding)}
                                        </div>
                                      </td>
                                      <td className="py-2 align-top text-[10px] text-muted-foreground">
                                        {formatUnixSeconds(binding.updated_at)}
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <div className="text-xs text-muted-foreground">
                            当前 agent 还没有绑定任何 IM 群或会话。
                          </div>
                        )}
                      </div>

                      {details.health.recent_error && (
                        <div className="border border-warning/30 bg-warning/5 p-3 md:col-span-2">
                          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-warning">
                            Recent Log
                          </div>
                          <div className="font-mono text-warning/90">
                            {details.health.recent_error}
                          </div>
                        </div>
                      )}
                      {details.health.config_error && (
                        <div className="border border-warning/30 bg-warning/5 p-3 md:col-span-2">
                          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-warning">
                            Config
                          </div>
                          <div className="font-mono text-warning/90">
                            {details.health.config_error}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {isEditingSoul && (
                <div className="border-t border-border px-4 pb-4 pt-3 flex flex-col gap-2">
                  <Label
                    htmlFor={`soul-editor-${p.name}`}
                    className="flex items-center gap-2 text-xs uppercase tracking-wider text-muted-foreground"
                  >
                    {t.profiles.soulSection}
                  </Label>
                  <textarea
                    id={`soul-editor-${p.name}`}
                    className="flex min-h-[180px] w-full border border-input bg-transparent px-3 py-2 text-sm font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    placeholder={t.profiles.soulPlaceholder}
                    value={soulText}
                    onChange={(e) => setSoulText(e.target.value)}
                  />
                  <div>
                    <Button
                      size="sm"
                      onClick={() => handleSaveSoul(p.name)}
                      disabled={soulSaving}
                    >
                      {soulSaving ? t.common.saving : t.profiles.saveSoul}
                    </Button>
                  </div>
                </div>
              )}
            </Card>
          );
        })}
      </div>

      {memoryEditor && (
        <ProfileManagementModal
          title={`${memoryEditor.profile}: ${memoryEditor.file}`}
          description="Preview and edit the profile-local memory file. Raw content is loaded only after this modal opens."
          onClose={() => setMemoryEditor(null)}
          wide
        >
          {memoryEditor.loading ? (
            <div className="text-sm text-muted-foreground">Loading memory file...</div>
          ) : (
            <div className="grid gap-3">
              <textarea
                className="min-h-[48vh] w-full border border-input bg-background/40 px-3 py-2 font-mono text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                value={memoryEditor.content}
                onChange={(e) =>
                  setMemoryEditor((prev) =>
                    prev ? { ...prev, content: e.target.value } : prev,
                  )
                }
              />
              <div className="flex justify-end">
                <Button
                  size="sm"
                  disabled={memoryEditor.saving}
                  onClick={handleSaveMemory}
                >
                  {memoryEditor.saving ? "Saving" : "Save memory"}
                </Button>
              </div>
            </div>
          )}
        </ProfileManagementModal>
      )}

      {skillEditor && (
        <ProfileManagementModal
          title={`${skillEditor.profile}: ${skillEditor.skill}/SKILL.md`}
          description="Edits only this profile-local skill manifest. Default profile skills remain protected from this dashboard editor."
          onClose={() => setSkillEditor(null)}
          wide
        >
          {skillEditor.loading ? (
            <div className="text-sm text-muted-foreground">Loading skill manifest...</div>
          ) : (
            <div className="grid gap-3">
              <textarea
                className="min-h-[52vh] w-full border border-input bg-background/40 px-3 py-2 font-mono text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                value={skillEditor.content}
                onChange={(e) =>
                  setSkillEditor((prev) =>
                    prev ? { ...prev, content: e.target.value } : prev,
                  )
                }
              />
              <div className="flex justify-end">
                <Button
                  size="sm"
                  disabled={skillEditor.saving}
                  onClick={handleSaveSkillManifest}
                >
                  {skillEditor.saving ? "Saving" : "Save skill"}
                </Button>
              </div>
            </div>
          )}
        </ProfileManagementModal>
      )}

      {modelPickerFor && (
        <ModelPickerDialog
          loader={() => api.getProfileModelOptions(modelPickerFor)}
          alwaysGlobal
          title={`Set model: ${modelPickerFor}`}
          onApply={async ({ provider, model }) => {
            await api.setProfileModel(modelPickerFor, { provider, model });
            showToast(`Model updated: ${modelPickerFor}`, "success");
            await refreshProfileDetails(modelPickerFor);
            load();
          }}
          onClose={() => setModelPickerFor(null)}
        />
      )}
    </div>
  );
}
