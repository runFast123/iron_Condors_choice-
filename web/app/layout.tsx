import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { ThemeToggle } from "@/components/ThemeToggle";
import { getDataset } from "@/lib/data";

export const metadata: Metadata = {
  title: "Iron Condor Ladder | NIFTY",
  description:
    "Backtest and forward-test a laddered NIFTY iron-condor strategy on Choice FinX data.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const { provenance } = getDataset();

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
        {/* Apply the stored theme before first paint so the page never flashes. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('ic-theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}`,
          }}
        />
      </head>
      <body>
        <div style={{ display: "flex", minHeight: "100vh" }}>
          <aside
            style={{
              width: 210,
              flexShrink: 0,
              borderRight: "1px solid var(--border)",
              background: "var(--surface-2)",
              display: "flex",
              flexDirection: "column",
              position: "sticky",
              top: 0,
              height: "100vh",
            }}
          >
            <div style={{ padding: "18px 16px 14px", borderBottom: "1px solid var(--border)" }}>
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

            <div style={{ marginTop: "auto", padding: 14, borderTop: "1px solid var(--border)" }}>
              <ThemeToggle />
              <div style={{ fontSize: 10, color: "var(--ink-muted)", marginTop: 10, lineHeight: 1.5 }}>
                Data {provenance.range[0]} &rarr; {provenance.range[1]}
                <br />
                {provenance.bars} bars &middot; {provenance.resolution}
              </div>
            </div>
          </aside>

          <main style={{ flex: 1, minWidth: 0, padding: "22px 26px 60px" }}>{children}</main>
        </div>
      </body>
    </html>
  );
}

/** Condor-wing mark in the Choice blue/gold pairing. */
function Mark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" style={{ flexShrink: 0 }}>
      <rect width="26" height="26" rx="7" fill="var(--brand)" />
      <path d="M4 16.5 L9 9.5 L13 14 L17 9.5 L22 16.5" fill="none" stroke="var(--brand-ink)"
            strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" opacity="0.95" />
      <circle cx="13" cy="14" r="1.9" fill="var(--accent)" />
    </svg>
  );
}
