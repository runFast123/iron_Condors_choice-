"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

export function LoginForm({ engineReady }: { engineReady: boolean }) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);

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

  return (
    <form onSubmit={onSubmit} className="auth-form" autoComplete="off">
      {!engineReady && (
        <div className="auth-alert auth-alert-info" role="status">
          <strong>Engine not configured.</strong> Set <code className="mono">ENGINE_URL</code> to the
          machine whose static IP is declared against your Choice API key, then reload. Sign-in
          cannot reach Choice until then.
        </div>
      )}

      {error && (
        <div className="auth-alert auth-alert-error" role="alert">
          {error}
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
