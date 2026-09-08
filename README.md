# NIFTY Iron Condor Ladder

Backtest and forward-test a laddered NIFTY iron-condor strategy on Choice FinX data.

**Choice FinX is the only data source** — historical and live. There is no third-party market-data
vendor anywhere in this project.

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

## Architecture

Choice binds every API key to a **declared static IP** and rejects everything else
(Integration Guide §8 — VPNs and proxies always fail). Vercel's serverless egress IPs are dynamic,
so **no Choice call can originate from Vercel**. Hence the split:

```
[Vercel]  Next.js dashboard  ── static export, no server at request time
                ▲
                │ reads a JSON bundle built by the engine
                │
[Your static IP]  Python engine ──HTTPS/WSS──▶  [Choice FinX API]   ← the only source
```

The dashboard is a pure static site. Nothing it serves can leak a credential, and it stays browsable
whether or not the engine is running.

```
engine/
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

```bash
python -m engine.tools.live                    # paper mode, polls every 15s
python -m engine.tools.live --ticks 4          # a few cycles, then stop
python -m engine.tools.live --mode live --arm  # REAL ORDERS
```

Writes `web/data/live.json` on every tick, which the dashboard's **Forward Test** section reads
(Live Monitor, plus Log & History). It drives the same `Ladder` and `build_legs` as the backtester,
so the two cannot diverge. Safety: paper by default; live needs `--arm`; a rung with any unquotable
leg is skipped rather than half-opened; protective wings are sent before shorts; limit orders only
(Choice has no market order); and a daily-loss breach disarms the runner.

### Dashboard

```bash
cd web
npm install
npm run dev      # http://localhost:3000
npm run build    # static export to web/out/
```

> If `npm install` fails with `ECONNRESET` against `registry.npmjs.org`, your network is blocking it.
> Use a mirror: `npm install --registry=https://registry.npmmirror.com`.

---

## Deploy

The dashboard is a static export, so any static host works. For Vercel:

```bash
npm i -g vercel
cd web
vercel login          # interactive, one time
vercel --prod
```

Vercel auto-detects Next.js; no environment variables are required, because the site ships its data
as a build-time bundle and never calls Choice.

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

Implemented and tested: the hardened Choice adapter, option resolver, ladder engine, condor
construction and netting, Black-76 pricing with greeks, the Indian cost model, the two-pass
backtester, the Choice-only market-data layer, the forward-test runner, and the full dashboard.

Not yet built: live forward-testing (order placement and the websocket feed have adapters but no
runner), and Postgres persistence — the dashboard currently reads a JSON bundle. Neither can be
meaningfully exercised without credentials on a static IP.

The shipped dataset is the empty "awaiting connection" placeholder, because Choice is the only
permitted source and no credentials are configured yet.

**This is research software, not trading advice.** The seeded results are modeled, and modeled
option premiums are not what you would have been filled at.
