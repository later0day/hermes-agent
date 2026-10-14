import { ConfirmDialog } from "@/components/ConfirmDialog";
import { prefersZh } from "@/lib/doc-locale";

/**
 * Confirm + full-page reload after a model change.
 *
 * Changing the main model persists to config.yaml, but the RUNNING chat keeps
 * its model until its session is rebuilt. A full reload (fresh PTY session that
 * boots its agent from the just-saved config) is the reliable way to apply it —
 * the in-place hot-swap and partial remount both proved unreliable. We confirm
 * first because the reload starts a fresh chat (the current one stays resumable
 * in Sessions and the agent's memory is kept).
 *
 * Shared by the chat sidebar picker and the Models page so both behave
 * identically. `model` is the short model name awaiting confirmation, or null
 * when the dialog is closed.
 */
export function ModelReloadConfirm({
  model,
  description,
  onCancel,
}: {
  model: string | null;
  /** Override the default body copy (e.g. the Models-page phrasing). */
  description?: string;
  onCancel: () => void;
}) {
  const zh = prefersZh();
  return (
    <ConfirmDialog
      open={model !== null}
      title={zh ? "切换模型？" : "Switch model?"}
      description={
        description ??
        (zh
          ? `切换到 ${model ?? ""} 会开始一个新对话。当前对话会保留在会话列表中，智能体的记忆也会保留。现在重新加载以生效吗？`
          : `Switching to ${model ?? ""} starts a fresh chat. Your current chat stays in your Sessions list and the agent's memory is kept. Reload now to apply it?`)
      }
      confirmLabel={zh ? "重新加载" : "Reload"}
      onConfirm={() => window.location.reload()}
      onCancel={onCancel}
    />
  );
}
