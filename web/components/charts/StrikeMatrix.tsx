"use client";

import { useMemo, useState } from "react";
import type { Condor, MatrixRow } from "@/lib/types";
import { num } from "@/lib/format";
import { Badge } from "@/components/ui";

/**
 * The Strike Ladder Matrix.
 *
 * Rows are strikes, columns are ladder rungs, cells are signed quantity, and
 * the NET column is the point: where a rung's long wing sits on the same
 * strike as a lower rung's short, the two cancel and NET reads zero. That is
 * the strategy's central claim, shown rather than asserted.
 *
 * Signed quantity is a polarity, so the cell scale is diverging — one hue for
 * long, one for short, with an explicit neutral for flat. Every cell also
 * carries its number and a +/- sign, so nothing depends on colour alone.
 */
export function StrikeMatrix({
  rows,
  condors,
  expiry,
}: {
  rows: MatrixRow[];
  condors: Condor[];
  expiry?: string;
}) {
  const [right, setRight] = useState<"PE" | "CE">("PE");
  const [onlyOffset, setOnlyOffset] = useState(false);

  const expiries = useMemo(
    () => Array.from(new Set(rows.map((r) => r.expiry))).sort(),
    [rows],
  );
  const [selectedExpiry, setSelectedExpiry] = useState(expiry ?? expiries[0]);

  const visible = useMemo(() => {
    let out = rows.filter((r) => r.right === right && r.expiry === selectedExpiry);
    if (onlyOffset) out = out.filter((r) => r.is_flat);
    return out.sort((a, b) => b.strike - a.strike);
  }, [rows, right, selectedExpiry, onlyOffset]);

  const rungs = useMemo(() => {
    const indices = new Set<number>();
    visible.forEach((row) => Object.keys(row.by_condor).forEach((k) => indices.add(Number(k))));
    return condors.filter((c) => indices.has(c.index)).sort((a, b) => b.level - a.level);
  }, [visible, condors]);

  const maxQty = useMemo(
    () => Math.max(1, ...visible.flatMap((r) => Object.values(r.by_condor).map(Math.abs))),
    [visible],
  );

  const offsetCount = visible.filter((r) => r.is_flat).length;

  if (!expiries.length) return null;

  return (
    <div>
      {/* Filters in one row above the chart. */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <Toggle
          options={[
            { value: "PE", label: "Puts" },
            { value: "CE", label: "Calls" },
          ]}
          value={right}
          onChange={(v) => setRight(v as "PE" | "CE")}
        />
        {expiries.length > 1 && (
          <select
            value={selectedExpiry}
            onChange={(e) => setSelectedExpiry(e.target.value)}
            style={{
              padding: "5px 9px", fontSize: 12, fontFamily: "inherit", borderRadius: 6,
              border: "1px solid var(--border-strong)", background: "var(--surface)", color: "var(--ink)",
            }}
          >
            {expiries.map((e) => (
              <option key={e} value={e}>{e}</option>
            ))}
          </select>
        )}
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: "var(--ink-2)", cursor: "pointer" }}>
          <input type="checkbox" checked={onlyOffset} onChange={(e) => setOnlyOffset(e.target.checked)} />
          Only fully offset strikes
        </label>
        <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--ink-muted)" }}>
          {offsetCount} of {visible.length} strikes net to zero
        </span>
      </div>

      <div className="scroll-x">
        <table style={{ minWidth: 520 }}>
          <thead>
            <tr>
              <th style={{ left: 0, position: "sticky", zIndex: 2, background: "var(--surface-2)" }}>Strike</th>
              {rungs.map((c) => (
                <th key={c.index} style={{ textAlign: "center" }} title={`Rung opened at ${num(c.level)}`}>
                  {num(c.level)}
                </th>
              ))}
              <th style={{ textAlign: "center", borderLeft: "2px solid var(--border-strong)" }}>Net</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <tr key={`${row.right}-${row.strike}`}>
                <td
                  className="tnum"
                  style={{
                    fontWeight: 600, position: "sticky", left: 0,
                    background: "var(--surface-2)", zIndex: 1,
                  }}
                >
                  {num(row.strike)}
                  <span style={{ color: "var(--ink-muted)", fontWeight: 400, marginLeft: 4 }}>{row.right}</span>
                </td>
                {rungs.map((c) => {
                  const qty = row.by_condor[String(c.index)] ?? 0;
                  return <Cell key={c.index} qty={qty} max={maxQty} />;
                })}
                <td
                  className="tnum"
                  style={{
                    textAlign: "center",
                    fontWeight: 700,
                    borderLeft: "2px solid var(--border-strong)",
                    color: row.is_flat ? "var(--ink-muted)" : row.net_qty > 0 ? "var(--c1)" : "var(--c2)",
                    background: row.is_flat ? "var(--surface-3)" : undefined,
                  }}
                  title={row.is_flat ? "Fully offset by an opposing leg" : undefined}
                >
                  {row.is_flat ? "— 0" : signed(row.net_qty)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 12, alignItems: "center" }}>
        <Swatch color="var(--c1)" label="Long (bought)" />
        <Swatch color="var(--c2)" label="Short (sold)" />
        <Swatch color="var(--surface-3)" label="Net zero — offset" border />
        <span style={{ fontSize: 11.5, color: "var(--ink-muted)" }}>
          Quantities are in shares ({num(condors[0]?.legs[0]?.qty ?? 0)} = 1 lot).
        </span>
      </div>
    </div>
  );
}

function Cell({ qty, max }: { qty: number; max: number }) {
  if (qty === 0) {
    return <td style={{ textAlign: "center", color: "var(--border-strong)" }}>&middot;</td>;
  }
  const intensity = 0.14 + 0.5 * (Math.abs(qty) / max);
  const hue = qty > 0 ? "var(--c1)" : "var(--c2)";
  return (
    <td
      className="tnum"
      style={{ textAlign: "center", padding: 3 }}
      title={qty > 0 ? `Long ${qty} shares` : `Short ${Math.abs(qty)} shares`}
    >
      <span
        style={{
          display: "block",
          padding: "4px 6px",
          margin: 1,
          borderRadius: 5,
          background: `color-mix(in srgb, ${hue} ${(intensity * 100).toFixed(0)}%, transparent)`,
          color: "var(--ink)",
          fontWeight: 600,
          fontSize: 11.5,
        }}
      >
        {signed(qty)}
      </span>
    </td>
  );
}

const signed = (n: number) => (n > 0 ? `+${num(n)}` : num(n));

function Swatch({ color, label, border }: { color: string; label: string; border?: boolean }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--ink-2)" }}>
      <span
        style={{
          width: 13, height: 13, borderRadius: 4, background: color,
          border: border ? "1px solid var(--border-strong)" : "none", display: "inline-block",
        }}
      />
      {label}
    </span>
  );
}

function Toggle({
  options,
  value,
  onChange,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 3, background: "var(--surface-3)", padding: 3, borderRadius: 7 }}>
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
          style={{
            padding: "4px 12px", fontSize: 12, fontWeight: 600, border: "none", borderRadius: 5,
            cursor: "pointer", fontFamily: "inherit",
            background: value === o.value ? "var(--surface)" : "transparent",
            color: value === o.value ? "var(--ink)" : "var(--ink-muted)",
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
