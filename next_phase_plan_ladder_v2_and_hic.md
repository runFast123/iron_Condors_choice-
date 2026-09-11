# Next-Phase Plan: Ladder v2 + Hybrid Iron Condor (HIC)

NIFTY monthly options · both strategies held to monthly expiry · no take-profit or stop-loss
Revision 2: HIC halves confirmed as **debit spreads** (the condor's side, same strikes, bought instead of sold).

Two workstreams, built as two isolated strategies, each with its own state, risk limits and dashboard.

| Workstream | What it is | Type |
|---|---|---|
| Ladder v2 | Your current down-only condor ladder, plus an optional up-side (a condor on every 100-pt rally too) | Improvement to Strategy 1 |
| HIC | Full condors at the anchor and at the first step either way. Beyond that, one debit spread per 100-pt step, in the direction of the move | New Strategy 2 |

---

## 1. The current ladder, and two things worth knowing

Restating the rules so you can check my understanding:
- **Anchor:** spot floored to 100.
- **Entries:** every new 100-pt low opens a condor at that level: BUY PE −400, SELL PE −200, SELL CE +200, BUY CE +400. Wings are placed first.
- **Firing:** each level fires once, and gap-downs fill the levels they skipped.
- **Limits:** 20-condor cap, 1 lot per condor.
- **Exit:** everything is held to expiry.

**The cancellation works on both sides.** Your write-up shows the put side. The call side cancels the same way:

| | Put side | Call side |
|---|---|---|
| Condor 24,000 | BUY 23,600 PE · SELL 23,800 PE | SELL 24,200 CE · BUY 24,400 CE |
| Condor 23,800 | BUY 23,400 PE · SELL 23,600 PE | SELL 24,000 CE · BUY 24,200 CE |
| Nets to zero | 23,600 PE | 24,200 CE |

**Consequence 1: the broker always carries eight legs.** Take a continuous ladder whose highest level is T and lowest is B. Everything in between cancels, and this is what remains:

| Leg | Strikes |
|---|---|
| SELL PE | T − 200, T − 300 |
| BUY PE | B − 300, B − 400 |
| SELL CE | B + 200, B + 300 |
| BUY CE | T + 300, T + 400 |

I checked this for n = 2, 3, 6 and 20 condors, and it is always 8 legs. Those spreads are 100 × n points wide. So netting cuts the contracts you carry and the exposure margin, but not the risk. The worst case at expiry is still n × 200 × 65 − credits.

**Consequence 2: a long ladder needs rich credits even in its best case.** A condor at level x:
- loses nothing if expiry is within 200 of x;
- loses 100 if expiry is 300 away;
- loses the full 200 if expiry is 400 or more away.

On a 100-pt grid, expiry can be inside the zero-loss zone of at most five condors. Everything else loses.

| Condors open | Worst case, gross | Best case, gross | Avg credit per condor needed to break even in the best case |
|---|---|---|---|
| 5 | 1,000 pts (₹65,000) | 0 | 0 |
| 10 | 2,000 pts (₹1,30,000) | 800 pts | 80 |
| 15 | 3,000 pts (₹1,95,000) | 1,800 pts | 120 |
| 20 | 4,000 pts (₹2,60,000) | 2,800 pts | 140 |

"Gross" means before credits. The net result at expiry is total credit collected minus gross loss. If your average credit per condor is below the figure for n, a ladder that reaches n condors cannot finish positive at expiry, wherever NIFTY settles.

The cap also has a side effect in long trends. Once it is hit, the levels nearest to where the trend eventually stops never get a condor. So every open condor finishes far from settlement.

---

## 2. Phase 1: Ladder v2 (two-way option)

### 2.1 Rule change

New setting: `direction: down | up | both`.
- `down` is today's behaviour and stays the default.
- In `up` or `both`, every new 100-pt high above the anchor opens a condor built exactly the same way.
- Each level still fires at most once per cycle.
- The gap-fill toggle applies to gap-ups too.

### 2.2 Example (anchor 23,400; market goes to 23,300, then 23,500)

| Level | Trigger | BUY PE | SELL PE | SELL CE | BUY CE |
|---|---|---|---|---|---|
| 23,400 | anchor | 23,000 | 23,200 | 23,600 | 23,800 |
| 23,300 | first −100 | 22,900 | 23,100 | 23,500 | 23,700 |
| 23,500 | first +100 (new) | 23,100 | 23,300 | 23,700 | 23,900 |

Two strikes net to zero:
- **23,100 PE:** bought by 23,500, sold by 23,300.
- **23,700 CE:** sold by 23,500, bought by 23,300.

Cancellation only needs two levels 200 apart, not a particular order. So it works in both directions, and the eight-leg formula above still holds, with T = highest fired level and B = lowest.

### 2.3 Caps

| Setting | Value | Reason |
|---|---|---|
| `max_down` | 20 | Unchanged |
| `max_up` | 10 | Start smaller; rally-side credits are usually thinner |
| `max_total` | 20 | Worst case never exceeds today's 4,000 pts |

A level that arrives after its cap is hit is marked `CAPPED` and never opens in that cycle.

### 2.4 Anchor mode

With `floor`, spot sits 0–99 pts above the anchor at the start. So the first up-level is on average ~50 pts away, while the first down-level is ~150 pts away. That is fine for a down-only ladder but lopsided for a two-way one.

- Keep `floor` for `down`, so results stay identical to today.
- Default to `nearest` for `both`.
- Backtest both (run B3).

### 2.5 My view

Build it, but keep it off until the backtest proves it.

It fixes a real gap. Today, a rally that stalls leaves you holding one anchor condor losing on its call side. A two-way ladder would have fresh condors near settlement.

The costs:
- Every condor is a bet that expiry lands near its own level. Months that swing both ways leave you with more condors far from settlement.
- NIFTY IV usually falls on rallies, so up-side condors collect less credit for the same 200-pt risk.

See §4 for how it compares by month type.

### 2.6 Acceptance

- Down mode reproduces the current backtest trade-for-trade: same levels, strikes, fills and P&L.
- A unit test covers the §2.2 example, including the netting.
- Reports and dashboard split P&L into down-side and up-side, so the up-side's contribution is visible on its own.

---

## 3. Phase 2: Hybrid Iron Condor (HIC)

### 3.1 Rules

Levels are counted in steps from the anchor: k = 0 is the anchor, k = −1 is 100 below, k = +1 is 100 above.

| Zone | Levels (anchor 23,400) | Opens | Legs |
|---|---|---|---|
| Anchor (k = 0) | 23,400 | Full condor (credit) | BUY 23,000 PE · SELL 23,200 PE · SELL 23,600 CE · BUY 23,800 CE |
| First step down (k = −1) | 23,300 | Full condor (credit) | BUY 22,900 PE · SELL 23,100 PE · SELL 23,500 CE · BUY 23,700 CE |
| First step up (k = +1) | 23,500 | Full condor (credit) | BUY 23,100 PE · SELL 23,300 PE · SELL 23,700 CE · BUY 23,900 CE |
| Further down (k ≤ −2) | 23,200, 23,100 … | Put debit spread | at 23,200: BUY 23,000 PE · SELL 22,800 PE |
| Further up (k ≥ +2) | 23,600, 23,700 … | Call debit spread | at 23,600: BUY 23,800 CE · SELL 24,000 CE |

The debit spreads use the same strikes as the condor's side at that level, with buy and sell reversed:
- **Puts:** BUY PE x − 200 · SELL PE x − 400
- **Calls:** BUY CE x + 200 · SELL CE x + 400

Other rules:
- Both first-step levels are full condors if the market reaches both.
- Each level fires once per cycle.
- Gaps fill skipped levels, each with its own correct type.
- The BUY leg always goes in first.
- All units use the current monthly contract and are held to expiry.

The width of the core zone is a setting, `full_band_steps`, with default 1. Setting it to 0 makes only the anchor a full condor, with spreads from the first step.

### 3.2 Walk-through: NIFTY falls 23,400 → 22,800

| # | Spot reaches | k | Opens | Effect at the broker |
|---|---|---|---|---|
| 1 | 23,400 | 0 | Full condor | — |
| 2 | 23,300 | −1 | Full condor | — |
| 3 | 23,200 | −2 | BUY 23,000 PE · SELL 22,800 PE | BUY 23,000 PE adds to #1's long 23,000 PE (now long 2) |
| 4 | 23,100 | −3 | BUY 22,900 PE · SELL 22,700 PE | BUY 22,900 PE adds to #2's long 22,900 PE (now long 2) |
| 5 | 23,000 | −4 | BUY 22,800 PE · SELL 22,600 PE | BUY 22,800 PE closes #3's short 22,800 PE |
| 6 | 22,900 | −5 | BUY 22,700 PE · SELL 22,500 PE | BUY 22,700 PE closes #4's short 22,700 PE |
| 7 | 22,800 | −6 | BUY 22,600 PE · SELL 22,400 PE | BUY 22,600 PE closes #5's short 22,600 PE |

Netting still happens, but differently from the ladder. The debit spreads net against each other two steps apart, and they stack on the core condors' wings instead of cancelling them.

On a one-way move, the broker's put book stays at six strikes however far it runs:
- SELL 23,200 and 23,100
- BUY 2× 23,000 and 2× 22,900
- SELL the two strikes 300 and 400 below the lowest spread level (here 22,500 and 22,400)

Add the core condors' four call legs and that is 10 strikes in total.

What happens next. This is illustrative, assuming ~130 pts credit per core condor and ~65 debit per spread:

| If NIFTY then… | Units | Expiry P&L |
|---|---|---|
| Stalls at 22,800 | 7 | −165 pts |
| Bounces back to 23,400 | 7 | −65 pts |
| Keeps falling to 22,000 (spreads cap at 10) | 12 | +1,110 pts |

### 3.3 How HIC makes and loses money

| Unit | Pays at expiry when | Worst case | Cash flow |
|---|---|---|---|
| Core condor at x | Expiry within ~200 of x (keeps its credit) | 200 × 65 − credit | Credit |
| Put debit spread bought at x | Expiry below x − 200; full 200 at x − 400 or lower | Debit paid | Debit (roughly half a condor's credit) |
| Call debit spread bought at x | Expiry above x + 200; full 200 at x + 400 or higher | Debit paid | Debit |

The core condors earn in quiet months. The debit spreads earn only when the move keeps going past them.

That makes HIC close to the mirror image of the ladder. It wins big in one-way trends, where the ladder loses. It loses when the market moves far enough to buy spreads and then turns back.

Its worst month type is a swing through the anchor. Spreads get bought on both sides and expire worthless, and a core condor loses on the far side.

### 3.4 The protection gap, and one variant worth testing

With the same strikes reversed, each spread starts paying 200 pts below the level where it was bought. The first one, bought at 23,200, owns the 23,000 PE. That is exactly where the anchor condor's put side has already hit its full loss.

So from 23,200 down to 23,000, nothing protects the core. In months that fall ~500 and stall, the last few spreads expire worthless.

The plan keeps your strikes as the default and adds one setting for the backtest: `debit_shift`. At 200, the spread is bought at the level itself (at 23,200: BUY 23,200 PE · SELL 23,000 PE). It costs more, assumed ~90 instead of ~65. In the same simulation:

| Path | Same strikes (default) | Shifted 200 |
|---|---|---|
| 500 down (or up), stalls | −300 | 0 |
| 1,500 one-way trend | +1,210 | +960 |
| 500 out, back to anchor | 0 | −100 |
| Swing through the anchor | −565 | −440 |

Quiet months are +390 either way. Run it as H3 and let real credits and debits decide.

### 3.5 Parameters

```yaml
strategy_id: hic
underlying: NIFTY
expiry: monthly              # all units in a cycle use the current monthly contract
step: 100
short_offset: 200
long_offset: 400
lots_per_unit: 1             # lot size read from the instrument master (65 today)
anchor_mode: nearest         # floor | nearest
full_band_steps: 1           # 1 = anchor and ±100 are core condors; 0 = anchor only
half_mode: buy               # buy = debit spread (confirmed) | sell = credit half (comparison only)
debit_shift: 0               # 0 = same strikes reversed; 200 = bought at the level (§3.4)
max_put_spreads: 10
max_call_spreads: 10
fill_gaps: true
take_profit: off
stop_loss: off
entry_cutoff_days: 0         # optional: no new units in the last N trading days
margin_budget_inr: <set>
```

### 3.6 Risk envelope

Each new level adds only its debit to the worst case, not 200 pts as on the ladder. The core's loss is also partly absorbed: by the time all core condors are fully lost, the first spread is already paying.

Worst case at expiry = (core loss net of the first spread's payoff) + every debit paid − core credits.
- The core loss net of the first spread is at most 300 pts gross with two core condors, and 500 with three.

With 10/10 caps and the same illustrative prices:

| Scenario | Worst case |
|---|---|
| Spreads filled on one side only (two core condors) | ≈ 690 pts (₹44,850) |
| Spreads filled on both sides, all expire worthless (three core condors) | ≈ 1,410 pts (₹91,650) |

The second case needs a month that ranges more than ~2,200 pts and then settles 400–500 pts from the anchor. That is rare.

---

## 4. Expected behaviour by month type

This is illustrative: expiry P&L in points per 1-lot set, from simulated paths with anchor 23,400. It assumes ~130 pts credit per full condor and ~65 per spread (debit or credit). Replace these with your backtest's real averages. The pattern matters more than the numbers.

| Path | Ladder, down-only (today) | Ladder, two-way | HIC (debit spreads) | HIC (credit halves, comparison) |
|---|---|---|---|---|
| Quiet: 23,250–23,550, settles 23,450 | +260 | +390 | +390 | +390 |
| 500 down, stalls | +280 | +280 | −300 | +20 |
| 500 up, stalls | −70 | +280 | −300 | +20 |
| 1,500 down, keeps going | −420 | −420 | +1,210 ² | −1,490 ² |
| 1,500 up, keeps going | −70 | −770 ¹ | +1,210 ² | −1,490 ² |
| 500 down, back to anchor | +280 | +280 | 0 | +520 |
| 500 up, back to anchor | +130 | +280 | 0 | +520 |
| 500 down, then up to 23,800 | −420 | 0 | −565 | +345 |
| 500 up, then down to 23,000 | +350 | 0 | −565 | +345 |

¹ The up cap of 10 was hit, so the levels nearest to where the rally stopped never got a condor.
² The spread cap of 10 was hit.

In short:
- **Down-only ladder:** depends on which way the month goes.
- **Two-way ladder:** makes it symmetric, but hurts in big trends.
- **HIC with debit spreads:** a trend strategy. It wins big in one-way months, is flat when the move comes back to the anchor, and loses when the move stalls or swings through the anchor.

The ladder and HIC mostly fail in different months, so running both partly offsets. The main exception is a fall that reverses up through the anchor (−420 and −565), and to a lesser degree a rally that stalls (−70 and −300). Watch those month types in the combined backtest (P1).

---

## 5. Running the two strategies in isolation

Isolated means each strategy has its own process, state, ledger, risk budget, kill switch, logs and dashboard. A bug, crash or pause in one cannot touch the other.

They can still share read-only inputs and a broker gateway. They can also share code: the level trigger is identical in both.

```
            ┌──────────── shared, read-only ──────────────┐
            │ Market data: NIFTY spot + option quotes      │
            │ Instrument master: strikes, lot size, expiry │
            └───────────┬──────────────────────┬───────────┘
                        │                      │
       ┌────────────────▼───────┐   ┌──────────▼─────────────┐
       │ Ladder v2  (process A) │   │ HIC  (process B)       │
       │ trigger → unit builder │   │ trigger → unit builder │
       │ state DB · ledger      │   │ state DB · ledger      │
       │ caps · margin budget   │   │ caps · margin budget   │
       │ kill switch · logs     │   │ kill switch · logs     │
       │ dashboard  /ladder     │   │ dashboard  /hic        │
       └────────────┬───────────┘   └──────────┬─────────────┘
                    │  tagged orders            │  tagged orders
            ┌───────▼───────────────────────────▼──────┐
            │ Broker gateway: margin pre-check, order  │
            │ tags, account-level limit, reconciliation│
            └──────────────────────────────────────────┘
```

| Component | Shared or isolated | Notes |
|---|---|---|
| Market data, instrument master | Shared, read-only | Read expiry dates and lot size from here. Don't hardcode an expiry weekday; NSE has moved expiry days before |
| Level trigger and unit builder code | Shared library | Same code, separate instances with separate configs |
| Process | Isolated | Separate service each; one crashing or pausing doesn't stop the other |
| State store | Isolated | Own DB or schema: cycle, anchor, fired levels, units, orders |
| Position ledger | Isolated | What this strategy owns, leg by leg |
| Risk | Isolated | Own caps, margin budget, kill switch |
| Dashboard | Isolated | Own route or port; reads only its own store |
| Broker gateway | Shared | Routes tagged orders, enforces the account-level margin limit, runs reconciliation |

**The one-account problem.** If both strategies trade NIFTY monthly options in the same broker account, the broker nets them together. If the ladder is short the 23,200 PE and HIC is long it, the broker shows zero. This will happen often here, because HIC buys puts at the same strikes where the ladder sells them.

That is harmless, and even saves margin, as long as two things hold:
- Each strategy keeps its own virtual ledger.
- A reconciliation job checks every few minutes that ladder ledger + HIC ledger = broker net position, strike by strike. Any mismatch pauses both strategies.

The cleaner option, if you have it, is a separate trading account per strategy. Then margin, P&L and the broker's view are separate by construction.

**Execution rules for both strategies:**

| Rule | Detail |
|---|---|
| Order tags | Every order carries strategy, cycle, level and leg in its tag or client order ID. Keep it short, since many broker APIs cap tag length. Fills are attributed by tag, never by strike |
| Fire once, even across restarts | Write the level as `FIRED` in the state store before sending any order. After a restart, reload state and match open orders by tag instead of re-firing |
| Margin pre-check | Before a unit, ask the broker's basket-margin calculator for all its legs given the current book. If it doesn't fit the strategy's budget, mark the level `MARGIN_SKIPPED` and alert. Never send part of a unit |
| Partial units | If the BUY leg fills and the SELL is rejected, retry N times. If it still fails, keep the long leg (risk is limited to its premium), mark the unit `PARTIAL` and alert. A SELL is never placed before its BUY has filled |
| Netting is expected | When a new unit's leg lands on a strike the strategy already holds the other way, it closes that position at the broker. The ledger still records both units' legs, so per-unit P&L stays correct |
| Regulation | If these run live through a broker API, check with your broker whether each strategy needs to be registered or tagged as its own algo under SEBI's retail algo framework |

---

## 6. Dashboards (one per strategy)

| Panel | Shows | Ladder v2 specifics | HIC specifics |
|---|---|---|---|
| Status bar | Cycle, expiry date, anchor, spot, mode, running/paused/killed, last data tick | Direction mode | Band, debit shift |
| Level map | Vertical strip of levels around the anchor, each marked fired / capped / margin-skipped / skipped, with fill time | Condor per level | Core condor / put spread / call spread, colour-coded |
| Units | One row per unit: legs, fill prices, credit or debit, current MTM, worst case | ✓ | ✓ |
| Net book | The legs the broker actually carries for this strategy; cancellation % | ✓ | ✓ (stacked strikes shown as 2×) |
| Expiry payoff | P&L at expiry across NIFTY settlement levels for the current book, including credits and debits; spot marker; best and worst case | ✓ | ✓ |
| Risk | Caps used, margin used vs budget, worst case in ₹ | Down x/20, up y/10, total z/20 | Put spreads x/10, call spreads y/10, total debits paid |
| P&L | Cycle-to-date realised and MTM | Split: down-side vs up-side | Split: core credits vs spread debits and payoffs |
| Events | Fills, rejects, gap fills, capped and margin-skipped levels, partial units, reconciliation results | ✓ | ✓ |
| Backtest | Equity curve, per-cycle table, month-type breakdown, average credit and debit vs assumptions | ✓ | ✓ |

The expiry payoff panel is the single most useful addition. It shows each strategy's shape live, before expiry.

If both strategies share one account, also add a small read-only Account page. It shows the broker's combined net position, total margin and reconciliation status. It sits outside both strategies and cannot send orders.

---

## 7. Validation plan

### 7.1 Backtest matrix

| Run | Strategy | Settings | Question it answers |
|---|---|---|---|
| B0 | Ladder | down, floor anchor (today's config) | Did the refactor change anything? Must match your existing backtest trade-for-trade |
| B1 | Ladder | up only | What does the up-side earn on its own? |
| B2 | Ladder | both, nearest anchor | Is the combined system better than B0? |
| B3 | Ladder | both, floor anchor | How much does anchor mode matter? |
| H1 | HIC | band 1, debit spreads, same strikes | The spec as confirmed |
| H2 | HIC | band 0, debit spreads | Only the anchor as core |
| H3 | HIC | band 1, debit spreads, shift 200 | Does closing the protection gap pay for its extra cost? |
| H4 | HIC | band 1, credit halves | Comparison: the opposite reading |
| C1 | Both | entry cutoff 3 and 5 trading days | Are late-month units worth opening? Late condors collect little credit; late spreads have little time to pay |
| P1 | Both | B0 and H1 summed per cycle | How much do the two strategies offset each other, month by month? |

### 7.2 What to measure

Per cycle and in aggregate:
- Net P&L after costs
- Worst intra-cycle MTM drawdown
- Peak units open and peak margin
- Average credit per condor and average debit per spread, compared with the §4 assumptions
- Share of units that paid off
- P&L by month type

### 7.3 Month-type labels

Give each monthly cycle one label, based on its anchor (A), high (H), low (Lo) and settlement (C):

| Label | Rule |
|---|---|
| Quiet | The range stays within ±200 of A |
| Trend | One side exceeds 1,000 and C is within 300 of that extreme |
| Out-and-back | An excursion exceeds 300 and C is within 200 of A |
| Swing-through | Both sides exceed 300 and C is 300+ beyond A on the side reached second |
| Move-and-stall | Everything else |

Tune the thresholds as needed. The point is to group real results the same way as the §4 table, so you can see whether the pattern holds on real data.

### 7.4 Costs

Include brokerage, exchange and SEBI fees, stamp duty, GST and STT. That covers STT on legs that finish in the money and settle at expiry, which matters more for HIC: in its winning months, the debit spreads finish in the money and settle. Model wider fills at the open for units created by gap fills.

### 7.5 Unit tests

- The §2.2 and §3.2 tables as exact expected outputs: levels fired, legs, netting and stacking.
- A gap from 23,450 to 23,050 fires 23,300 (core condor), then 23,200 and 23,100 (put debit spreads), in that order.
- A level never fires twice in a cycle, including after a restart.
- The `CAPPED` and `MARGIN_SKIPPED` paths.
- Partial-unit handling.
- Reconciliation catches an injected mismatch and pauses both strategies.

### 7.6 Paper trading

Run both strategies on live data with simulated fills for at least two monthly cycles before any real order.

---

## 8. Rollout and gates

| Phase | What gets built | Gate to move on |
|---|---|---|
| 0 · Foundations | `strategy_id` everywhere; per-strategy state store, ledger, margin budget, kill switch; order tags; reconciliation job; extract the level-trigger core from the current ladder | The current ladder runs on the new plumbing, and B0 matches your existing backtest trade-for-trade |
| 1 · Ladder v2 | Direction switch, per-side caps, gap-up fill, nearest-anchor option, up/down attribution, expiry payoff panel | B1–B3 reviewed; you decide whether the up-side goes on (default off) |
| 2 · HIC engine | Unit builder with band, half mode and debit shift; caps; own process and state | H1–H4 and P1 reviewed; §7.5 unit tests pass |
| 3 · HIC dashboard | Panels from §6 | A replayed backtest cycle shows identical numbers on the dashboard |
| 4 · Paper | Both strategies on live data, simulated fills, at least 2 monthly cycles | Paper vs backtest within agreed tolerance; zero reconciliation breaks |
| 5 · Live | 1 lot each; kill switches tested (manual, stale data feed, margin breach, reconciliation break) | Review after every monthly cycle before any size increase |

---

## 9. Decisions

| # | Decision | Setting in this plan |
|---|---|---|
| 1 | HIC half | Debit spread, same strikes reversed (confirmed) |
| 2 | Core-condor band | Anchor ± 1 step (both 23,300 and 23,500 are full condors) |
| 3 | Debit spread strikes | Same strikes reversed; the shifted version only as backtest H3 |
| 4 | Ladder up-side | Built, but off until B1/B2 justify it |
| 5 | Ladder caps | Down 20, up 10, total 20 |
| 6 | HIC caps | 10 put spreads, 10 call spreads |
| 7 | Anchor mode | `floor` for the down-only ladder (parity); `nearest` for the two-way ladder and HIC |
| 8 | Entry cutoff near expiry | Off, but backtest 3 and 5 trading days |
| 9 | Accounts | Separate accounts if available; otherwise one account with ledgers and reconciliation |
| 10 | Cycle start and anchor capture time | Same as today's ladder; HIC uses the identical definition |

---

## Appendix: shared trigger core and unit builder

Both strategies use the same two pieces. Only the config differs.

```python
import math

def make_anchor(spot, step=100, mode="floor"):
    if mode == "floor":
        return step * math.floor(spot / step)
    return step * math.floor(spot / step + 0.5)          # "nearest"

class LevelTrigger:
    """Fires each grid level at most once per cycle."""
    def __init__(self, anchor, step=100, direction="down", fill_gaps=True):
        self.anchor, self.step = anchor, step
        self.direction, self.fill_gaps = direction, fill_gaps
        self.low = self.high = 0           # furthest level index reached each way
        self.fired = {0}                   # k = 0 is the anchor, fired at cycle start

    def on_price(self, spot):
        x = (spot - self.anchor) / self.step
        hits = []
        if self.direction in ("down", "both") and math.ceil(x) < self.low:
            k = math.ceil(x)
            hits += range(self.low - 1, k - 1, -1) if self.fill_gaps else [k]
            self.low = k
        if self.direction in ("up", "both") and math.floor(x) > self.high:
            k = math.floor(x)
            hits += range(self.high + 1, k + 1) if self.fill_gaps else [k]
            self.high = k
        hits = [k for k in hits if k not in self.fired]
        self.fired.update(hits)            # persist to the state store BEFORE any order
        return hits                        # nearest level first

def build_unit(level, k, strategy, band=1, half_mode="buy", shift=0, s=200, w=400):
    put_side  = [("BUY", "PE", level - w), ("SELL", "PE", level - s)]
    call_side = [("BUY", "CE", level + w), ("SELL", "CE", level + s)]
    if strategy == "ladder" or abs(k) <= band:
        legs = put_side + call_side                          # full condor (credit)
    elif half_mode == "sell":
        legs = put_side if k < 0 else call_side              # credit half (comparison only)
    elif k < 0:                                              # put debit spread (default)
        legs = [("BUY", "PE", level - s + shift), ("SELL", "PE", level - w + shift)]
    else:                                                    # call debit spread
        legs = [("BUY", "CE", level + s - shift), ("SELL", "CE", level + w - shift)]
    return sorted(legs, key=lambda leg: leg[0] != "BUY")     # every BUY leg goes first
```

Caller, per strategy:

```python
for k in trigger.on_price(spot):
    level = trigger.anchor + k * trigger.step
    kind  = unit_kind(k)                    # core | put_spread | call_spread (ladder: condor)
    if cap_reached(kind, k):   mark(k, "CAPPED");         continue
    legs = build_unit(level, k, STRATEGY, BAND, HALF_MODE, DEBIT_SHIFT)
    if not margin_ok(legs):    mark(k, "MARGIN_SKIPPED"); continue
    place_in_order(legs, tag=f"{STRATEGY}-{cycle}-{k}")  # wait for BUY fills before SELLs
```
