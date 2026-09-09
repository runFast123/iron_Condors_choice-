/**
 * Shown while a page waits on the engine.
 *
 * Every page here is `force-dynamic` and fetches from the engine on the
 * server, so without this file Next flushes nothing until that call returns:
 * a hard load is a blank white tab for the full round-trip, and an in-app
 * navigation leaves the *previous* page on screen with no sign anything is
 * happening. The engine sits behind a tunnel and can take seconds.
 *
 * A skeleton costs one file and turns that into instant feedback.
 */
export default function Loading() {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading data from the engine…</span>

      <div style={{ marginBottom: 22 }}>
        <div className="skeleton" style={{ width: 190, height: 26, marginBottom: 10 }} />
        <div className="skeleton" style={{ width: "min(560px, 90%)", height: 13 }} />
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          gap: 10,
          marginBottom: 18,
        }}
      >
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="card" style={{ padding: "11px 13px" }}>
            <div className="skeleton" style={{ width: "55%", height: 10, marginBottom: 9 }} />
            <div className="skeleton" style={{ width: "75%", height: 19 }} />
          </div>
        ))}
      </div>

      <div className="card" style={{ padding: 16 }}>
        <div className="skeleton" style={{ width: 150, height: 13, marginBottom: 14 }} />
        <div className="skeleton" style={{ width: "100%", height: 210 }} />
      </div>
    </div>
  );
}
