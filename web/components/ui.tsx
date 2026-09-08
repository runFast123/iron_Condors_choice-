import type { ReactNode } from "react";

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
}: {
  verified: boolean;
  realFraction: number;
  note: string;
  awaiting?: boolean;
}) {
  if (awaiting) return <AwaitingConnection note={note} />;

  const modeled = 1 - realFraction;
  if (modeled <= 0) {
    return (
      <div
        className="card"
        style={{ padding: "10px 14px", display: "flex", gap: 9, alignItems: "center", borderColor: "var(--pos)" }}
      >
        <Badge tone="pos">VERIFIED</Badge>
        <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
          All option premiums are real Choice FinX candles.
        </span>
      </div>
    );
  }
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
      </div>
    </div>
  );
}

/**
 * Shown when Choice has not been connected.
 *
 * Choice is the only permitted data source, so with no session there is
 * genuinely nothing to display. Showing an empty state — and the exact steps
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
              "Choice is the only permitted data source for this project, so there is nothing to display until credentials are configured."}
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
