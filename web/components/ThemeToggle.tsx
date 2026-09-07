"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const stored = (() => {
      try { return localStorage.getItem("ic-theme") as Theme | null; } catch { return null; }
    })();
    const initial =
      stored ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setTheme(initial);
  }, []);

  function apply(next: Theme) {
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ic-theme", next); } catch { /* private mode */ }
  }

  return (
    <div style={{ display: "flex", gap: 4, background: "var(--surface-3)", padding: 3, borderRadius: 7 }}>
      {(["light", "dark"] as Theme[]).map((option) => (
        <button
          key={option}
          onClick={() => apply(option)}
          aria-pressed={theme === option}
          style={{
            flex: 1,
            padding: "5px 8px",
            fontSize: 11.5,
            fontWeight: 600,
            textTransform: "capitalize",
            border: "none",
            borderRadius: 5,
            cursor: "pointer",
            fontFamily: "inherit",
            background: theme === option ? "var(--surface)" : "transparent",
            color: theme === option ? "var(--ink)" : "var(--ink-muted)",
          }}
        >
          {option}
        </button>
      ))}
    </div>
  );
}
