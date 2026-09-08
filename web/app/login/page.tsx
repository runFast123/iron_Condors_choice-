import { LoginForm } from "@/components/LoginForm";
import { engine, engineConfigured } from "@/lib/engine";

export const dynamic = "force-dynamic";

export const metadata = { title: "Sign in | Condor Ladder" };

export default async function LoginPage() {
  const ready = engineConfigured();

  // Resolved server-side so the address is on the page before the first
  // sign-in attempt, rather than only appearing after a rejection.
  let engineIp: string | null = null;
  let engineReachable = false;
  if (ready) {
    try {
      const info = await engine.clientIp();
      engineIp = info.engine_egress_ip;
      engineReachable = true;
    } catch {
      engineReachable = false;
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <svg width="34" height="34" viewBox="0 0 64 64" aria-hidden="true">
            <rect width="64" height="64" rx="14" fill="#0f1621" />
            <path d="M44 22.5a15 15 0 1 0 0 19" fill="none" stroke="#ffffff" strokeWidth="7" strokeLinecap="round" />
            <path d="M17 51.5c7-3.6 23-3.6 30 0" fill="none" stroke="#2777f3" strokeWidth="4.5" strokeLinecap="round" />
          </svg>
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, letterSpacing: "-0.01em" }}>Condor Ladder</div>
            <div style={{ fontSize: 11.5, color: "var(--ink-muted)" }}>NIFTY &middot; Choice FinX</div>
          </div>
        </div>

        <h1 className="auth-title">Sign in with your Choice account</h1>
        <p className="auth-sub">
          Your Choice credentials <strong>are</strong> the login — there is no separate password for
          this app to store. They are used once to open a broker session and are never written to
          disk.
        </p>

        <LoginForm engineReady={ready} engineReachable={engineReachable} engineIp={engineIp} />

        <div className="auth-note">
          <div className="auth-note-title">How this works</div>
          <ol>
            <li>
              Generate an API key at <strong>finx.choiceindia.com</strong> &rarr; Profile &rarr;
              Settings &rarr; Generate API Key.
            </li>
            <li>
              Declare <strong>the engine server&apos;s static IP</strong> against that key — not your
              laptop&apos;s. Choice rejects requests from any other address, and VPNs always fail.
            </li>
            <li>
              Sign in here. We call <code className="mono">LoginTOTP</code> &rarr;{" "}
              <code className="mono">GetClientLoginTOTP</code> &rarr;{" "}
              <code className="mono">ValidateTOTP</code>; Choice returns the OTP itself, so there is
              no authenticator app.
            </li>
          </ol>
          <p className="auth-fine">
            Sessions are day-scoped, matching Choice&apos;s own — you will be signed out at end of
            day. Each user gets an isolated broker session; nobody sees anyone else&apos;s positions.
          </p>
        </div>
      </div>
    </div>
  );
}
