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
              <ThemeToggle />
              <div className="data-range" style={{ fontSize: 10, color: "var(--ink-muted)", lineHeight: 1.5 }}>
                Data {provenance.range[0]} &rarr; {provenance.range[1]}
                <br />
                {provenance.bars} bars &middot; {provenance.resolution}
              </div>
            </div>
          </aside>

          <main className="main">{children}</main>
        </div>
      </body>
    </html>
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
