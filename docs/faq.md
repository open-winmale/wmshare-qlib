# FAQ & error reference

## Is this a data source?

No. `wmshare-qlib` is a **format bridge**: [wmshare](https://github.com/open-winmale/wmshare)
serves the data, Qlib consumes the `.bin` layout, and this package translates between
them. It never computes factors or re-derives upstream metrics.

## What exactly is Qlib-native here?

Three things, all in Qlib's own on-disk conventions:

- `features/<inst>/<field>.day.bin` — little-endian float32, first element = start
  index into the global calendar, payload values begin at that index
  (`values[0]` ↔ `calendar[start_index]`)
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

## Why does `update-history` refuse my store?

```
store was not built under hfq_factor ... rebuild the store with dump-history first
```

Incremental appends need an **immutable price basis**. `qfq_const` re-anchors at
every ex-dividend event — appending under it would silently corrupt the panel
across corporate actions. Stores dumped by 0.6.x in demo mode (`qfq_const`) or
before the `adjust` param existed record no usable convention, so they are
refused. Fix: `dump-history` once to rebuild the store, then `update-history` to
grow it.

## hfq vs qfq — which one and why?

`hfq_factor` is the **default**. Prices stored in `open/high/low/close` are
back-adjusted values as served by wmshare, and `factor = hfq/raw` per date. hfq is
anchored at the IPO, so **past values do not change** when a new corporate action
occurs — which is exactly what makes incremental appends safe.

`qfq_const` (explicit only, retired from auto-selection in 0.7.0) emits qfq prices
with `factor = 1.0`. Because qfq re-anchors at every event, fragments fetched
across an ex-div date are mutually inconsistent — wrong for research.

## Why is `--max-ec` mandatory?

It is a deliberate circuit breaker, not bureaucracy: **no command in this
adapter can run unbounded by omission.** Some upstream surfaces scan a whole
universe in one request, so the cost is only knowable once paid — declaring a
ceiling first is what keeps a typo'd `--symbols` from turning into an
uncontrolled run (including on the **public-pool free trial**, whose shared
quota the ceiling also protects). Per-symbol paths measure one probe symbol,
then size subsequent batches from the *remaining* budget with an upper bound
that only grows; one-shot paths check the ceiling post-hoc. If a run stops
early with a ceiling message, raise the ceiling and re-run — `--resume` picks
up where it left off.

## What is the public-pool free trial / why am I "public_pool" in fetch_meta?

wmshare >=3.3 serves unregistered callers from a **public pool**: no credentials,
no token, and a quota **shared by every unregistered caller of your source
network**, metered like any other client. `fetch_meta.json` records it as
`metering.auth_mode == "public_pool"` (vs `"credentials"` for your own account,
`"cache_hit"` for a pure-resume run), and the CLI prints a one-line `[free-trial]`
hint on stderr. Force it with `--anonymous`; forbid the fallback with
`WMSHARE_DISABLE_ANON=1`.

## I hit `ip_rate_limited` / `pool_unavailable` / `ip_banned`

All three are **terminal public-pool refusals** — retrying does not help, and the
adapter deliberately never retries them (`ip_rate_limited` is per-hour per source
network; `pool_unavailable` means the pool app is suspended; `ip_banned` needs
real credentials to bypass). Configure credentials to use your own account and
your own rate budget.

## Where did demo mode go?

Removed in 0.7.0, together with the SDK's offline `demo_client` (wmshare 3.0
removed it upstream). The public-pool free trial is the zero-credential path now:
real data instead of a canned snapshot. The default `--symbols` (a 10-code
starter universe) is kept so a bare `dump-history --max-ec ...` still targets a
bounded set.

## I passed `universe=` to `fetch_cross_section` and it was ignored

Known limitation: the path uses `screener.query(idx=...)` and does not
apply a per-call `universe` list. Use `idx=` to scope a cross-section (a warning
is emitted when `universe=` is passed).

## Why is a short range query as heavy as a long one?

Range queries (`begin`/`end`) are bounded by the server's **anchor window**, not
by the returned rows: the server materializes the whole window it anchors on,
even when you asked for a short slice of it. Use `--n` for the most recent
bars; never use a range query for increments.

## Doesn't daily valuation already provide history? Why do metric codes still have none?

Two different upstream domains. **Valuation fields** (`pe/pb/ps/pr` via
`valuation.hist_stats`, plus the `--daily-basic` family) live on a surface that
serves *dated series* — they go into the layer-1 `.bin` panel and are
backtestable today. The **~2,750 precomputed metric codes** (ROE, 3-year
average dividend yield, …) live on the screener/indicator surface, whose query
API accepts **no date parameter at all** — asking for such a code's value on a
past date is inexpressible in the protocol, so there is nothing a client could
read, today or retroactively. The two catalogs do not overlap: the valuation
surface has no ROE or dividend-yield field. One partial bridge exists:
`$PE_TTM_LAST`/`$PB_TTM_LAST` correspond to the daily_basic fields
`pe_ttm`/`pb`, so those two fundamentals also grow a real daily history inside
layer 1 for the covered sample group. For everything else the only honest path
is `accumulate` — one real snapshot per day, backtestable after the 60-day
cold-start guard.

## What happens when the quota runs out?

`QuotaError` — and the SDK's message is authoritative: on the **public-pool free
trial** the quota is shared and not refilled by retrying (configure credentials
for your own account); on credentials, platform default applications refill
weekly (Mondays) and self-created additional applications get a one-time quota
with no refill. Switch credentials, upgrade, or top up.

## Can I use this commercially / what's the license?

BSD-3-Clause, same as wmshare. See [LICENSE](../LICENSE).

## I found a bug / want a feature

See [CONTRIBUTING.md](../CONTRIBUTING.md). Note that upstream data-surface
requests (e.g. "please add a history endpoint for metric codes") belong in the
[wmshare](https://github.com/open-winmale/wmshare) repo, not here — this adapter
cannot invent a series the service does not serve.
