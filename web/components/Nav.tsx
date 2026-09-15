"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

type Item = {
  href: string;
  label: string;
  icon: string;
  /**
   * Whether this page is scoped to one forward run.
   *
   * These links used to drop the `run` query parameter, so someone watching
   * their second forward test in the Live Monitor clicked "Log & History" and
   * landed on whichever run the engine answered with by default -- reading
   * another test's fills and P&L under the heading they had just been on.
   */
  run?: boolean;
};

/**
 * Two working modes, kept visually separate: research on history, and running
 * the same engine forward on live Choice quotes.
 */
const SECTIONS: { title: string; items: Item[] }[] = [
  {
    title: "Backtest",
    items: [
      { href: "/", label: "Overview", icon: "M3 12h4l3-8 4 16 3-8h4" },
      { href: "/ladder", label: "Strike Ladder", icon: "M4 6h16M4 12h16M4 18h16" },
      { href: "/payoff", label: "Payoff", icon: "M3 17l6-6 4 4 8-8" },
      { href: "/chart", label: "Price & Levels", icon: "M4 19V5m0 14h16M8 15l4-6 4 3 4-7" },
      { href: "/trades", label: "Trades", icon: "M4 6h16v12H4zM4 10h16" },
    ],
  },
  {
    title: "Forward Test",
    items: [
      { href: "/forward", label: "Live Monitor", run: true, icon: "M12 2v4m0 12v4M2 12h4m12 0h4M7.8 7.8l2.8 2.8m2.8 2.8 2.8 2.8m0-8.4-2.8 2.8m-2.8 2.8-2.8 2.8" },
      { href: "/forward/log", label: "Log & History", run: true, icon: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" },
    ],
  },
  {
    title: "Reference",
    items: [
      { href: "/data", label: "Data Health", icon: "M12 3v18M5 8v8M19 8v8" },
      { href: "/about", label: "The Strategy", icon: "M12 8v5m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
    ],
  },
];

export function Nav() {
  // `useSearchParams` suspends, and this sits in the layout above every page.
  // Without the boundary it would opt the whole shell out of static rendering.
  return (
    <Suspense fallback={<NavLinks run={null} />}>
      <NavWithRun />
    </Suspense>
  );
}

function NavWithRun() {
  return <NavLinks run={useSearchParams().get("run")} />;
}

function NavLinks({ run }: { run: string | null }) {
  const path = usePathname();
  const normalise = (p: string) => (p !== "/" && p.endsWith("/") ? p.slice(0, -1) : p);
  const here = normalise(path);
  const linkTo = (item: Item) =>
    item.run && run ? `${item.href}?run=${encodeURIComponent(run)}` : item.href;

  return (
    <nav className="nav">
      {SECTIONS.map((section) => (
        <div key={section.title} className="nav-section">
          <div className="nav-title">{section.title}</div>
          {section.items.map((item) => (
            <Link
              key={item.href}
              href={linkTo(item)}
              aria-current={here === item.href ? "page" : undefined}
              className="nav-link"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={item.icon} />
              </svg>
              {item.label}
            </Link>
          ))}
        </div>
      ))}
    </nav>
  );
}
