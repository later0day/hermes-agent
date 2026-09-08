import { useMemo, useState } from "react";
import { Send } from "lucide-react";
import { Input } from "@nous-research/ui/ui/components/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@nous-research/ui/ui/components/dialog";
import type { ProfileInfo, RoomCreateRequest, RoomSummary, RoomTopologyResponse } from "@/lib/api";

const buttonClass = "inline-flex h-9 w-auto flex-none items-center justify-center whitespace-nowrap rounded-md border border-border bg-background px-3 text-xs font-semibold text-text-primary transition-colors hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:pointer-events-none disabled:opacity-50";
const primaryClass = buttonClass + " border-primary bg-primary text-primary-foreground hover:bg-primary/90";

function slug(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48);
}

export function buildMemberHandles(profiles: readonly ProfileInfo[], selected: readonly string[]) {
  const used = new Set<string>();
  return selected.map((profile, index) => {
    const item = profiles.find((candidate) => candidate.name === profile);
    const base = slug(item?.display_name || "") || slug(profile) || `member-${index + 1}`;
    let handle = base;
    let suffix = 2;
    while (used.has(handle.toLowerCase())) handle = `${base.slice(0, 43)}-${suffix++}`;
    used.add(handle.toLowerCase());
    return { profile, handle, display_name: item?.display_name || profile };
  });
}

export interface CreateTeamDialogProps {
  profiles: readonly ProfileInfo[];
  busy?: boolean;
  createOpen: boolean;
  onCloseCreate(): void;
  onCreate(request: RoomCreateRequest): Promise<boolean>;
}

export interface WorkComposerProps {
  topology?: RoomTopologyResponse | null;
  room: RoomSummary;
  busy?: boolean;
  onDelegate?(recipient: string, text: string): Promise<boolean>;
}

export function CreateTeamDialog({ profiles, busy, createOpen, onCloseCreate, onCreate }: CreateTeamDialogProps) {
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [leader, setLeader] = useState("");
  const valid = name.trim().length > 1 && selected.length >= 2 && selected.length <= 6 && selected.includes(leader);
  const toggle = (profile: string) => setSelected((current) => current.includes(profile) ? current.filter((item) => item !== profile) : current.length < 6 ? [...current, profile] : current);
  const submit = async () => {
    if (!valid) return;
    const request: RoomCreateRequest = { room_id: (slug(name) || "team") + "-" + crypto.randomUUID().slice(0, 8), name: name.trim(), members: buildMemberHandles(profiles, selected).map((member) => ({ ...member, role: member.profile === leader ? "decider" : "worker" })) };
    if (await onCreate(request)) { setName(""); setSelected([]); setLeader(""); onCloseCreate(); }
  };
  return <Dialog open={createOpen} onOpenChange={(next) => { if (!next) onCloseCreate(); }}><DialogContent data-testid="create-team-dialog" className="max-h-[90vh] w-[min(40rem,calc(100vw-2rem))] overflow-y-auto rounded-2xl p-0"><div className="border-b border-border px-5 py-5 pr-12 sm:px-6"><DialogHeader className="text-left"><DialogTitle>Build a team</DialogTitle><DialogDescription>Choose 2–6 agent profiles. One leader coordinates, delegates, and synthesizes; workers execute independently.</DialogDescription></DialogHeader></div><div className="space-y-5 px-5 py-5 sm:px-6"><label className="block text-sm font-medium">Team name<Input aria-label="Team name" className="mt-2" value={name} onChange={(event) => setName(event.target.value)} placeholder="Research launch review" /></label><fieldset><legend className="text-sm font-medium">Teammates <span className="font-normal text-text-tertiary">({selected.length}/6)</span></legend><div className="mt-2 grid gap-2 sm:grid-cols-2">{profiles.map((profile) => <label key={profile.name} className={"flex cursor-pointer items-start gap-3 rounded-lg border p-3 " + (selected.includes(profile.name) ? "border-primary bg-primary/10" : "border-border")}><input type="checkbox" checked={selected.includes(profile.name)} disabled={!selected.includes(profile.name) && selected.length >= 6} onChange={() => toggle(profile.name)} /><span className="min-w-0"><b className="block truncate">{profile.display_name || profile.name}</b><small className="block text-text-secondary">{profile.description || ((profile.provider || "Configured") + " · " + (profile.model || "default model"))}</small></span></label>)}</div>{!profiles.length ? <p className="mt-2 rounded-lg border border-dashed p-4 text-sm text-text-secondary">Create at least two Profiles before building a team.</p> : null}</fieldset>{selected.length ? <label className="block text-sm font-medium">Team leader<select aria-label="Team leader" value={leader} onChange={(event) => setLeader(event.target.value)} className="mt-2 h-10 w-full rounded-md border border-border bg-background px-3"><option value="">Choose the coordinating agent</option>{selected.map((profile) => <option key={profile} value={profile}>{profiles.find((item) => item.name === profile)?.display_name || profile}</option>)}</select><small className="mt-1 block text-text-tertiary">The leader plans and dispatches through the shared task list; it does not perform ordinary worker execution.</small></label> : null}</div><DialogFooter className="flex-col gap-2 border-t border-border px-5 py-4 sm:flex-row sm:justify-end sm:px-6"><button type="button" className={buttonClass + " w-full sm:w-auto"} onClick={onCloseCreate}>Cancel</button><button type="button" data-testid="create-team-submit" className={primaryClass + " w-full sm:w-auto"} disabled={!valid || busy} onClick={() => void submit()}>Create team</button></DialogFooter></DialogContent></Dialog>;
}

export function WorkComposer({ topology, room, busy, onDelegate }: WorkComposerProps) {
  const members = useMemo(() => topology?.members ?? room.members.map((member) => ({ ...member, role: member.role === "decider" ? "team_lead" as const : "teammate" as const })) ?? [], [topology, room.members]);
  const leader = members.find((member) => member.role === "team_lead" || member.role === "coordinator");
  const preferredRecipient = leader?.handle || members.find((member) => member.handle)?.handle || "";
  const [selectedRecipient, setSelectedRecipient] = useState(preferredRecipient);
  const recipient = members.some((member) => member.handle === selectedRecipient) ? selectedRecipient : preferredRecipient;
  const [text, setText] = useState("");
  const send = async () => { if (onDelegate && text.trim() && recipient && await onDelegate(recipient, text.trim())) setText(""); };
  if (!onDelegate || !room || room.disbanded_at) return null;
  return <section aria-label="Delegate new work" className="mb-4 rounded-xl border border-primary/30 bg-primary/5 p-3 sm:p-4"><div className="mb-3"><h2 className="flex items-center gap-2 font-semibold"><Send className="h-4 w-4 text-primary" /> Delegate new work</h2><p className="mt-1 text-xs text-text-secondary">Send to the leader for planning and delegation, or directly to one worker for focused execution.</p></div><div className="grid min-w-0 gap-2 sm:grid-cols-[11rem_minmax(0,1fr)_auto] sm:items-end"><label className="text-xs font-medium text-text-secondary">Send to<select aria-label="Work recipient" value={recipient} onChange={(event) => setSelectedRecipient(event.target.value)} className="mt-1 h-10 w-full rounded-md border border-border bg-background px-2 text-sm">{members.filter((member) => member.handle).map((member) => <option key={member.member_id || member.handle} value={member.handle}>{member.role === "team_lead" || member.role === "coordinator" ? "Leader" : "Worker"} · {("display_name" in member && member.display_name) || member.handle}</option>)}</select></label><label className="text-xs font-medium text-text-secondary">Task<textarea aria-label="New team task" value={text} onChange={(event) => setText(event.target.value)} rows={2} className="mt-1 min-h-10 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm" placeholder={leader && recipient === leader.handle ? "Describe the outcome; the leader will break down and assign the work." : "Give this worker a self-contained task with clear context and deliverable."} /></label><button type="button" data-testid="delegate-work-submit" className={primaryClass + " h-10 w-full sm:w-auto"} disabled={!recipient || !text.trim() || busy} onClick={() => void send()}><Send className="mr-1.5 h-3.5 w-3.5" /> Send</button></div></section>;
}
