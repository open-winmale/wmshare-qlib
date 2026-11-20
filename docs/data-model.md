# The three-layer data model

Why some fields can be backtested, some cannot, and where the money goes.

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

## Layer 2 — today only (NOT backtestable)

wmshare exposes roughly 2,750 **precomputed metric codes**. They return a *current*
value. There is **no history-series endpoint** for them — verified against the SDK's
screener/indicator surface, which accepts no date parameters.

This adapter therefore emits them as `score/<date>.csv`, a format Qlib's
`ScoreFileModel` (`qlib.contrib.online.online_model`) can consume for **online
scoring**, and marks them `history=false`.

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

## EC costs

**EC is wmshare's billing unit** for compute and data materialization. The platform
default application has a **weekly quota that refills every Monday**; an additional
self-created application gets a one-time quota and does *not* participate in the
weekly refill. When it runs out:

```
配额已耗尽，重试无效：平台默认应用仍有每周额度（每周一补满）；
自建附加应用为一次性额度、不参与每周补充——
可到 open.winmale.com「我的应用」换用默认应用凭证，或升级会员/充值。
```

(That message comes from the SDK's `QuotaError` guidance verbatim.)

**Batching saves requests, not EC.** wmshare bills by symbol and compute, so the
anchors below are the single source of truth (`config.EC_*`):

| Call | Anchor (measured) |
|---|---|
| `kline.bars` | `156 + 67 × served_bars`; the live `hfq_factor` path pulls twice (adj + raw) |
| `moneyflow.bars` | `156 + 16 × n` |
| `valuation.hist_stats` | 712 EC per metric (3y-window fit; the default is now 10y, which bills more) |
| `kline.snapshot` batch | ≈66–100 EC/票 |
| `screener.query` | full market × 4 light columns = **204,430 EC** |
| `valuation.daily_basic` | bills by **rows scanned**: 1-day floor ≈1,707 EC, full history ≈18,317 EC, incremental ≈312 EC/票/日 |
| range queries (`begin/end`) | **force-pay the anchor window, not the returned rows** — a 49-row window paid 33,688 EC (= 156 + 67×500) |

Every number above is an *estimate* from live probes. The paid
`metering.ec_cost` — recorded per fetch into `fetch_meta.json` and `*.meta.json` —
is the only real quote. `--max-ec` remains the binding ceiling.

### Why the demo costs nothing

The demo path uses `wmshare.demo_client()`: a fixed offline snapshot with no
network and no meter. `metering.ec_cost` is `null` and the note says so explicitly.

## What this design protects

1. **No fabricated history.** Ever. See layer 2.
2. **No silent rewrites.** Incremental appends must reconcile with stored history
   within float tolerance; an upstream revision fails loud instead of quietly
   changing your backtest inputs.
3. **No accidental spend.** Every live command that can bill requires an explicit
   `--max-ec`, and the executor sizes batches from a measured probe so the ceiling
   is enforced before submission.
4. **No global side effects.** `qlib.init()` runs only in a child process, so
   importing this package never reconfigures a Qlib session you already have.
