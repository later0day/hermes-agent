import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { useI18n } from "@/i18n";
import { getConfigFieldLabel } from "@/i18n/config-labels";

function FieldHint({
  schema,
  schemaKey,
  description: descriptionOverride,
}: {
  schema: Record<string, unknown>;
  schemaKey: string;
  description?: string;
}) {
  const keyPath = schemaKey.includes(".") ? schemaKey : "";
  const description =
    descriptionOverride ?? (schema.description ? String(schema.description) : "");

  if (!keyPath && !description) return null;

  return (
    <div className="flex flex-col gap-0.5">
      {keyPath && <span className="text-xs font-mono text-text-tertiary">{keyPath}</span>}
      {description && <span className="text-xs text-text-secondary">{description}</span>}
    </div>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatScalar(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function ObjectEditor({
  fieldKey,
  value,
  onChange,
}: {
  fieldKey: string;
  value: Record<string, unknown>;
  onChange: (v: unknown) => void;
}) {
  const [newKey, setNewKey] = useState("");
  const entries = Object.entries(value);

  const addKey = () => {
    const k = newKey.trim();
    // Ignore blank names and refuse to clobber an existing key (silent no-op
    // rather than overwriting the user's current value for that key).
    if (!k || Object.prototype.hasOwnProperty.call(value, k)) return;
    onChange({ ...value, [k]: "" });
    setNewKey("");
  };

  const removeKey = (k: string) => {
    const next = { ...value };
    delete next[k];
    onChange(next);
  };

  return (
    <div className="grid gap-2 border border-border p-2">
      {entries.map(([subKey, subVal]) => (
        <div key={subKey} className="grid gap-1">
          <div className="flex items-center justify-between">
            <Label className="text-xs text-muted-foreground">{subKey}</Label>
            <Button
              type="button"
              ghost
              size="icon"
              className="h-6 w-6 text-muted-foreground hover:text-destructive"
              onClick={() => removeKey(subKey)}
              aria-label={`Remove ${subKey}`}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
          <NestedValueEditor
            fieldKey={`${fieldKey}.${subKey}`}
            value={subVal}
            onChange={(next) => onChange({ ...value, [subKey]: next })}
          />
        </div>
      ))}
      <div className="flex items-center gap-1">
        <Input
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addKey();
            }
          }}
          placeholder="new key"
          className="h-7 text-xs"
        />
        <Button
          type="button"
          outlined
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={addKey}
          disabled={
            !newKey.trim() ||
            Object.prototype.hasOwnProperty.call(value, newKey.trim())
          }
          prefix={<Plus className="h-3.5 w-3.5" />}
        >
          Add
        </Button>
      </div>
    </div>
  );
}

function NestedValueEditor({
  fieldKey,
  value,
  onChange,
}: {
  fieldKey: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (isRecord(value)) {
    return <ObjectEditor fieldKey={fieldKey} value={value} onChange={onChange} />;
  }

  if (Array.isArray(value)) {
    // Preserve the element shape when appending: mirror the last item's type
    // (object → {}, array → [], otherwise empty string) so a new entry renders
    // with the same editor the existing items use.
    const seed = (): unknown => {
      const last = value[value.length - 1];
      if (isRecord(last)) return {};
      if (Array.isArray(last)) return [];
      return "";
    };
    return (
      <div className="grid gap-2">
        {value.map((item, index) => (
          <div key={`${fieldKey}.${index}`} className="grid gap-1">
            <div className="flex items-center justify-between">
              <Label className="text-xs text-muted-foreground">Item {index + 1}</Label>
              <Button
                type="button"
                ghost
                size="icon"
                className="h-6 w-6 text-muted-foreground hover:text-destructive"
                onClick={() => onChange(value.filter((_, i) => i !== index))}
                aria-label={`Remove item ${index + 1}`}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
            <NestedValueEditor
              fieldKey={`${fieldKey}.${index}`}
              value={item}
              onChange={(next) =>
                onChange(value.map((existing, i) => (i === index ? next : existing)))
              }
            />
          </div>
        ))}
        <Button
          type="button"
          outlined
          size="sm"
          className="h-7 justify-start gap-1 text-xs"
          onClick={() => onChange([...value, seed()])}
          prefix={<Plus className="h-3.5 w-3.5" />}
        >
          Add item
        </Button>
      </div>
    );
  }

  return (
    <Input
      value={formatScalar(value)}
      onChange={(e) => onChange(e.target.value)}
      className="text-xs"
    />
  );
}

export function AutoField({
  schemaKey,
  schema,
  value,
  onChange,
}: AutoFieldProps) {
  const { locale } = useI18n();
  const override = getConfigFieldLabel(locale, schemaKey);
  const rawLabel = schemaKey.split(".").pop() ?? schemaKey;
  const label =
    override?.label ??
    rawLabel.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const description = override?.description;

  if (isRecord(value) || (Array.isArray(value) && value.some((item) => isRecord(item)))) {
    return (
      <div className="grid gap-3 border border-border p-3">
        <Label className="text-xs font-medium">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        <NestedValueEditor fieldKey={schemaKey} value={value} onChange={onChange} />
      </div>
    );
  }

  if (schema.type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <Label className="text-sm">{label}</Label>
          <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        </div>
        <Switch checked={!!value} onCheckedChange={onChange} />
      </div>
    );
  }

  if (schema.type === "select") {
    const options = (schema.options as string[]) ?? [];
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        <Select value={String(value ?? "")} onValueChange={(v) => onChange(v)}>
          {options.map((opt) => (
            <SelectOption key={opt} value={opt}>
              {opt || "(none)"}
            </SelectOption>
          ))}
        </Select>
      </div>
    );
  }

  if (schema.type === "number") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        <Input
          type="number"
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange(0);
              return;
            }
            const n = Number(raw);
            if (!Number.isNaN(n)) {
              onChange(n);
            }
          }}
        />
      </div>
    );
  }

  if (schema.type === "text") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        <textarea
          className="flex min-h-[80px] w-full border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
    );
  }

  if (schema.type === "list") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
        <Input
          value={Array.isArray(value) ? value.join(", ") : String(value ?? "")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            )
          }
          placeholder="comma-separated values"
        />
      </div>
    );
  }

  return (
    <div className="grid gap-1.5">
      <Label className="text-sm">{label}</Label>
      <FieldHint schema={schema} schemaKey={schemaKey} description={description} />
      <Input value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

interface AutoFieldProps {
  schemaKey: string;
  schema: Record<string, unknown>;
  value: unknown;
  onChange: (v: unknown) => void;
}
