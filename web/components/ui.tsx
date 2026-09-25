import type { ReactNode } from "react";

import { pct } from "@/lib/format";
import type { ExchangeUse } from "@/lib/types";

export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header
      style={{
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "space-between",
        gap: 20,
        marginBottom: 20,
        flexWrap: "wrap",
      }}
    >
      <div>
        <h1 style={{ margin: 0, fontSize: 21, fontWeight: 700, letterSpacing: "-0.02em" }}>{title}</h1>
        {subtitle && (
          <p style={{ margin: "5px 0 0", color: "var(--ink-muted)", fontSize: 13, maxWidth: "70ch" }}>
            {subtitle}
          </p>
        )}
      </div>
      {right}
    </header>
  );
}

export function Card({
  title,
  hint,
  children,
  right,
  pad = 16,
}: {
  title?: string;
  hint?: ReactNode;
  children: ReactNode;
  right?: ReactNode;
  pad?: number;
}) {
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      {title && (
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            justifyContent: "space-between",
            gap: 12,
            padding: "12px 16px",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <div>
            <h2 style={{ margin: 0, fontSize: 13.5, fontWeight: 600 }}>{title}</h2>
            {hint && (
              <p style={{ margin: "3px 0 0", fontSize: 11.5, color: "var(--ink-muted)", maxWidth: "80ch" }}>
                {hint}
              </p>
            )}
          </div>
          {right}
        </div>
      )}
      <div style={{ padding: pad }}>{children}</div>
    </section>
  );
}

/**
 * A single figure. Deliberately not a chart: one number with a label reads
 * faster than any plot of one number.
 */
export function Stat({
  label,
  value,
  delta,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: string;
  delta?: string;
  tone?: "neutral" | "pos" | "neg";
  hint?: string;
}) {
  const color = tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : "var(--ink)";
  return (
    <div className="card" style={{ padding: "12px 14px" }}>
      <div style={{ fontSize: 11, color: "var(--ink-muted)", fontWeight: 600, letterSpacing: "0.02em" }}>
        {label}
      </div>
      <div className="tnum" style={{ fontSize: 21, fontWeight: 700, color, marginTop: 4, letterSpacing: "-0.02em" }}>
        {value}
      </div>
      {delta && (
        <div className="tnum" style={{ fontSize: 11.5, color: "var(--ink-muted)", marginTop: 2 }}>
          {delta}
        </div>
      )}
      {hint && <div style={{ fontSize: 10.5, color: "var(--ink-muted)", marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export function StatGrid({ children, min = 160 }: { children: ReactNode; min?: number }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fit, minmax(${min}px, 1fr))`,
        gap: 10,
      }}
    >
      {children}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "pos" | "neg" | "warn" | "brand";
  title?: string;
}) {
  const tones = {
    neutral: { bg: "var(--surface-3)", fg: "var(--ink-2)" },
    pos: { bg: "var(--pos-soft)", fg: "var(--pos)" },
    neg: { bg: "var(--neg-soft)", fg: "var(--neg)" },
    warn: { bg: "var(--surface-3)", fg: "var(--warn)" },
    brand: { bg: "var(--brand-soft)", fg: "var(--brand)" },
  }[tone];
  return (
    <span
      title={title}
      style={{
        display: "inline-block",
        padding: "1.5px 7px",
        borderRadius: 5,
        fontSize: 10.5,
        fontWeight: 600,
        letterSpacing: "0.02em",
        background: tones.bg,
        color: tones.fg,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

/**
 * States plainly where the numbers came from.
 *
 * This is load-bearing, not decoration: with Choice historical option data
 * unavailable, every premium on the page is a Black-76 model output, and a
 * reader must not mistake that for broker-verified data.
 */
export function ProvenanceBanner({
  verified,
  realFraction,
  note,
  awaiting = false,
  hasData = true,
  legs,
  backupFraction = 0,
  exchangeFraction = 0,
  exchange,
}: {
  verified: boolean;
  realFraction: number;
  note: string;
  awaiting?: boolean;
  hasData?: boolean;
  /** Why legs were modelled, when the run recorded it. */
  legs?: {
    total?: number;
    real?: number;
    empty?: number;
    unused?: number;
    unresolved?: number;
    emptyExpiries?: string[];
    /** Priced from the backup source at least once. */
    backup?: number;
  };
  /** Share of price lookups the backup source answered. Counted inside
   *  realFraction: a backup price is a real traded price, not a model. */
  backupFraction?: number;
  /** Share answered by a real closing trade from the exchange's record. Also
   *  inside realFraction. */
  exchangeFraction?: number;
  /** How the run used the exchange's record, and how accurate the model was
   *  against it. */
  exchange?: ExchangeUse;
}) {
  if (awaiting) return <AwaitingConnection note={note} />;

  // With nothing computed there are no figures to caveat, and a "100% MODELED"
  // warning over an empty page reads as a problem rather than a prompt.
  if (!hasData) {
    return (
      <div className="card" style={{ padding: "11px 14px", display: "flex", gap: 9, alignItems: "center" }}>
        <Badge tone="brand">NO DATA YET</Badge>
        <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{note}</span>
      </div>
    );
  }

  const modeled = 1 - realFraction;
  if (modeled <= 0) {
    const others = backupFraction + exchangeFraction;
    const parts = [`${(100 * (1 - others)).toFixed(0)}% from Choice FinX`];
    if (backupFraction > 0) parts.push(`${(100 * backupFraction).toFixed(0)}% from the backup source`);
    if (exchangeFraction > 0) parts.push(`${(100 * exchangeFraction).toFixed(0)}% from the exchange's record of closing trades`);
    return (
      <div
        className="card"
        style={{ padding: "10px 14px", display: "flex", gap: 9, alignItems: "center", borderColor: "var(--pos)" }}
      >
        <Badge tone="pos">{others > 0 ? "REAL PRICES" : "VERIFIED"}</Badge>
        <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
          {others > 0
            ? `Every option premium is a real traded price: ${parts.join(", ")}, for contracts Choice has no history for.`
            : "All option premiums are real Choice FinX candles."}
        </span>
      </div>
    );
  }
  const accuracy = exchange?.accuracy ?? null;
  return (
    <div
      className="card"
      style={{
        padding: "11px 14px",
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
        borderColor: "var(--warn)",
        background: "var(--surface-2)",
      }}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" strokeWidth="2"
           strokeLinecap="round" style={{ flexShrink: 0, marginTop: 1 }} aria-hidden="true">
        <path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      </svg>
      <div>
        <div style={{ display: "flex", gap: 7, alignItems: "center", flexWrap: "wrap" }}>
          <Badge tone="warn">{(modeled * 100).toFixed(0)}% MODELED</Badge>
          <strong style={{ fontSize: 12.5 }}>These P&amp;L figures are not broker-verified.</strong>
        </div>
        <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--ink-muted)", maxWidth: "88ch", lineHeight: 1.55 }}>
          {note}
        </p>
        {/* The reason, not just the percentage. "76% modelled" invites a hunt
            for a misconfiguration; "Choice served no candles for three settled
            expiries" is the actual answer and is not fixable from here. */}
        {/* The reason, not just the percentage. The percentage counts every
            price lookup the replay made; the lines below count legs. */}
        {legs && (legs.empty || legs.unused || legs.unresolved) ? (
          <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--ink-2)", maxWidth: "88ch", lineHeight: 1.55 }}>
            <strong>{legs.real ?? 0} of {legs.total ?? 0} legs</strong> priced from real Choice candles.
            {legs.unused
              ? ` ${legs.unused} more came back with bars, but none close to the moments they were needed, so they were modelled as well.`
              : ""}
            {legs.empty
              ? ` Choice returned no bars at all for ${legs.empty}${
                  legs.emptyExpiries?.length ? ` (${legs.emptyExpiries.join(", ")})` : ""
                }.`
              : ""}
            {legs.unresolved ? ` ${legs.unresolved} could not be resolved to a contract.` : ""}
            {legs.backup
              ? ` Of the legs Choice could not price, ${legs.backup} were priced from real candles from the backup source instead.`
              : ""}
            {legs.backup || accuracy
              ? ""
              : " So far only the expiry that is currently trading has priced reliably from real data; a range inside it is the most trustworthy test."}
          </p>
        ) : null}
        {/* How far the model can be trusted, measured rather than asserted:
            the same model the replay used, checked every session against the
            exchange's closing prices for the legs this run traded. */}
        {accuracy ? (
          <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--ink-2)", maxWidth: "88ch", lineHeight: 1.55 }}>
            Where Choice had no candle, the model is anchored to the exchange&rsquo;s closing prices for the
            same contract the session before, moved by NIFTY and India VIX since. Checked against those closes
            on this run&rsquo;s own legs &mdash; {accuracy.checks.toLocaleString("en-IN")} checks across{" "}
            {accuracy.contracts} contracts &mdash; it was typically within{" "}
            <strong>{pct(accuracy.anchored.median_abs)}</strong>, averaging{" "}
            {pct(Math.abs(accuracy.anchored.bias))} {accuracy.anchored.bias >= 0 ? "high" : "low"}. The India VIX
            model alone was typically {pct(accuracy.vix_model.median_abs)} out, averaging{" "}
            {pct(Math.abs(accuracy.vix_model.bias))} {accuracy.vix_model.bias >= 0 ? "high" : "low"}.
          </p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Shown when Choice has not been connected.
 *
 * Choice is this project's data source -- the backup source only fills what
 * a backtest could not get from Choice, and a backtest needs Choice anyway --
 * so with no session there is genuinely nothing to display. Showing an empty state — and the exact steps
 * to fix it — is the honest alternative to filling the page with numbers from
 * a source this project is not allowed to use.
 */
export function AwaitingConnection({ note }: { note?: string }) {
  const steps: [string, ReactNode][] = [
    ["Generate an API key", <>At finx.choiceindia.com &rarr; Profile &rarr; Settings &rarr; Generate API Key.</>],
    ["Declare your static IP", <>Choice binds the key to it and rejects every other address. VPNs and proxies always fail this check.</>],
    ["Fill .env on that machine", <><code className="mono">CHOICE_VENDOR_ID</code>, <code className="mono">CHOICE_API_KEY</code>, <code className="mono">CHOICE_MOBILE_NO</code>.</>],
    ["Verify the connection", <><code className="mono">python -m engine.tools.doctor</code> — logs in, loads the scrip master, resolves an option and fetches candles.</>],
    ["Build the dataset", <><code className="mono">python -m engine.tools.seed</code>, then redeploy.</>],
  ];

  return (
    <section className="card" style={{ borderColor: "var(--brand)", overflow: "hidden" }}>
      <div style={{ padding: "16px 18px", borderBottom: "1px solid var(--border)", display: "flex", gap: 11 }}>
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" strokeWidth="2"
             strokeLinecap="round" style={{ flexShrink: 0, marginTop: 1 }} aria-hidden="true">
          <path d="M12 2a10 10 0 100 20 10 10 0 000-20zM12 8v5m0 3h.01" />
        </svg>
        <div>
          <h2 style={{ margin: 0, fontSize: 14.5, fontWeight: 700 }}>Awaiting Choice FinX connection</h2>
          <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-muted)", maxWidth: "84ch", lineHeight: 1.6 }}>
            {note ??
              "Choice FinX is this project's data source, so there is nothing to display until credentials are configured."}
          </p>
        </div>
      </div>
      <ol style={{ margin: 0, padding: "14px 18px 16px 36px", fontSize: 12.5, lineHeight: 1.85, color: "var(--ink-2)" }}>
        {steps.map(([title, body]) => (
          <li key={title} style={{ marginBottom: 4 }}>
            <strong>{title}.</strong> {body}
          </li>
        ))}
      </ol>
      <div style={{ padding: "12px 18px", borderTop: "1px solid var(--border)", background: "var(--surface-3)" }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--ink-muted)", marginBottom: 6, letterSpacing: "0.03em" }}>
          LOGIN FLOW (NON-INTERACTIVE)
        </div>
        <div className="mono" style={{ fontSize: 11.5, lineHeight: 1.9, color: "var(--ink-2)" }}>
          POST api/OpenAPIV1/LoginTOTP<span style={{ color: "var(--ink-muted)" }}> &nbsp;&larr; mobile, base64</span>
          <br />
          POST api/OpenAPIV1/GetClientLoginTOTP<span style={{ color: "var(--ink-muted)" }}> &nbsp;&rarr; Choice returns the OTP</span>
          <br />
          POST api/OpenAPIV1/ValidateTOTP<span style={{ color: "var(--ink-muted)" }}> &nbsp;&rarr; SessionId</span>
        </div>
        <p style={{ margin: "8px 0 0", fontSize: 11.5, color: "var(--ink-muted)", lineHeight: 1.6 }}>
          Choice serves the OTP itself, so no authenticator app is involved. The session is
          day-scoped &mdash; the engine re-authenticates automatically once per trading day.
        </p>
      </div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div style={{ padding: "28px 16px", textAlign: "center", color: "var(--ink-muted)", fontSize: 13 }}>
      {children}
    </div>
  );
}

/** Legend for multi-series charts. Identity is never colour-alone. */
export function Legend({ items }: { items: { color: string; label: string; dashed?: boolean }[] }) {
  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center" }}>
      {items.map((item) => (
        <span key={item.label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--ink-2)" }}>
          <svg width="14" height="10" aria-hidden="true">
            <line x1="0" y1="5" x2="14" y2="5" stroke={item.color} strokeWidth="2.5"
                  strokeDasharray={item.dashed ? "3 2" : undefined} strokeLinecap="round" />
          </svg>
          {item.label}
        </span>
      ))}
    </div>
  );
}
