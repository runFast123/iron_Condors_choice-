/**
 * Server-side client for the engine API.
 *
 * The engine runs on the machine whose static IP is declared against each
 * user's Choice API key. Everything Choice-related happens there; this app
 * never sees a credential after the login request has been forwarded.
 */

export const ENGINE_URL = (process.env.ENGINE_URL ?? "").replace(/\/$/, "");
const ENGINE_KEY = process.env.ENGINE_SHARED_SECRET ?? "";

export const engineConfigured = () => ENGINE_URL.length > 0;

export class EngineError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function call<T>(
  path: string,
  { method = "GET", body, token }: { method?: string; body?: unknown; token?: string | null } = {},
): Promise<T> {
  if (!engineConfigured()) {
    throw new EngineError(
      "ENGINE_URL is not configured. The engine must run on the machine whose static IP is declared against your Choice API key.",
      503,
    );
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (ENGINE_KEY) headers["X-Engine-Key"] = ENGINE_KEY;
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${ENGINE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    throw new EngineError(
      `Could not reach the engine at ${ENGINE_URL}. Is it running? (${(err as Error).message})`,
      504,
    );
  }

  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON error body */
  }

  if (!res.ok) {
    const detail =
      (parsed as { detail?: string } | null)?.detail ?? text.slice(0, 300) ?? res.statusText;
    throw new EngineError(detail || `Engine returned ${res.status}`, res.status);
  }
  return parsed as T;
}

export interface EngineUser {
  user_id: string;
  mobile: string;
  vendor_id: string;
  name: string | null;
  client_code: string | null;
  created_at: string;
  expires_at: string;
  has_market_data: boolean;
  forward_running: boolean;
}

export const engine = {
  health: () => call<{ ok: boolean; market_open: boolean; sessions: number }>("/health"),

  login: (vendor_id: string, api_key: string, mobile: string) =>
    call<{ token: string; user: EngineUser }>("/auth/login", {
      method: "POST",
      body: { vendor_id, api_key, mobile },
    }),

  logout: (token: string) => call<{ ok: boolean }>("/auth/logout", { method: "POST", token }),

  me: (token: string) => call<{ user: EngineUser; market_open: boolean }>("/me", { token }),

  spot: (token: string) =>
    call<{ symbol: string; token: number; ltp: number | null; ts: string }>("/market/spot", { token }),

  forwardState: (token: string) =>
    call<{ running: boolean; state: unknown }>("/forward/state", { token }),

  forwardStart: (token: string, body: { mode: string; arm: boolean; lots: number; step: number }) =>
    call<{ ok: boolean; state: unknown }>("/forward/start", { method: "POST", token, body }),

  forwardStop: (token: string) =>
    call<{ ok: boolean; state: unknown }>("/forward/stop", { method: "POST", token }),
};
