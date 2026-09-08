"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview", icon: "M3 12h4l3-8 4 16 3-8h4" },
  { href: "/ladder", label: "Strike Ladder", icon: "M4 6h16M4 12h16M4 18h16" },
  { href: "/payoff", label: "Payoff", icon: "M3 17l6-6 4 4 8-8" },
  { href: "/chart", label: "Price & Levels", icon: "M4 19V5m0 14h16M8 15l4-6 4 3 4-7" },
  { href: "/trades", label: "Trades", icon: "M4 6h16v12H4zM4 10h16" },
  { href: "/data", label: "Data Health", icon: "M12 3v18M5 8v8M19 8v8" },
  { href: "/about", label: "The Strategy", icon: "M12 8v5m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="nav">
      {LINKS.map((link) => {
        const active = path === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className="nav-link"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d={link.icon} />
            </svg>
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
