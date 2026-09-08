import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Condor Ladder | NIFTY",
  description:
    "Backtest and forward-test a laddered NIFTY iron-condor strategy on Choice FinX data.",
};

/**
 * Root shell only. The authenticated application chrome lives in
 * `app/(app)/layout.tsx`, so the sign-in page renders on its own without a
 * sidebar pointing at pages the visitor cannot reach yet.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
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
      <body>{children}</body>
    </html>
  );
}
