"use client";

import type { ReactNode } from "react";
import type { RunSettings } from "@/lib/live";
import {
  IconShield,
  IconTarget,
  IconZap,
  IconTwoWay,
  IconLadder,
  IconClock,
  IconCheck,
  IconSparkles,
} from "./Icons";
import { InfoTooltip } from "@/components/ui";

const FIELD_MAP: Record<string, string> = {
  short_offset: "wings",
  long_offset: "wings",
  max_entry_vix: "max_vix",
  min_entry_dte: "min_dte",
  min_credit_ratio: "min_credit",
  trailing_sl_trigger: "trailing_trigger",
  bar_minutes: "resolution",
  full_band_steps: "hic_band",
};

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
  { key: "stop_loss", label: "Campaign Stop loss", hint: "₹ max monthly loss (e.g. 25000); blank = hold", kind: "optional", replayOnly: true },
  { key: "trailing_sl", label: "Campaign Trailing SL", hint: "% pullback from peak capital (e.g. 15% halts at ₹21,250 on ₹25k); blank = off", kind: "optional", percent: true, replayOnly: true },
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

export interface StrategyPreset {
  id: string;
  label: string;
  icon: ReactNode;
  hint: string;
  replayOnly?: boolean;
  ladderOnly?: boolean;
  edits: Edits;
}

export const STRATEGY_PRESETS: StrategyPreset[] = [
  {
    id: "sl_25k",
    label: "₹25k Monthly SL",
    icon: <IconShield size={13} />,
    hint: "Cap monthly campaign loss at ₹25,000 (protects Sep 2026)",
    replayOnly: true,
    edits: { stop_loss: 25000 },
  },
  {
    id: "tp_50",
    label: "50% Take Profit",
    icon: <IconTarget size={13} />,
    hint: "Lock in gains when condor reaches 50% max profit",
    replayOnly: true,
    edits: { take_profit: 0.5 },
  },
  {
    id: "trail_15",
    label: "15% Monthly TSL",
    icon: <IconShield size={13} />,
    hint: "Halt if capital pulls back 15% from peak campaign-end capital (e.g. ₹21,250 on ₹25k)",
    replayOnly: true,
    edits: { trailing_sl: 0.15 },
  },
  {
    id: "vix_14",
    label: "VIX ≤ 14 Guard",
    icon: <IconZap size={13} />,
    hint: "Pause new entries when India VIX rises above 14.0",
    edits: { max_entry_vix: 14.0 },
  },
  {
    id: "two_way",
    label: "Two-Way Ladder",
    icon: <IconTwoWay size={13} />,
    hint: "Trade both up and down moves instead of one-way",
    ladderOnly: true,
    edits: { direction: "both" },
  },
  {
    id: "step_150",
    label: "Wider 150 Step",
    icon: <IconLadder size={13} />,
    hint: "Place condors every 150 points for wider spacing",
    ladderOnly: true,
    edits: { step: 150 },
  },
  {
    id: "tf_5m",
    label: "5-Min Bar Filter",
    icon: <IconClock size={13} />,
    hint: "Trigger levels on 5-min bar closes instead of 1-min ticks",
    replayOnly: true,
    ladderOnly: true,
    edits: { bar_minutes: 5 },
  },
];

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
  const presets = STRATEGY_PRESETS.filter((p) => (!p.replayOnly || mode === "replay") && (!p.ladderOnly || !hic));

  const value = (f: Field): number | string | null => {
    const v = f.key in edits ? edits[f.key] : (base[f.key] as number | string | null | undefined);
    return v ?? null;
  };
  const changed = (f: Field) => f.key in edits && (edits[f.key] ?? null) !== ((base[f.key] as unknown) ?? null);

  const isPresetActive = (p: StrategyPreset) => {
    return Object.entries(p.edits).every(([k, expected]) => {
      const key = k as keyof RunSettings;
      const current = key in edits ? edits[key] : base[key];
      return current === expected;
    });
  };

  const togglePreset = (p: StrategyPreset) => {
    const active = isPresetActive(p);
    const next = { ...edits };
    if (active) {
      for (const k of Object.keys(p.edits) as (keyof RunSettings)[]) {
        delete next[k];
      }
    } else {
      for (const [k, v] of Object.entries(p.edits)) {
        (next as Record<string, unknown>)[k] = v;
      }
    }
    onChange(next);
  };

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
    <div>
      {presets.length > 0 && (
        <div className="pg-presets-bar">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11.5, fontWeight: 700, color: "var(--ink-muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginRight: 4 }}>
            <IconSparkles size={13} style={{ color: "var(--brand)" }} />
            Quick What-If Ideas:
          </span>
          {presets.map((p) => {
            const active = isPresetActive(p);
            return (
              <button
                key={p.id}
                type="button"
                className={`pg-preset-chip${active ? " is-active" : ""}`}
                onClick={() => togglePreset(p)}
                title={p.hint}
              >
                <span style={{ display: "inline-flex", alignItems: "center" }}>{p.icon}</span>
                <span>{p.label}</span>
                {active && (
                  <span style={{ display: "inline-flex", alignItems: "center", color: "var(--brand-strong)", marginLeft: 2 }}>
                    <IconCheck size={11} />
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      <div className="pg-fields">
        {fields.map((f) => {
          const v = value(f);
          const shown = v == null ? "" : f.percent && typeof v === "number" ? String(Math.round(v * 1000) / 10) : String(v);
          return (
            <label key={f.key} className={`pg-field${changed(f) ? " is-changed" : ""}`}>
              <span className="pg-field-label">
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  {f.label}
                  <InfoTooltip field={FIELD_MAP[f.key] ?? f.key} title={f.label} content={f.hint} />
                </span>
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
