import { useMemo, useState } from "react";
import { Link2, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import type { ProfileBindingSummary } from "@/lib/api";
import { profileBindingsApi, type BindableChat } from "@/lib/api-profile-bindings";
import { useI18n } from "@/i18n";
import { localeSection } from "@/i18n/section";

interface ProfileBindingEditorProps {
  profile: string;
  summary: ProfileBindingSummary | undefined;
  onChanged: () => void;
  onError: (message: string) => void;
}

const chatValue = (chat: BindableChat) => `${chat.platform}\u0000${chat.chat_id}`;

/** Bind a known chat to this profile, or unbind one of its ``/agent use`` bindings, without being in the chat. */
export function ProfileBindingEditor({ profile, summary, onChanged, onError }: ProfileBindingEditorProps) {
  const dynamic = summary?.dynamic ?? [];
  const { t } = useI18n();
  const s = useMemo(() => localeSection(t, "profileBindingEditor"), [t]);
  const [open, setOpen] = useState(false);
  const [chats, setChats] = useState<BindableChat[] | null>(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);

  const bound = new Set(dynamic.map((b) => `${b.platform}\u0000${b.chat_id}`));
  const choices = (chats ?? []).filter((c) => !bound.has(chatValue(c)));

  const toggle = () => {
    setOpen(!open);
    if (!open && chats === null) {
      profileBindingsApi.chats().then((res) => setChats(res.chats)).catch(() => setChats([]));
    }
  };
  const run = (action: () => Promise<unknown>) => {
    setBusy(true);
    action()
      .then(() => { setPicked(""); onChanged(); })
      .catch((e: unknown) => onError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };
  const chosen = choices.find((c) => chatValue(c) === picked);

  return (
    <div className="flex flex-col gap-1.5 text-xs">
      <button type="button" className="self-start text-primary underline" onClick={toggle}>
        {open ? s.close : s.manage}
      </button>
      {open && (
        <div className="flex flex-col gap-1.5 border border-border/50 p-2">
          {dynamic.length === 0 && <span className="text-muted-foreground/60 italic">{s.noBindings}</span>}
          {dynamic.map((b) => (
            <div key={b.source_binding_key} className="flex items-center gap-1.5">
              <span className="truncate font-mono text-muted-foreground" title={b.source_binding_key}>
                {b.platform} · {b.chat_id}
              </span>
              <Button ghost size="xs" disabled={busy} aria-label={s.unbind} title={s.unbind}
                onClick={() => run(() => profileBindingsApi.unbind(b.source_binding_key))}>
                <X className="h-3 w-3" />
              </Button>
            </div>
          ))}
          <div className="flex items-center gap-1.5">
            <Select value={picked} onValueChange={setPicked} aria-label={s.pickChat}>
              <SelectOption value="">{chats === null ? t.common.loading : s.pickChat}</SelectOption>
              {choices.map((c) => (
                <SelectOption key={chatValue(c)} value={chatValue(c)}>
                  {`${c.platform} · ${c.name} (${c.chat_type === "dm" ? s.dm : s.group})`}
                </SelectOption>
              ))}
            </Select>
            <Button size="xs" disabled={!chosen || busy} prefix={<Link2 className="h-3 w-3" />}
              onClick={() => chosen && run(() => profileBindingsApi.bind(profile, chosen))}>
              {s.bind}
            </Button>
          </div>
          <span className="text-muted-foreground/70">{s.hint}</span>
        </div>
      )}
    </div>
  );
}
