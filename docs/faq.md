# FAQ & error reference

## Is this a data source?

No. `wmshare-qlib` is a **format bridge**: [wmshare](https://github.com/open-winmale/wmshare)
serves the data, Qlib consumes the `.bin` layout, and this package translates between
them. It never computes factors or re-derives upstream metrics.

## What exactly is Qlib-native here?

Three things, all in Qlib's own on-disk conventions:

- `features/<inst>/<field>.day.bin` — little-endian float32, first element = start
  index into the global calendar
- `calendars/day.txt` — ascending trading dates
- `instruments/<name>.txt` — `INST\tstart\tend`

`format.py` is the single source of truth for that contract, and it is Qlib-free
(pure numpy). Only `qlib_io.py` imports Qlib, and only for reading back.

## Do I need pyqlib installed?

No, not for dumping. `pyqlib` is an extra:

```bash
pip install 'wmshare-qlib[qlib]'
```

It is needed only for `verify` (Qlib's own reader) and the Alpha158 example. Without
it, `verify` prints `UNAVAILABLE (ok=None)` — explicitly not a pass — and the dumper
is unaffected.

## Why did `verify` say UNAVAILABLE instead of PASS?

Because `pyqlib` is not importable. This is deliberate: a missing optional
dependency must never look like a successful verification. Install the `qlib` extra
to get a real PASS/FAIL.

## Why does `update-history` refuse my demo store?

```
store was not built under hfq_factor ... rebuild the store with dump-history --live first
```

Incremental appends need an **immutable price basis**. The demo snapshot only
supports `qfq_const`, and qfq re-anchors at every ex-dividend event — appending
under it would silently corrupt the panel across corporate actions. So: `dump-history
--live` to build the store, then `update-history --live` to grow it.

## hfq vs qfq — which one and why?

`hfq_factor` is the **live default**. Prices stored in `open/high/low/close` are
back-adjusted values as served by wmshare, and `factor = hfq/raw` per date. hfq is
anchored at the IPO, so **past values do not change** when a new corporate action
occurs — which is exactly what makes incremental appends safe.

`qfq_const` (demo only) emits qfq prices with `factor = 1.0`. Because qfq re-anchors
at every event, fragments fetched across an ex-div date are mutually inconsistent —
fine for a pipeline proof, wrong for research.

## Why is `--max-ec` mandatory on live commands?

wmshare bills real compute, and some surfaces have very large anchors (a
full-market screener query is 204,430 EC). Requiring an explicit budget is a
deliberate circuit breaker: no live command in this adapter can spend money by
omission. The executor measures one probe symbol, then sizes subsequent batches from
the *remaining* budget with an upper bound that only grows.

## The demo only has 10 symbols and I get "symbols outside universe"

Correct and intentional. The demo snapshot covers 10 A-share codes, 120 kline bars
and 30 moneyflow bars, and stores only `pe` (not `pb/ps/pr`). It exists to prove the
pipeline offline, not to run research. Point at `--live` with credentials for a real
universe.

## Where do my demo dates come from?

The snapshot has a fixed last date. Anchors in the docs (`2026-11-16`, close
`1453.9`) are **snapshot values, not market facts**, and they move when the wmshare
SDK re-captures the snapshot. Re-derive them rather than trusting the docs:

```bash
tail -1 out/qlib_data/calendars/day.txt
```

Also note: in demo mode `--as-of` is only a label for output filenames — it does not
change the snapshot's contents.

## I passed `universe=` to `fetch_cross_section(live=True)` and it was ignored

Known limitation in 0.3.x: the live path uses `screener.query(idx=...)` and does not
apply a per-call `universe` list. Use `idx=` to scope a live cross-section. This is
scheduled to raise or warn rather than silently ignore.

## Why is a 49-day window so expensive?

Range queries (`begin`/`end`) force-pay the anchor window, not the returned rows.
Measured on wmshare 2.2.0: a 49-trading-day kline window returned 49 rows but paid
**33,688 EC** (= `156 + 67×500`). Use `--n` for the most recent bars; never use a
range query for increments.

## What happens when my EC runs out?

`QuotaError` — and the SDK's message is authoritative: platform default applications
refill weekly (Mondays), self-created additional applications get a one-time quota
with no refill. Switch to the default application's credentials, upgrade, or top up.

## Can I use this commercially / what's the license?

BSD-3-Clause, same as wmshare. See [LICENSE](../LICENSE).

## I found a bug / want a feature

See [CONTRIBUTING.md](../CONTRIBUTING.md). Note that upstream data-surface
requests (e.g. "please add a history endpoint for metric codes") belong in the
[wmshare](https://github.com/open-winmale/wmshare) repo, not here — this adapter
cannot invent a series the service does not serve.
