# NIFTY Iron Condor Ladder

Backtest and forward-test a laddered NIFTY iron-condor strategy on Choice FinX data.

**Paper only.** This platform backtests and forward-tests. It places no orders — the
order-placement path does not exist in the codebase, so there is nothing to arm, disarm or
misconfigure. Choice is used strictly read-only: sign-in, scrip master, candles and quotes.

**Choice FinX is the only data source** — historical and live. There is no third-party market-data
vendor anywhere in this project.

**Multi-user:** each person signs in with their own Choice credentials. There is no master API key,
and no user can see another's session, positions or logs.

**Live dashboard:** _(see "Deploy" below)_

---

## The strategy

Open an iron condor at an anchor level, then open another at **every 100-point decline**. Each rung
is built the same way around its reference level `L`:

| Leg | Rule | At `L = 24,000` |
|---|---|---|
| BUY PE | `L − 400` | 23,600 PE |
| SELL PE | `L − 200` | 23,800 PE |
| SELL CE | `L + 200` | 24,200 CE |
| BUY CE | `L + 400` | 24,400 CE |

Rules confirmed and implemented:

- **Down-only.** Rallies open nothing, and each level fires **at most once** per campaign.
- **Fire on reach.** A level fires the moment price trades *at or below* it. At 23,899 the ladder
  has reached 23,900 and no further — it does not also fire 23,800.
- **Gap-fill** (toggleable). A gap-down opens every level it skipped, so an overnight drop builds
  the same ladder a gradual decline would.
- **Exits.** Hold to expiry by default, with optional per-rung take-profit (% of credit) and
  stop-loss (× credit).

### Why the legs cancel

The ladder steps 100 points, but the wings sit 200 and 400 points out — a 200-point gap, which is
**exactly two steps**. So the long put of one rung lands on the same strike as the short put of the
rung two steps below:

```
Rung 24,000  →  BUY  23,600 PE
Rung 23,800  →  SELL 23,600 PE
─────────────────────────────
NET             0        ← same strike, opposite sides
```

Over the seeded 13-month backtest this cancelled **50% of all quantity traded** — 20 of 40 strikes
netted to exactly zero. The `/ladder` page shows this cell by cell.

---

## Accounts & sign-in

Your Choice credentials **are** the login — there is no separate password for this app to store.
Sign-in performs a real Choice authentication; if Choice rejects the credentials, no session exists.

```
Browser ──POST /api/auth/login──▶ Next.js server ──▶ Engine (static IP) ──▶ Choice
   ▲                                                       │
   └────────── httpOnly session cookie ◀───── opaque token ─┘
```

| Property | How it is handled |
|---|---|
| Credentials | Forwarded once, exchanged for a Choice session, held **in memory only**. Never written to disk, logged, or returned to the browser. |
| Session token | Random and opaque — carries no user data to decode or tamper with. Stored in an `httpOnly`, `sameSite=Lax`, `secure` cookie, so page scripts cannot read it even if an XSS bug existed. |
| Session lifetime | Day-scoped, matching Choice's own. A token that outlived the broker session would only produce confusing 401s. |
| Re-login | Replaces the previous session, so a leaked token cannot outlive a fresh sign-in. |
| Brute force | Per-user attempt throttle on the engine, so the login endpoint is not an oracle for guessing API keys. |
| Identity shown | A salted hash of the mobile number; the number itself is only ever displayed masked (`********10`). |
| Isolation | One `ChoiceSession` and one forward runner per user, keyed by token. Verified by tests. |
| Validation | Every request re-checks the token against the engine, so revoked or expired sessions stop working immediately — the cookie alone is never trusted. |

### The static-IP constraint for multiple users

Choice binds each API key to a declared static IP (Integration Guide §8). With several users, each
one generates their own API key and declares **the engine server's static IP** against it — the
"bring your own key" pattern §6.2 describes. Their credentials still authenticate them individually.

> ⚠️ Operating this as a service for other people makes you a **Type B Technology Vendor** under
> §3.2, which requires a registered legal entity, **exchange empanelment with NSE/BSE/MCX**, and
> server co-location at Choice. Choice will not issue production vendor credentials without proof
> of empanelment. For personal use, or a handful of users who each bring their own key and declare
> your IP, the pattern above is what the guide describes. Anything commercial needs the empanelment
> route first.

---

## Architecture

Choice binds every API key to a **declared static IP** and rejects everything else
(Integration Guide §8 — VPNs and proxies always fail). Vercel's serverless egress IPs are dynamic,
so **no Choice call can originate from Vercel**. Hence the split:

```
[Vercel]  Next.js app (auth, UI, server routes)
                │  X-Engine-Key + Bearer token
                ▼
[Your static IP]  FastAPI engine ──HTTPS/WSS──▶  [Choice FinX API]   ← the only source
                   one Choice session per signed-in user
```

Vercel never talks to Choice — its egress IPs are dynamic and would be rejected. It calls the
engine, which runs where the declared static IP is. No credential ever reaches Vercel's storage:
the login request passes through, and only an opaque token comes back.

```
engine/
  api.py        FastAPI service: per-user auth, market data, forward control
  auth/         multi-user session registry, throttling, id derivation
  choice/       hardened adapter over the Choice API — the real deliverable
  pricing/      Black-76, IV surface, Indian F&O cost model
  strategy/     ladder trigger, condor construction, netting
  backtest/     two-pass runner, price providers, metrics
  data/         market data — Choice only (spot, India VIX, options, live quotes)
  tools/        doctor (connectivity diagnostic), seed (dataset builder)
web/            Next.js 15 dashboard
```

---

## What was wrong with `kkunal`, and what this fixes

The `kkunal` package (import name `choice_api`) is the supplied Choice SDK. Its `historical.py` is
**byte-identical between 1.2.0 and 1.3.0**, so upgrading does not help. This project wraps it rather
than forking it, and fixes the following in `engine/choice/`:

### Historical data — why it "just fails"

| Upstream behaviour | Consequence | Fixed in |
|---|---|---|
| `_parse_date` catches every exception and returns `0` | One bad date silently requests 45 years from the 1980 epoch | `history.to_ist` raises `ChoiceDateError` |
| Non-`Success` status returns an **empty DataFrame**, error discarded | A bad token, dead session, throttle and a market holiday are indistinguishable | `ChoiceHistoryError` vs `ChoiceNoDataError`, both carrying Choice's own message |
| No chunking or pagination | Long ranges come back empty with no explanation | Per-resolution windows + automatic bisection on failure |
| No retry, no rate limiting | Throttling looks like "no data" | Token bucket + exponential backoff with jitter, honours `Retry-After` |
| `requests` called with **no timeout** | A hung endpoint blocks forever | Explicit `(connect, read)` timeout on every call |
| `int(parts[5])` on `"1234.0"` | `ValueError` kills the whole batch | Per-row parsing with `int(float(x))` and bad-row accounting |
| Naive local-time epoch arithmetic | Intraday bars can be silently shifted 5.5h | Explicit IST + `calibrate_epoch()` probes the server rather than assuming |

### Everything else

- **No option resolver.** `ScripMaster.get_token()` is an exact, case-sensitive match, and its
  seven-key projection *drops strike, expiry and CE/PE*. `instruments.py` parses raw rows and builds
  an `(underlying, expiry, strike, right) → contract` index.
- **Locale-dependent scrip-master URL.** `strftime("%b")` 404s on a non-English Windows locale;
  month names are now hard-coded, and the fallback window is 7 days rather than 3.
- **`fetch()` appends without clearing**, duplicating the whole master on a second call. Fixed.
- **Contradictory NFO segment id** (the README says both `2` and `13`). Now inferred from real
  option rows at runtime via `infer_nfo_segment()`.
- **`ClientOrderNo` hardcoded to `123456`** on every order — fatal for a four-leg condor, since
  modify/cancel key off it. The order path calls the endpoint directly with unique ids.
- **`access_token` left `None`** when the login response is a bare string, so the price feed logs on
  with an empty token. Patched.
- **Missing dependency:** `websockets_feed.py` imports `websocket` but `websocket-client` is not in
  `install_requires`, so `import choice_api` fails on a clean install. Declared in `requirements.txt`.
- **Blank `MarketLot` → lot size 1**, which would place a 1-share order instead of one lot. Now `0`,
  so callers can detect it.
- Bare `Exception` everywhere, with response bodies interpolated into messages. Replaced with a typed
  hierarchy that scrubs credentials.

---

## Data sources — Choice only

| Series | Choice source |
|---|---|
| NIFTY spot | index token from the scrip master → `api/OpenGraph/ChartData` |
| India VIX | `INDIAVIX` index token → `api/OpenGraph/ChartData` |
| Option premiums | per-leg option tokens → `api/OpenGraph/ChartData` |
| Expiries, strikes, lot size | the daily scrip master CSV |
| Live quotes | `api/OpenAPI/MultipleTouchline` + the FIX3.0 streaming feed |

Every price carries a source tag, surfaced as a badge throughout the UI:

| Tag | Meaning |
|---|---|
| `CHOICE` | A real Choice candle or quote. |
| `MODELED` | Black-76, using volatility derived from Choice's own India VIX series. |

`MODELED` is not a second data vendor — it is a pricing model applied when Choice serves no candles
for a specific contract. Those legs are badged everywhere and excluded from the verified statistics.

### Authentication

Access requires a session. The flow is non-interactive because Choice serves the OTP itself:

```
POST api/OpenAPIV1/LoginTOTP           ← mobile number, base64-encoded
POST api/OpenAPIV1/GetClientLoginTOTP  → Choice returns the OTP
POST api/OpenAPIV1/ValidateTOTP        → SessionId
```

Every subsequent request then carries three headers: `VendorId`, `Bearer` (the API key), and
`Authorization: SessionId <id>`. Sessions are **day-scoped**, so the engine re-authenticates
automatically once per trading day. All of it must originate from the declared static IP.

Until credentials are configured the dashboard renders an explicit *"Awaiting Choice FinX
connection"* state rather than numbers from a source this project does not permit.

## Getting started

### Engine

```bash
pip install -r engine/requirements.txt
cp .env.example .env          # then fill in your Choice credentials
pytest engine/tests -q        # 127 tests, no credentials needed
```

Verify the Choice connection end to end — **run this from your declared static IP**:

```bash
python -m engine.tools.doctor
```

It logs in, loads the scrip master, resolves a real NIFTY option, calibrates the ChartData epoch and
fetches candles for both the index and one option leg. On failure it prints **Choice's own error
message**, which is the whole point.

Rebuild the dashboard dataset from Choice:

```bash
python -m engine.tools.seed --days 120 --resolution D
```

Without credentials this writes the "awaiting connection" placeholder instead of calling the API.

### Forward testing (live Choice data)

**Paper only.** There is no order-placement code in the engine, so no run can
place an order. That is a structural property, not a setting: the `NewOrder`
call, the arming step and the live mode were removed rather than disabled.

Start a run from the dashboard's **Forward Test** page. To run one from the box
itself instead:

```bash
python -m engine.tools.live                 # polls every 15s
python -m engine.tools.live --ticks 4       # a few cycles, then stop
```

It drives the same `Ladder` and `build_legs` as the backtester, so the two
cannot diverge.

**Fills cross the spread.** With no live path, the fill model *is* the result,
so filling at the last traded price would report a P&L nobody could have
traded. Buys lift the offer, sells hit the bid. Where Choice returns no depth a
spread is modelled (2% of premium by default, floored at one tick) rather than
assumed to be zero, and every run reports what fraction of its legs were priced
on a real book — see `fill_quality` in the state, surfaced on the page. While a
condor is open it is marked at mid; the cost of crossing is charged on entry and
exit, not smeared across every tick.

Other guards: a condor with any unquotable leg, or any book too wide to trade
through, is skipped rather than half-opened; a net-debit condor is flagged,
because an iron condor cannot be a debit unless the quotes are wrong; and a
daily-loss breach stops the run.

### Durability — runs survive a restart

State lives in SQLite at `engine/state/engine.db` (override with `ENGINE_DB`).

| Stored | Why |
|---|---|
| Forward runs | Ladder anchor, fired levels, open condors and their fills, as a resumable document |
| Tick history | So the live chart redraws the whole session on reload instead of starting empty |
| Backtest results | A run costs minutes of Choice calls; losing it to a restart is pure waste |
| Learned holidays | A closure discovered once is not rediscovered every session |
| User-id salt | Ids are a salted hash of the mobile number — a per-process salt would orphan every saved run on restart |

**No credential is ever written here.** API keys, session ids and access tokens
stay in memory and die with the process.

A run is resumed at **login**, not at startup: quotes need Choice credentials,
and those are deliberately not persisted. Until someone signs in, the run sits
in the database marked `running` — which is the truth, since it has positions
open and a ladder mid-flight and simply has nobody to ask for prices. On resume
the fired-level set is restored too, so a level already held is never opened
twice.

### Trading calendar

Weekday-and-clock is not a calendar: it says the market is open on Diwali.
Three sources are layered, most authoritative first:

1. **Choice's `MarketStatus` endpoint** — the exchange knows about unscheduled
   closures no static list can.
2. **Learned closures** — a weekday reported shut during session hours is
   remembered and persisted.
3. **`engine/data/nse_holidays.json`** — fixed-date national holidays only.
   Movable ones (Holi, Eid, Diwali) shift every year and are deliberately *not*
   guessed; add them from NSE's annual circular, or let source 1 teach them.

An ambiguous or unreachable `MarketStatus` falls back to the local calendar
rather than guessing in either direction.

### Keeping it running

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-engine.ps1
powershell -ExecutionPolicy Bypass -File scripts\run-tunnel.ps1 -UpdateVercel
```

`run-engine.ps1` restarts the engine when it exits, with a backoff, and loads
`.env.engine.local` — which is where `ENGINE_SHARED_SECRET` belongs. **An engine
started without that variable accepts every caller**, because an empty expected
key disables the check entirely.

`run-tunnel.ps1` addresses the quick-tunnel problem: `trycloudflare.com` mints a
new hostname on every start, so each restart silently breaks the deployed
dashboard until someone re-pastes the URL. The script reads the new hostname out
of cloudflared's output and pushes it to Vercel. Better still, use a named
tunnel once you have a domain — one hostname, set once:

```powershell
cloudflared tunnel login
cloudflared tunnel create iron-condor
cloudflared tunnel route dns iron-condor engine.yourdomain.com
.\scripts\run-tunnel.ps1 -Named iron-condor -TunnelHostname engine.yourdomain.com
```

Register either with Task Scheduler (`schtasks /create /sc onstart`) to survive
a reboot.

### Engine service (multi-user)

```bash
uvicorn engine.api:app --host 0.0.0.0 --port 8000
```

Runs on the static-IP machine. Holds one Choice session per signed-in user; set
`ENGINE_SHARED_SECRET` so only your web app can call it.

### Dashboard

```bash
cd web
npm install
ENGINE_URL=http://127.0.0.1:8000 npm run dev   # http://localhost:3000
npm run build && npm start
```

Everything except `/login` requires a session. To exercise the whole sign-in flow without real
credentials:

```bash
python -m engine.tests.fake_engine --port 8010          # real API, stubbed Choice
cd web && ENGINE_URL=http://127.0.0.1:8010 npm start -- --port 3010
python web/verify_auth.py --base http://127.0.0.1:3010  # 22 browser checks
```

> If `npm install` fails with `ECONNRESET` against `registry.npmjs.org`, your network is blocking it.
> Use a mirror: `npm install --registry=https://registry.npmmirror.com`.

---

## Deploy

```bash
npm i -g vercel
cd web
vercel login          # interactive, one time
vercel --prod
```

Set `ENGINE_URL` and `ENGINE_SHARED_SECRET` in the Vercel project. Without `ENGINE_URL` the app
still deploys and renders, but sign-in reports that the engine is unreachable — which is the
truthful state, since Choice cannot be called from Vercel.

---

## Security

- The Choice PDFs in this directory are marked **Confidential** and are `.gitignore`d. This repo is
  public — do not commit them.
- Credentials live only in `.env` on the engine machine, never in the web app and never in git.
- API keys are bound to a declared static IP. Rotate them if the IP changes
  (`finx.choiceindia.com` → Profile → Settings → Generate API Key).
- Order rate is capped below 10/sec by default: at or above that threshold SEBI/NSE require the
  strategy to be registered as an Algorithm with the exchange (Integration Guide §9).

---

## Status

Working end to end: multi-user sign-in against Choice, backtesting with real
Choice premiums (Black-76 where a leg has no history, badged `MODELED`), the
strike-offset matrix, payoff, and paper forward testing with a live chart.

**Scope: backtesting and paper forward testing only.** No real money, by
construction — the order path does not exist. Consequently the open questions
that would matter for live trading (order reconciliation, margin, SPAN) do not
arise here.

Known limits, stated plainly:

- The **modelled spread is a parameter, not a measurement**. Where Choice
  returns no depth, 2% of premium is an assumption; tune it once you can
  compare against real books. Every run reports what fraction it had to model.
- **Option history depth** from `ChartData` for NFO strikes is whatever Choice
  serves; anything missing falls back to Black-76 and is badged, never silently
  substituted.
- The **holiday file carries fixed-date holidays only** — movable ones are
  learned from `MarketStatus` or added by hand.
- The strategy itself lost money over the seeded range, and honouring expiry
  rolls *reduced* the offsetting benefit (50% → 40%), because the two-step
  offset needs three condors deep in one expiry and weekly expiries rarely
  allow it. Worth testing against monthly expiries before drawing conclusions.
