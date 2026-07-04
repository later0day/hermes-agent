import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Plus, Trash2 } from "lucide-react";
import { useI18n } from "@/i18n";

export function KeyValueEditor({
  value,
  onChange,
  keyPlaceholder = "Key",
  valPlaceholder = "Value",
}: {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  keyPlaceholder?: string;
  valPlaceholder?: string;
}) {
  const entries = Object.entries(value || {});

  const updateKey = (oldKey: string, newKey: string) => {
    if (oldKey === newKey) return;
    const next = { ...value };
    const val = next[oldKey];
    delete next[oldKey];
    next[newKey] = val;
    onChange(next);
  };

  const updateVal = (key: string, val: string) => {
    onChange({ ...value, [key]: val });
  };

  const removeRow = (key: string) => {
    const next = { ...value };
    delete next[key];
    onChange(next);
  };

  const addRow = () => {
    let newKey = "new_key";
    let i = 1;
    while (newKey in (value || {})) {
      newKey = `new_key_${i++}`;
    }
    onChange({ ...(value || {}), [newKey]: "" });
  };

  return (
    <div className="flex flex-col gap-2">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center gap-2">
          <Input
            value={k}
            placeholder={keyPlaceholder}
            onChange={(e) => updateKey(k, e.target.value)}
            className="flex-1 text-xs"
          />
          <Input
            value={v}
            placeholder={valPlaceholder}
            onChange={(e) => updateVal(k, e.target.value)}
            className="flex-1 text-xs"
          />
          <Button size="icon" ghost className="text-red-500 hover:text-red-600" onClick={() => removeRow(k)}>
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      ))}
      <Button size="sm" outlined onClick={addRow} prefix={<Plus className="h-4 w-4" />} className="w-fit">
        Add Row
      </Button>
    </div>
  );
}

export function ChannelOverridesEditor({
  value,
  onChange,
}: {
  value: Record<string, any>;
  onChange: (v: Record<string, any>) => void;
}) {
  const { t } = useI18n();
  const entries = Object.entries(value || {});

  const updateChannel = (oldKey: string, newKey: string) => {
    if (oldKey === newKey) return;
    const next = { ...value };
    const val = next[oldKey];
    delete next[oldKey];
    next[newKey] = val;
    onChange(next);
  };

  const updateField = (channel: string, field: string, val: string) => {
    const next = { ...value };
    next[channel] = { ...next[channel], [field]: val };
    onChange(next);
  };

  const removeRow = (channel: string) => {
    const next = { ...value };
    delete next[channel];
    onChange(next);
  };

  const addRow = () => {
    let newKey = "channel_id";
    let i = 1;
    while (newKey in (value || {})) {
      newKey = `channel_id_${i++}`;
    }
    onChange({ ...(value || {}), [newKey]: { model: "", provider: "", system_prompt: "" } });
  };

  return (
    <div className="flex flex-col gap-4">
      {entries.map(([channel, config]) => (
        <div key={channel} className="flex flex-col gap-2 border border-border p-3 rounded">
          <div className="flex items-center justify-between">
            <Label className="text-xs font-semibold">{t.dashboard?.uiChannelId || "Channel ID"}</Label>
            <Button size="icon" ghost className="text-red-500 hover:text-red-600" onClick={() => removeRow(channel)}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
          <Input
            value={channel}
            placeholder={t.dashboard?.uiChannelId || "Channel ID"}
            onChange={(e) => updateChannel(channel, e.target.value)}
            className="text-xs font-mono"
          />
          
          <div className="grid grid-cols-2 gap-2 mt-2">
            <div className="flex flex-col gap-1">
              <Label className="text-xs text-muted-foreground">{t.dashboard?.uiModelOverride || "Model Override"}</Label>
              <Input
                value={config.model || ""}
                placeholder="e.g. anthropic/claude-3-5-sonnet"
                onChange={(e) => updateField(channel, "model", e.target.value)}
                className="text-xs"
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label className="text-xs text-muted-foreground">{t.dashboard?.uiProviderOverride || "Provider Override"}</Label>
              <Input
                value={config.provider || ""}
                placeholder="e.g. openrouter"
                onChange={(e) => updateField(channel, "provider", e.target.value)}
                className="text-xs"
              />
            </div>
          </div>
          
          <div className="flex flex-col gap-1 mt-2">
            <Label className="text-xs text-muted-foreground">{t.dashboard?.uiSystemPromptOverride || "System Prompt Override"}</Label>
            <textarea
              value={config.system_prompt || ""}
              placeholder={t.dashboard?.uiSystemPromptPlaceholder || "Override the agent's identity for this channel..."}
              onChange={(e) => updateField(channel, "system_prompt", e.target.value)}
              className="flex min-h-[60px] w-full border border-input bg-transparent px-3 py-2 text-xs shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        </div>
      ))}
      <Button size="sm" outlined onClick={addRow} prefix={<Plus className="h-4 w-4" />} className="w-fit mt-2">
        Add Channel Override
      </Button>
    </div>
  );
}
