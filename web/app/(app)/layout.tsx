import { redirect } from "next/navigation";
import { Nav } from "@/components/Nav";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SignOut } from "@/components/SignOut";
import { engine, engineConfigured, type EngineUser } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * Chrome for the signed-in application.
 *
 * The session is validated against the engine on every request rather than
 * trusted from the cookie alone: the cookie only proves a token was issued,
 * while the engine knows whether that token still maps to a live Choice
 * session. A token revoked, expired, or replaced by a newer login must stop
 * working immediately.
 */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const token = await getSessionToken();
  if (!token) redirect("/login");

  let user: EngineUser | null = null;
  let engineError: string | null = null;

  if (engineConfigured()) {
    try {
      user = (await engine.me(token)).user;
    } catch (err) {
      const status = (err as { status?: number }).status;
      if (status === 401) redirect("/login");
      engineError = (err as Error).message;
    }
  } else {
    engineError = "ENGINE_URL is not configured, so live Choice data is unavailable.";
  }

  const initials = (user?.name ?? "Choice user")
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("") || "CU";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Mark />
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, letterSpacing: "-0.01em" }}>
                Condor Ladder
              </div>
              <div style={{ fontSize: 10.5, color: "var(--ink-muted)" }}>NIFTY &middot; Choice FinX</div>
            </div>
          </div>
        </div>

        <Nav />

        <div className="sidebar-foot">
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="user-chip">
              <div className="user-avatar">{initials}</div>
              <div className="user-meta">
                <div className="user-name">{user?.name ?? "Signed in"}</div>
                <div className="user-sub">
                  {user ? `${user.mobile}${user.client_code ? ` · ${user.client_code}` : ""}` : "session active"}
                </div>
              </div>
            </div>
            <SignOut />
          </div>
          <ThemeToggle />
        </div>
      </aside>

      <main className="main">
        {engineError && (
          <div
            className="card"
            style={{ padding: "10px 14px", marginBottom: 16, borderColor: "var(--warn)", fontSize: 12.5 }}
            role="status"
          >
            <strong>Engine unreachable.</strong>{" "}
            <span style={{ color: "var(--ink-muted)" }}>{engineError}</span>
          </div>
        )}
        {children}
      </main>
    </div>
  );
}

/**
 * Choice "C" monogram with the wordmark's underline flourish, in their own
 * logo colours (#0f1621 ink, #2777f3 blue).
 */
function Mark() {
  return (
    <svg width="30" height="30" viewBox="0 0 64 64" aria-hidden="true" style={{ flexShrink: 0 }}>
      <rect width="64" height="64" rx="14" fill="#0f1621" />
      <path d="M44 22.5a15 15 0 1 0 0 19" fill="none" stroke="#ffffff" strokeWidth="7" strokeLinecap="round" />
      <path d="M17 51.5c7-3.6 23-3.6 30 0" fill="none" stroke="#2777f3" strokeWidth="4.5" strokeLinecap="round" />
    </svg>
  );
}
