"use client";

/**
 * Shown when a page throws while rendering.
 *
 * Without this file Next serves its own bare "Application error: a server-side
 * exception has occurred" — no sidebar, no explanation, and no way back except
 * the browser's Back button. A trader hitting that has no idea whether their
 * positions are fine.
 */
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div style={{ maxWidth: "70ch" }}>
      <h1 style={{ fontSize: 19, fontWeight: 700, margin: "0 0 10px" }}>
        This page could not be loaded
      </h1>
      <p style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.7, margin: "0 0 6px" }}>
        Something went wrong while building this view. Nothing has been traded and no
        saved result has been changed — this platform places no orders, and a forward
        run keeps ticking on the engine whether or not this page renders.
      </p>
      <p style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.7, margin: "0 0 16px" }}>
        Most often this means the engine returned something unexpected. Try again, and
        if it persists check the engine log.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button onClick={reset} className="auth-submit" style={{ marginTop: 0, minWidth: 130 }}>
          Try again
        </button>
        <a href="/" className="btn-quiet" style={{ display: "inline-flex", alignItems: "center" }}>
          Back to overview
        </a>
      </div>

      {error.digest && (
        <p style={{ fontSize: 11.5, color: "var(--ink-muted)", marginTop: 18 }}>
          Reference <code>{error.digest}</code> — quote this when checking the engine log.
        </p>
      )}
    </div>
  );
}
