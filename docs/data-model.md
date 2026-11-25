# The three-layer data model

Why some fields can be backtested today, some cannot, and how the gap is closed
honestly.

`wmshare-qlib` is a **thin translation layer** between the wmshare data service and
Microsoft Qlib's on-disk format. It does not compute factors, does not re-derive
metrics, and never invents history. Everything it emits is either (a) upstream
history passed through, or (b) a snapshot it captured on a real day and filed under
that real date.

That last constraint is what forces the three-layer model.

## Layer 1 — real history (backtestable today)

These upstream surfaces serve dated series, so they go straight into Qlib `.bin`
and are immediately usable in a backtest:

| Field family | Upstream surface | Depth |
|---|---|---|
| `open/high/low/close/volume/amount` | `kline.bars` | full served window (`--n`/`--years`) |
| `factor` | derived from adj+raw in the same call | same window |
| `vwap` | derived at dump time: `amount / volume` | same window |
| `pe/pb/ps/pr` | `valuation.hist_stats` | 10y default (`config.VALUATION_WINDOW`; the engine has no year cap — 600519 pe 10y = 2,450 daily points, live-probed 2026-11-20; requires wmshare ≥ 2.5.0) |
| `net_mf/elg_net/lg_net/md_net/sm_net` | `moneyflow.bars` | served window |
| `pe_ttm/total_mv/circ_mv/turnover_rate/dv_ttm` | `valuation.daily_basic` (`--daily-basic`) | server-materialized, grows one day at a time |

`vwap` is not an upstream field — Qlib's bundled cn_data ships one and Alpha158
consumes it, so the dumper derives it (`dump._append_vwap`). The stored `volume`
is already divided by the adjustment factor, which makes the quotient consistent
with the adjusted (hfq) prices and preserves the `close × volume == amount`
invariant. Suspension days (zero volume) stay NaN.

The `--daily-basic` family is the newest addition: the wmshare service materializes a
per-symbol-per-day valuation wide table on a daily cadence, so these fields have a
real, growing history rather than a computed-on-request one. When the upstream is
unreachable the flag degrades to an explicit skip warning and the rest of the panel
is unchanged.

**Use `--n`/`--years` for depth, never range (`begin/end`) queries.** A range
query is bounded by the server's anchor window, not by the rows you asked for —
the server materializes (and charges for) the whole window even when you want a
short slice of it. The n-bars path serves exactly the requested recency in one
call.

## Layer 2 — today only (NOT backtestable)

The screener/indicator domain exposes roughly 2,750 **precomputed metric
codes** (e.g. `$ROE_TTM_LAST`, `$BONUS_RATE_3Y_AVG_LAST`). They return a
*current* value, and there is **no way to ask for their past**: the SDK's
screener/indicator surface accepts no date parameters at all, so a historical
read is inexpressible in the protocol — not merely unimplemented.

**Layer 2 is a different upstream catalog from layer 1.** The valuation fields
above (`valuation.hist_stats`, `valuation.daily_basic`) are a *separate* data
domain that does serve dated series — that is exactly why they are layer 1.
The two domains do not overlap: ROE and dividend-yield metric codes simply do
not exist on the valuation surface. (One partial bridge: `$PE_TTM_LAST` /
`$PB_TTM_LAST` have same-named daily_basic fields — `pe_ttm`/`pb` — so those
two fundamentals now also grow a real daily history inside layer 1 for the
covered sample group; the remaining ~2,748 codes have no such alternative.)

This adapter therefore emits them as `score/<date>.csv`, a format Qlib's
`ScoreFileModel` (`qlib.contrib.online.online_model`) can consume for **online
scoring**, and marks them `history=false`. The only honest way to give them a
history is layer 3 below: capture one real snapshot per day.

> Honesty note on the reader: pyqlib 0.9.7 ships `ScoreFileModel` with a broken
> upstream import (`qlib.contrib.model.base` does not exist in the wheel), so the
> adapter's read-back validates the CSV layout through an equivalent local reader
> and labels that backend `local-equivalent` — never silently claiming the official
> path ran.

> This is the adapter's hardest red line: it will not paste today's value across
> past dates to manufacture a series. A fabricated history is undetectable
> downstream once it is inside a backtest.

## Layer 3 — self-built PIT series

`accumulate` appends **one real snapshot per day** to a long frame. A series becomes
available **from its first captured day** and grows one genuine day at a time — the
same pattern behind Qlib's own PIT store and any tushare-based daily dataset.

A configurable cold-start guard (`--min-days`, default 60) gates *promotion* to a
backtestable panel. The guard is a sanity minimum against reading signal into a
handful of days; it never blocks data access.

```
day 1   accumulate → 1 day   → history_days=1,  backtestable=false
day 61  accumulate → 61 days → history_days=61, backtestable=true
```

Once the upstream materialized table covers the same window,
`reconcile-daily-basic` cross-checks your accumulated values against the official
history (**Gate B**, 100% agreement or fail loud).

Margin (两融) runs the same pattern through `accumulate-margin`: one
`margin.latest` per symbol per day lands as
`out/margin_accum/<date>.parquet` — six real columns (fin_buy_amt /
fin_repay_amt / fin_balance in CNY; loan_sell_vol / loan_repay_vol /
loan_balance in shares), forward-only, never backfilled. A missed day is
healed ONLY by the explicit, budgeted `--catchup K` (a single real
`margin.query(limit=K+2)` history read instead of K separate daily snapshot
runs). Because these rows are raw upstream captures
(not derived scores), the 60-day promotion guard does not apply: the series is
usable from its first captured day, and `dump.dump_panel` consumes the
concatenated days directly.

## Who runs: metering.auth_mode

Every fetch result carries `metering.auth_mode` — which client performed the
run:

- `credentials` — the caller's own account (configured via env vars or
  `~/.wmshare/credentials.env`).
- `public_pool` — the **free trial** (wmshare >=3.3): no credentials, requests
  carry no token, and the quota is **shared by every unregistered caller of
  your source network**. Its rate/quota refusals
  (`ip_rate_limited`, `pool_unavailable`, `ip_banned`) are terminal — the
  adapter never retries them.
- `cache_hit` — bookkeeping-only metering for a fully-cached resume run (no
  client was built; the fragments already exist on disk).

## What this design protects

1. **No fabricated history.** Ever. See layer 2.
2. **No silent rewrites.** Incremental appends must reconcile with stored history
   within float tolerance; an upstream revision fails loud instead of quietly
   changing your backtest inputs.
3. **No unbounded runs.** Every live command requires an explicit `--max-ec`
   budget ceiling, and the executor stops submitting symbols once it is
   reached (re-run with a higher ceiling to resume).
4. **No global side effects.** `qlib.init()` runs only in a child process, so
   importing this package never reconfigures a Qlib session you already have.
