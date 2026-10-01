"use client";

import type { RunSettings } from "@/lib/live";

/** The settings a replay or a plan may change, and how each is shown. */
type Field = {
  key: keyof RunSettings;
  label: string;
  hint: string;
  kind: "number" | "select" | "optional";
  options?: { value: string; label: string }[];
  step?: number;
  /** Shown as a percent, stored as a fraction. */
  percent?: boolean;
  ladderOnly?: boolean;
  hicOnly?: boolean;
  replayOnly?: boolean;
};

export const FIELDS: Field[] = [
  { key: "step", label: "Step", hint: "points between levels", kind: "number", step: 50 },
  { key: "short_offset", label: "Short strikes", hint: "points from the level", kind: "number", step: 50, ladderOnly: true },
  { key: "long_offset", label: "Long strikes", hint: "points from the level (the wing)", kind: "number", step: 50, ladderOnly: true },
  {
    key: "direction", label: "Direction", hint: "which moves open condors", kind: "select", ladderOnly: true,
    options: [{ value: "down", label: "Down only" }, { value: "both", label: "Two-way" }, { value: "up", label: "Up only" }],
  },
  { key: "max_down", label: "Max down", hint: "levels below the anchor; blank = no cap", kind: "optional", ladderOnly: true },
  { key: "max_up", label: "Max up", hint: "levels above the anchor; blank = no cap", kind: "optional", ladderOnly: true },
  { key: "max_condors", label: "Max open", hint: "positions in the campaign", kind: "number" },
  { key: "max_entry_vix", label: "Pause above VIX", hint: "blank = no VIX rule", kind: "optional" },
  { key: "min_entry_dte", label: "Skip last days", hint: "no entry with fewer days left; blank = off", kind: "optional", ladderOnly: true },
  { key: "min_credit_ratio", label: "Min credit", hint: "% of the wing; blank = off", kind: "optional", percent: true, ladderOnly: true },
  { key: "take_profit", label: "Take profit", hint: "% of credit; blank = hold", kind: "optional", percent: true, replayOnly: true },
  { key: "stop_loss", label: "Stop loss", hint: "× credit lost; blank = hold", kind: "optional", replayOnly: true },
  {
    key: "bar_minutes", label: "Time frame", hint: "what a level fires on", kind: "select", replayOnly: true,
    options: [
      { value: "1", label: "Every minute" }, { value: "5", label: "5-min bars" },
      { value: "15", label: "15-min bars" }, { value: "30", label: "30-min bars" }, { value: "60", label: "Hourly" },
    ],
  },
  { key: "lots", label: "Lots", hint: "per position", kind: "number" },
  { key: "daily_loss_limit", label: "Daily loss limit", hint: "₹ lost in a day stops new positions for it; blank = none", kind: "optional", step: 1000 },
  { key: "full_band_steps", label: "Core band", hint: "steps that open a full condor", kind: "number", hicOnly: true },
  { key: "max_put_spreads", label: "Put spreads", hint: "most below the band", kind: "number", hicOnly: true },
  { key: "max_call_spreads", label: "Call spreads", hint: "most above the band", kind: "number", hicOnly: true },
];

export type Edits = Partial<Record<keyof RunSettings, number | string | null>>;

/** The editable copy of `base` with `edits` applied, as the engine wants it. */
export function applyEdits(base: RunSettings, edits: Edits): Record<string, unknown> {
  return { ...base, ...edits };
}

export function SettingsEditor({
  base,
  edits,
  onChange,
  mode,
}: {
  base: RunSettings;
  edits: Edits;
  onChange: (edits: Edits) => void;
  mode: "replay" | "plan";
}) {
  const hic = base.strategy === "hic";
  const fields = FIELDS.filter((f) => (hic ? !f.ladderOnly : !f.hicOnly) && (mode === "replay" || !f.replayOnly));

  const value = (f: Field): number | string | null => {
    const v = f.key in edits ? edits[f.key] : (base[f.key] as number | string | null | undefined);
    return v ?? null;
  };
  const changed = (f: Field) => f.key in edits && (edits[f.key] ?? null) !== ((base[f.key] as unknown) ?? null);

  const set = (f: Field, raw: string) => {
    let next: number | string | null;
    if (f.kind === "select") next = f.key === "bar_minutes" ? Number(raw) : raw;
    else if (raw.trim() === "") next = f.kind === "optional" ? null : (base[f.key] as number);
    else next = f.percent ? Number(raw) / 100 : Number(raw);
    const out = { ...edits, [f.key]: next };
    if ((next ?? null) === ((base[f.key] as unknown) ?? null)) delete out[f.key];
    onChange(out);
  };

  return (
    <div className="pg-fields">
      {fields.map((f) => {
        const v = value(f);
        const shown = v == null ? "" : f.percent && typeof v === "number" ? String(Math.round(v * 1000) / 10) : String(v);
        return (
          <label key={f.key} className={`pg-field${changed(f) ? " is-changed" : ""}`}>
            <span className="pg-field-label">
              {f.label}
              {changed(f) && <span className="pg-field-was">was {formatBase(f, base)}</span>}
            </span>
            {f.kind === "select" ? (
              <select className="auth-input" value={shown} onChange={(e) => set(f, e.target.value)}>
                {f.options!.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            ) : (
              <input
                className="auth-input"
                type="number"
                inputMode="decimal"
                step={f.step ?? "any"}
                value={shown}
                placeholder={f.kind === "optional" ? "off" : undefined}
                onChange={(e) => set(f, e.target.value)}
              />
            )}
            <span className="pg-field-hint">{f.hint}</span>
          </label>
        );
      })}
    </div>
  );
}

function formatBase(f: Field, base: RunSettings): string {
  const v = base[f.key] as number | string | null | undefined;
  if (v == null) return "off";
  if (f.options) return f.options.find((o) => o.value === String(v))?.label ?? String(v);
  if (f.percent && typeof v === "number") return `${Math.round(v * 1000) / 10}%`;
  return String(v);
}
