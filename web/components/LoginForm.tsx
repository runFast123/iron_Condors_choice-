"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";

/** Does this failure mean "the address is wrong" rather than "the key is wrong"? */
function isIpFailure(message: string): boolean {
  const m = message.toLowerCase();
  return m.includes("undeclared ip") || m.includes("static ip") || m.includes("ip address");
}

export function LoginForm({
  engineReady,
  engineReachable,
  engineIp,
}: {
  engineReady: boolean;
  engineReachable: boolean;
  engineIp: string | null;
}) {
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied] = useState(false);

  async function copyIp() {
    if (!engineIp) return;
    try {
      await navigator.clipboard.writeText(engineIp);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked; the address is on screen to copy by hand */
    }
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const data = new FormData(event.currentTarget);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          vendor_id: String(data.get("vendor_id") ?? ""),
          api_key: String(data.get("api_key") ?? ""),
          mobile: String(data.get("mobile") ?? ""),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(body.error ?? `Sign-in failed (${res.status}).`);
        setPending(false);
        return;
      }
      // Full navigation so the server re-reads the new session cookie.
      const next = params.get("next");
      window.location.href = next && next.startsWith("/") ? next : "/";
    } catch (err) {
      setError((err as Error).message || "Network error.");
      setPending(false);
    }
  }

  const ipRejected = error !== null && isIpFailure(error);

  return (
    <form onSubmit={onSubmit} className="auth-form" autoComplete="off">
      {!engineReady && (
        <div className="auth-alert auth-alert-info" role="status">
          <strong>Engine not configured.</strong> Set <code className="mono">ENGINE_URL</code> to the
          machine whose static IP is declared against your Choice API key, then reload. Sign-in
          cannot reach Choice until then.
        </div>
      )}

      {engineReady && !engineReachable && (
        <div className="auth-alert auth-alert-error" role="status">
          <strong>Engine unreachable.</strong> The address in{" "}
          <code className="mono">ENGINE_URL</code> did not respond. Start it with{" "}
          <code className="mono">uvicorn engine.api:app</code> on your static-IP machine.
        </div>
      )}

      {error && (
        <div className="auth-alert auth-alert-error" role="alert">
          {error}
        </div>
      )}

      {/* The address Choice actually enforces on. Shown up front, because a
          rejected IP is the most common reason valid credentials fail. */}
      {engineIp && (
        <div className={`ip-panel${ipRejected ? " ip-panel-alert" : ""}`}>
          <div className="ip-panel-label">
            {ipRejected
              ? "Register THIS address with Choice"
              : "Declare this IP against your API key"}
          </div>
          <div className="ip-panel-row">
            <code className="ip-value mono">{engineIp}</code>
            <button type="button" className="ip-copy" onClick={copyIp}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <div className="ip-panel-hint">
            This is the engine&apos;s outbound address — the one Choice sees. Your own browser IP is
            irrelevant, and a VPN on this machine will break it.
          </div>
        </div>
      )}

      <label className="auth-label" htmlFor="vendor_id">
        Vendor ID
      </label>
      <input
        id="vendor_id"
        name="vendor_id"
        className="auth-input"
        required
        autoComplete="username"
        spellCheck={false}
        placeholder="Issued by Choice"
      />

      <label className="auth-label" htmlFor="mobile">
        Registered mobile number
      </label>
      <input
        id="mobile"
        name="mobile"
        className="auth-input"
        required
        inputMode="numeric"
        pattern="[0-9+\-\s]{6,20}"
        autoComplete="tel"
        placeholder="The number on your Choice account"
      />

      <label className="auth-label" htmlFor="api_key">
        API key
      </label>
      <div className="auth-input-wrap">
        <input
          id="api_key"
          name="api_key"
          className="auth-input"
          required
          type={showKey ? "text" : "password"}
          autoComplete="current-password"
          spellCheck={false}
          placeholder="Shown once when you generate it"
        />
        <button
          type="button"
          className="auth-reveal"
          onClick={() => setShowKey((v) => !v)}
          aria-label={showKey ? "Hide API key" : "Show API key"}
        >
          {showKey ? "Hide" : "Show"}
        </button>
      </div>

      <button type="submit" className="auth-submit" disabled={pending}>
        {pending ? "Signing in to Choice…" : "Sign in"}
      </button>

      <p className="auth-fine">
        Sent once over HTTPS to your engine, exchanged for a broker session, and held in memory
        only. Never stored in this app, never placed in browser storage, and never returned to the
        page.
      </p>
    </form>
  );
}
