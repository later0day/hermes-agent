import { useCallback, useMemo, type Dispatch, type SetStateAction } from "react";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";

import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";
import { api, type SkillInfo } from "@/lib/api";
import { canUninstallSkill, waitForAction } from "@/lib/skill-uninstall";

/** Delete (uninstall) for the Skills page. The backend spawns `hermes skills uninstall <name> --yes`
 *  and returns at once, so the row drops optimistically and the list is reconciled with a fresh
 *  getSkills once the CLI exits (an in-use skill may survive and re-appear). Scoped to the sidebar
 *  profile like every other write on the page; only hub skills offer the control. */
export function useSkillUninstall(
  scope: string | null | undefined,
  setSkills: Dispatch<SetStateAction<SkillInfo[]>>,
  showToast: (msg: string, kind: "success" | "error") => void,
) {
  const { t } = useI18n();
  const s = useMemo(() => localeSection(t, "skillsPageX"), [t]);
  const profile = scope || undefined;
  const skillDelete = useConfirmDelete<string>({
    onDelete: useCallback(
      async (name: string) => {
        try {
          const res = await api.uninstallSkillFromHub(name, profile);
          setSkills((prev) => prev.filter((s) => s.name !== name));
          showToast(s.uninstallingToast.replace("{name}", name), "success");
          void waitForAction((n) => api.getActionStatus(n, 0), res.name)
            .then(() => api.getSkills(profile))
            .then(setSkills)
            .catch(() => {});
        } catch (e) {
          showToast(`${t.status.error}: ${e}`, "error");
          throw e;
        }
      },
      [profile, setSkills, showToast, t.status.error, s],
    ),
  });
  return {
    onDeleteFor: (skill: SkillInfo) =>
      canUninstallSkill(skill) ? () => skillDelete.requestDelete(skill.name) : undefined,
    dialog: (
      <DeleteConfirmDialog
        open={skillDelete.isOpen}
        onCancel={skillDelete.cancel}
        onConfirm={skillDelete.confirm}
        title={s.deleteConfirmTitle}
        description={s.deleteConfirmDesc.replace("{name}", skillDelete.pendingId ?? "")}
        loading={skillDelete.isDeleting}
      />
    ),
  };
}
