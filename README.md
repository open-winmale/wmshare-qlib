# wmshare-qlib

[![CI](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml/badge.svg)](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml)

> **Powered by [wmshare](https://github.com/) — this repository turns wmshare's CN/HK
> data into a Qlib-native data layer.** The goal is to bring wmshare to the largest
> A-share AI-quant research community (Qlib) and earn GitHub stars for wmshare.

## Status: Phase 1 MVP (closed-loop validated, 2026-11-16)

> 后续优化方向调研见 [docs/optimization-roadmap.md](docs/optimization-roadmap.md)
> （2026-11-17：增量模式 / 真实 factor / 截面降本 / CI，含三个待 probe 假设）。

| Layer | Data | Qlib landing | Backtestable |
|---|---|---|---|
| ① History panel | OHLCV+amount, pe(≤3y), moneyflow | `.bin` via our dumper | ✅ yes |
| ② Cross section | precomputed metric snapshot | `score/YYYY-MM-DD.csv` → `ScoreFileModel` | ❌ no (today only) |
| ③ Accumulation | daily snapshots | parquet → promote when N days | ⏳ over time |

### An honesty contract (read this first)

wmshare's **2,752 precomputed metric codes have a CURRENT value only — there is no
history-series endpoint**. Qlib's value is historical panel backtesting. We do NOT
paste today's value across dates to fake history. Instead:
- fields with real history (K-line, pe≤3y, moneyflow) → backtestable `.bin`;
- today-only metrics → score CSV for **online scoring**, explicitly marked `history=false`;
- daily snapshots accumulate and become backtestable only after enough real days.

## Install

```bash
pip install -e .
pip install -e '.[qlib]'     # for .bin read-back verification / examples
```

The adapter imports wmshare. No credentials are needed for demo mode; live mode uses
`wmshare.Client.from_env()` (set wmshare AppId/API Key in env).

## Quick start

```bash
# 1) History panel -> Qlib .bin (demo offline snapshot)
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --out ./out/qlib_data

# 2) Verify .bin with Qlib's own reader (anchors: instrument:field:date:value)
#    Without pyqlib this prints UNAVAILABLE (ok=None), never a false PASS.
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-10-30:1392.05

# 3) Today's cross section -> score CSV (ScoreFileModel input)
wmshare-qlib dump-cross-section --as-of 2026-10-30 --out ./out/score

# 4) Accumulate a daily snapshot
wmshare-qlib accumulate --as-of 2026-10-30 --out ./out/cs_accum

# 5) M2: end-to-end backtestable loop (DataHandler -> Alpha-style features -> LGBM -> IC)
#    Needs the optional pyqlib wheel on PYTHONPATH.
PYTHONPATH=src:.qlibvendor python examples/alpha158_lgbm.py --qlib-dir ./out/qlib_data
```

### M3-lite: concurrent fetch + resumable fragments + EC hard ceiling

```bash
# Live small-scale validation: 3 symbols x 60 bars, 2 workers, hard EC ceiling 5000.
# Each symbol is cached as a fragment under <out>/_resume/ the moment it finishes.
wmshare-qlib dump-history --live --symbols 600519 000858 601318 \
  --n 60 --workers 2 --max-ec 5000 --out ./out/qlib_data

# Resume after an interruption: already-cached symbols are skipped (0 re-billing),
# all fragments are merged, then dumped once with a single global calendar.
wmshare-qlib dump-history --live --symbols 600519 000858 601318 \
  --n 60 --workers 2 --max-ec 5000 --resume --out ./out/qlib_data
```

Three enforced rules (not just documentation):
- **Demo is always serial.** The demo snapshot client is not thread-safe (parallel
  calls corrupt its shared kline cursor: 60 rows → 3). `--workers` is clamped to 1
  in demo; concurrency is used only for live. Covered by a regression test.
- **EC ceiling is enforced before submission.** A 1-symbol probe measures the real
  per-symbol EC; later batches are sized `floor(remaining / upper_bound)` with an
  upper bound that only grows. Honest worst case: the single probe symbol may itself
  exceed the ceiling — its cost is unknowable before the call. Live EC is volatile
  (observed 322–9763 EC for the same 3×60 workload across cold/warm cache), which is
  exactly why a pre-set ceiling is required.
- **Fragments, not per-symbol `.bin`.** Qlib `.bin` offsets are relative to a global
  calendar, so writing one instrument at a time would break seek offsets. We cache
  long-panel fragments and merge + dump once all symbols are present. A manifest
  guards against mixing panels fetched with different `n`/`fields`/`live`/`freq`.

### M6-lite: incremental daily updates (`update-history`, live + hfq only)

```bash
# 0) one-time bulk (as above)
wmshare-qlib dump-history --live --symbols 600519 000858 --n 60 --max-ec 20000 \
  --out ./out/qlib_data

# 1) daily: fetch NEW bars only, reconcile, re-dump the full .bin (dump is free)
wmshare-qlib update-history --live --max-ec 20000 --out ./out/qlib_data
```

Design (verified against deterministic pricing, roadmap §2.5):
- **Per symbol**: dual kline tail `bars(n=k, adj="hfq")` + `bars(n=k, adj=None)`
  — k = gap days + `--tail-overlap` (default 5) — plus a moneyflow tail.
  Cost ≈ `2×(156+67k)` EC; a daily run (k≈6) ≈ **1.1k EC/票/天** vs the ~7.4k
  full bundle → **~6.5×**. The dual pull is REQUIRED: `kline.batch_live`
  serves the raw live price with UNVERIFIED ex-div semantics (its `percent`
  basis on ex-div days cannot be probed on demand), while `bars(n=k)` returns
  hfq AND raw AND the exact factor in one merge. batch_live stays a documented
  follow-up, not a silent shortcut.
- **Reconciliation gate**: tail rows overlapping stored history must agree
  within float tolerance (hfq/raw prices, moneyflow, historical valuation are
  immutable upstream). Any disagreement — an upstream history revision — fails
  loud in `resume.load_panel_with_tail`; history is never silently rewritten.
- **Valuation cadence** (`--valuation-days`, default 7): the 3y valuation
  series refreshes only when due (it dominates EC: ~712 EC per metric);
  size your universe to your budget — at CSI300 scale weekly valuation is
  ~850k EC/week. The cadence advances only when the refresh actually
  succeeded (a failed refresh retries next run).
- **Stale guard** (`--tail-max-n`, default 30): a gap needing more bars than
  this is skipped with a warning (tail no longer cheaper than a full re-pull).
- **hfq_factor only, live only**: appends need the immutable hfq basis
  (qfq_const re-anchors at every ex-div event, so appended bars would not
  line up with stored history); demo snapshot has neither adj= nor windows.
  Stores built before the `adjust` param (or under another convention) are
  refused — conventions are never mixed in one dump.
- **Full re-dump every time**: the .bin rebuild is local IO (zero EC); the
  single-global-calendar invariant stays trivial because only the FETCH is
  incremental.

### M7-lite: sustainable daily cross-section (`--via snapshot`, P0-3)

The server does not serve screener index subsets (`index.hs300` -> "not
loadable", probe 2026-11-17) and `screener.query` bills the SCANNED universe —
so the scan size cannot be narrowed client-side on that path. The
`kline.snapshot` BATCH accepts an explicit symbol list and bills per ticket:
**it is the only path where the operator controls the scanned universe**.

```bash
# daily cross-section over an explicit ~300-name universe (~17-30k EC/day)
wmshare-qlib dump-cross-section --live --via snapshot \
  --symbols 600519 000858 601318 ... --max-ec 50000 --out ./out/score

# accumulate it (same dispatch)
wmshare-qlib accumulate --live --via snapshot --symbols ... --max-ec 50000
```

Honesty notes:
- **Two score families, never blurred**: screener = fundamentals composite
  (`$PE_TTM_LAST` etc., full-market ~204k EC — monthly cadence); snapshot =
  TECHNICAL COMPOSITE (`config.SNAPSHOT_METRICS`: chg_20d / roi_1y / ma60_up
  higher_better, mv lower_better — declared directions, z-score composite,
  per-ticket ~100 EC at N=3). The metering note labels the family so the
  score meta never mixes them.
- **Universe hygiene gates**: universe intersected with `stocks.list`;
  ST / suspended rows DROPPED with warnings (never scored); rows missing any
  metric value DROPPED (anti-pitfall #5 — never filled 0).
- **Guards**: `>= 3` symbols (fewer would silently fall back to per-ticket
  evaluation at ~10,250 EC each); `--max-ec` REQUIRED, checked post-hoc via
  `client.last_ec_cost`.
- **Cost anchors**: ~100 EC/票 measured at N=3 (docstring claims ~58/票 at
  large batches — the N=300 unit price is a pending probe); a stray
  out-of-index ticket falls back per-ticket (~10k EC) which the ceiling
  catches.
- Index membership files are operator-supplied for now (a real CSI300
  membership source is the P1-1 universe-file item, not silently guessed).

### P1-1: universe files (`write-universe`, P1-1)

`D.instruments("<name>")` needs `instruments/<name>.txt` — Qlib convention
`<INSTRUMENT>\t<start>\t<end>` per line. The adapter now writes them:

```bash
# from a real index membership (live; indices.members — cost NOT yet probed)
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --index index.hs300 --live

# from an operator codes file (offline; bare codes map via stocks.list,
# qlib-style SH600519 passes through — never digit-range guesses)
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --codes-file csi300_codes.txt

# optional hygiene pre-pass: drop ST/suspended BEFORE fetching history
# (~100 EC/票 snapshot batch, --max-ec required)
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --codes-file csi300_codes.txt --live --exclude-st --exclude-suspended \
  --max-ec 50000
```

Honesty notes:
- **Ranges are what the dump CONTAINS**: `[start, end]` derive from each
  instrument's .bin header vs the global calendar — a suspended or
  late-listed member never pretends to have traded days it does not have.
  Members without dumped data are dropped with a warning, never fabricated.
- **Demo degradation**: `--index` is live-only (the demo client has no
  `indices` namespace, verified offline); codes-file + demo `stocks.list`
  mapping works offline. `indices.members` cost is a pending probe
  (roadmap §5.6).
- The example scripts' hand-written `backtest_universe.txt` hack is now
  superseded: `D.instruments("csi300")` works directly on a dumped dir.

### P1-2: deep history (`--years N`, opt-in — read this first)

```bash
# ALWAYS dry-run first: deep history is EXPENSIVE and per-bar pricing beyond
# 500 bars is an extrapolation (probe pending, roadmap §5)
wmshare-qlib dump-history --dry-run --live --years 5 --max-ec ...
# then fire
wmshare-qlib dump-history --live --years 5 --symbols 600519 000858 --max-ec ...
```

- `--years N` maps to `n = 250×N` bars (one call; the client's served-bar cap
  is raised automatically, SDK hard cap 10,000 bars ≈ full A-share history
  back to 1991). Mutually exclusive with `--n`. Range queries (`begin/end`)
  are NOT used — falsified probe (they force-pay `max_limit` bars regardless
  of window; roadmap §2.5).
- **Cost honesty**: `n=1250` (5y) ≈ **190k EC/票 estimated** with the
  dual-pull hfq_factor path; per-bar pricing beyond the 500-bar probe point is
  a LINEAR EXTRAPOLATION — the dry-run labels it `EXTRAPOLATED` and you should
  treat it as a lower bound until re-probed. csi300-scale deep history
  (~24M+ EC) is explicitly NOT recommended; deep history is for small
  one-time builds.
- **Valuation stays 3y** (server-side hard cap — no backfill, ever; the
  honest extension is daily accumulation).

### On `factor` (Qlib-required field)

Two declared conventions (`config.ADJUSTMENT_MODES`, resolved by
`config.adjustment_mode(live, requested)`):

- **`hfq_factor` (live default, P0-2')**: prices stored in
  open/high/low/close are **hfq** (back-adjusted, IPO-anchored) values served
  by wmshare; the adapter pulls kline **twice** (`adj="hfq"` + raw) and emits
  the real `factor = hfq/raw`. Qlib convention verified against upstream
  `scripts/data_collector/yahoo/collector.py` (`YahooNormalize1d.adjusted_price`):
  stored close IS the adjusted price, `raw = close / factor`, and
  `volume_adj = raw / factor` — which the adapter applies because the probe
  showed **upstream volume is NOT adjusted** (raw volume == hfq volume,
  2026-11-17, 600519: factor 8.6463, dates align 1:1). hfq past values never
  change when a new corporate action occurs → fragments stay valid, daily
  bars append cleanly (the qfq re-anchoring problem is gone). Live kline EC
  doubles (deterministic: `156 + 67·n` per call, see the roadmap's §2.5).
- **`qfq_const` (demo, forced)**: the demo snapshot supports neither `adj=`
  nor date ranges (upstream UsageError), so prices are qfq and `factor = 1.0`
  is emitted explicitly on every date. Caveat stays: qfq re-anchors at every
  corporate action, so demo fragments fetched across an ex-div event are
  mutually inconsistent — live hfq_factor is the fix, demo is pipeline-only.

Both paths are gated: `panel.assert_panel_integrity` fails loud if `factor`
is non-positive or DECREASES along dates (hfq is cumulative; a decrease means
mixed-convention fragments or upstream inconsistency). The resume manifest
refuses stores that predate the `adjust` param key — never mix conventions
in one dump.

### M2 backtest result (demo, 10 names × 120 days — pipeline proof, not alpha claim)

DataHandler loads `(1000, 7)` = 6 features (incl. custom `$pe`) + 1 label, zero
phantom/missing columns. LightGBM fits 710 rows / predicts 300. Cross-sectional
rank IC: mean **+0.0500**, std 0.3608, IR 0.1387 over 28 days (2026-11-17,
after the timezone fix below). An earlier run reported IC **-0.0636 over 12
days**: valuation timestamps were converted with UTC instead of CST (UTC+8),
shifting `$pe` one day back and silently NaN-joining feature 5 — fewer valid
rows AND a different IC. The fix is locked by a cross-field date-alignment
assertion (`panel.assert_dates_aligned_across_fields`, wired into
`assert_panel_integrity`) plus a demo-data regression test. The numbers are
still demo-quality; the point is the research loop closes on CORRECT data and
emits an explainable metric, not that the demo factors are profitable.

### M5: vnpy.alpha injection channel (zero EC, demo-validated)

The same history panel can be injected into **vnpy.alpha** (`AlphaDataset.add_feature`)
next to the Qlib path — one data layer, two ecosystems:

```bash
PYTHONPATH=src python examples/vnpy_alpha_demo.py
```

Contract verified against vnpy master source (`vnpy/alpha/dataset/template.py`):
- a RESULT feature is a `pl.DataFrame[datetime, vt_symbol, data]`; the `data`
  column is renamed to the feature name and left-joined on
  `["datetime", "vt_symbol"]` during `prepare_data()`;
- `vt_symbol` mapping: SH→`.SSE`, SZ→`.SZSE`, BJ→`.BSE` (vnpy `Exchange` values);
- feature names must NOT collide with base-frame columns (`open/high/low/close/
  volume/amount`) — a duplicate-name join corrupts `prepare_data`'s column
  selection, so the adapter rejects it up front;
- the M5 gate is a two-way reconciliation: every learn value must match the
  source panel (zero mismatch) and every learn row must exist in the panel
  (zero phantom). Label injected as a result feature keeps vnpy's expression
  label empty; `show_feature_performance()` needs an expression label and is
  out of scope for the data gate (documented, not hidden).

`vnpy_alpha.py` is the only module that imports vnpy (same single-point
discipline as `qlib_io.py`); vnpy remains an optional dependency.

## Data-correctness gates (what the 2026-11-17 review added)

Two P0 bugs once passed every milestone gate; their root cause was missing
request-response reconciliation, not any single test. Four gates now close that
class of hole:

- **Row-count reconciliation.** Under concurrency, every symbol must receive
  EXACTLY `n` kline rows (total = n × symbols). The P0-1 bug — a loop variable
  shadowing the `n` parameter, shrinking every concurrent fetch to
  n=workers rows — is locked by `test_live_concurrent_delivers_n_rows_per_symbol`.
- **Param-sensitive stubs.** Every test stub MUST consume its arguments (return
  exactly `n` rows); a fixed-size fixture is blind to parameter pollution and
  let P0-1 through. This is a written stub discipline (see
  `tests/test_resume.py` module docstring), not a convention.
- **Cross-field date alignment.** Per instrument, valuation dates must cover the
  anchor (`close`) dates ~100%; a systematic offset (the P0-2 timezone bug)
  drives coverage to ~0 and fails the dump loudly.
- **Unit magnitude guard.** Values outside their declared unit's range
  (`config.unit_value_range`) fail `assert_panel_integrity` before any dump.
- **(date, instrument, field) uniqueness.** Upstream demo snapshots return
  identical-value duplicate rows (observed in kline/valuation tails,
  2026-11-17): identical duplicates are deduped at the fetch layer WITH a
  warning, conflicting duplicates raise loud. The previous behaviour —
  `pivot_table(aggfunc="last")` — swallowed this silently for months.
- **Factor monotonicity (P0-2').** `factor` must be positive and
  non-decreasing along dates per instrument (hfq is IPO-anchored cumulative;
  qfq_const's constant 1.0 passes trivially). Backstop for the resume
  manifest's convention guard.
- **Bulk/tail reconciliation (P0-1).** Every tail row overlapping a stored
  bulk row must agree within float tolerance; a disagreement (upstream
  history revision, or a convention mix that slipped past the manifest
  guard) raises before any dump. History is never silently rewritten.

## Ops: retries & pre-flight budgets (P1-3)

- **Transient retry, narrow by design**: per-symbol HISTORY calls only
  (kline adj/raw, moneyflow, valuation metrics) get ONE bounded retry
  (`fetch.RETRY_ATTEMPTS = 2`) on `TransportError` / `ServerError` /
  rate-limit-429 — honouring the SDK's `retry_after`. NEVER retried:
  `QuotaError` (exhausted quota; it subclasses LimitError, so the exclusion is
  explicit), `AuthError`/`UsageError`/`ApiError`/`DataError` (permanent), and
  the big one-shot queries (`screener.query`, `kline.snapshot`) — a timeout
  mid-materialization must not double-bill; `--max-ec` is their only guard.
  EC metering fires after EVERY attempt (a failed attempt may still have
  billed), and retries surface as warnings, never silently.
- **`--dry-run` pre-flight quotes** on `dump-history` / `update-history` /
  `dump-cross-section` / `accumulate`: a pure-local plan (no store write, no
  request) over the measured deterministic anchors (`config.EC_*`,
  roadmap §2.5). Estimates flag when they exceed `--max-ec`. The paid
  `metering.ec_cost` remains the only real quote.

## EC transparency

Batching saves **requests**, not **EC** — wmshare bills by symbol/compute. Every
fetch result carries `metering` (`requests`, `ec_cost`, `note`) written to
`fetch_meta.json` / `*.meta.json`. Demo mode has no EC meter and says so explicitly.

**Deterministic pricing (probed 2026-11-17, 11 calls, ~54.6k EC — full table in
`docs/optimization-roadmap.md` §2.5):** `kline = 156 + 67·served_bars` EC,
`moneyflow = 156 + 16·n`, `batch_live ≈ 45 EC/票`, `kline.snapshot ≈ 100 EC/票`
(N=3). Two corrections to the earlier anchors below: EC volatility came from
DIFFERENT served bar counts, not cache states; and "kline itself is cheap" was
wrong — at n=60 a single kline call (4,176 EC) exceeds all four valuation calls
combined. Range queries (`begin/end`) force-pay `max_limit` (default 500) bars —
never use them for increments (measured 33,656 EC for a ~60-bar window).
The bundle re-probe to refresh the per-symbol anchors below is still pending.

Measured cost anchors (2026-11-17, live, calibrated over four probes):
- History fetch: ~**4,275 EC per symbol** (4 × valuation 3y ≈ 712 EC each dominate;
  kline itself is cheap). EC is volatile across cache states (earlier observed
  322–9,763 EC for the same 3×60 workload) — hence the pre-set ceiling.
- **Cross-section billing scales with scanned/returned universe** (the SDK's
  "not per-symbol" note refers to request count, NOT EC):
  full market (5,617 rows) × 4 light columns = **204,430 EC** (~9/row/column);
  the same columns at `limit=10` cost **349 EC** — **small-limit probes DO NOT
  predict full-universe cost**; computed columns (`$PCT_*_10Y`) are ~110/row
  (621,399 EC full universe). `--live` therefore REQUIRES `--max-ec` (checked
  post-hoc via `client.last_ec_cost` — the gateway does not put EC in df attrs).
- `screener.query --idx` subsetting is NOT currently served (server runtime
  index map only holds the daily full-market index; `index.hs300` returned
  "not loadable"). `stocks.list` mapping costs **66 EC** and covers 5,693 names.
- Sustainable daily cadence (updated 2026-11-17, P0-3): full-market screener
  stays **weekly or monthly** (~200k EC/query). The DAILY path is
  `--via snapshot` over an explicit ~300-name universe (~17-30k EC/day,
  technical composite family — see M7-lite above); full-market daily remains
  not viable on any current endpoint.

## Module map

- `config.py` — field lists, units, direction expectations, metric-code selection
- `normalize.py` — the five anti-pitfall guards (direction, code mapping, unit, cold cache); `merge_adjusted_klines` (hfq+raw → factor, P0-2')
- `fetch.py` — demo/live acquisition + metering; bulk (`fetch_history_panel`) and incremental tail (`fetch_history_tail`, P0-1) share the EC-ceiling executor
- `panel.py` — integrity assertions (incl. the factor gate) + wide transform
- `dump.py` — Qlib-compatible `.bin` dumper (Qlib-free)
- `format.py` — the `.bin` on-disk contract (single source of truth) + pure-numpy self-check reader
- `qlib_io.py` — **the only module that imports Qlib**; child-process read-back + score reader
- `vnpy_alpha.py` — **the only module that imports vnpy**; panel → `AlphaDataset.add_feature` injection
- `resume.py` — fragment store (bulk + P0-1 tails) + manifest with the convention guard and the bulk/tail reconciliation gate
- `cross_section.py` — score CSV + accumulation/promotion
- `estimate.py` — pre-flight EC estimator over the measured anchors (P1-3 `--dry-run`)
- `cli.py` — command entry points (`dump-history` / `update-history` / `dump-cross-section` / `accumulate` / `verify` / `write-universe`)

## Qlib compatibility & low-intrusion design

Qlib is an **optional verification dependency**, not a runtime one. The pipeline
(dump / score / accumulate) runs with **zero** `import qlib`. Only `qlib_io.py`
may import it, and even then:

- **No `qlib.init()` in the caller's process.** Read-back runs in a short-lived
  child process, so a host application that already initialized Qlib against its
  own provider is never polluted (`qlib.config.C` stays unchanged).
- **Public API first.** Verification prefers `qlib.data.D.features`; the private
  `FileFeatureStorage` class is a fallback whose constructor kwargs are discovered
  via `inspect.signature`, never hard-coded.
- **Graceful absence.** Without pyqlib, `verify` reports `status=unavailable`
  (`ok=None`, explicitly NOT a pass) and every other command still works. The
  `ScoreFileModel` reader falls back to identical local logic and says so.
- **Format in one place.** The `.bin` layout lives in `format.FORMAT_CONTRACT`;
  a future Qlib format change touches only that file plus `format.py`.

Verified against **pyqlib 0.9.x**. We do not patch, vendor into the wheel, or fork
Qlib. Upgrading Qlib only ever requires re-running the optional Tier-2 cross-check.

## Tests

```bash
# Tier 1 (mandatory, zero Qlib) — the default, CI-friendly path
PYTHONPATH=src python -m pytest tests -q

# Tier 2 (optional) — adds the child-process Qlib read-back cross-check
PYTHONPATH=src:.qlibvendor python -m pytest tests -q
```

Offline tests use `wmshare.demo_client()` (fixed snapshot, no credentials). Tier 1
verifies the dumped `.bin` with pure numpy (`format.read_bin_selfcheck`) and passes
without pyqlib. Tier 2 (marked `qlib`) additionally reads it back with Qlib's own
reader and asserts the parent process config is untouched; `.qlibvendor` is only a
**local convenience** for this optional cross-check, not a project requirement.

## What we deliberately do NOT do

- Fake history from today's snapshot (red line).
- Per-symbol `indicator.get` loops (EC explosion + 429).
- Run a live metric-code `screener.query` without an explicit EC budget
  (measured 113,948 EC per query — requires `--max-ec`, fails loud otherwise).
- Call `qlib.init()` in the caller's process (global side effect → P0 in the roadmap).
- Touch the wmshare main repo (this is a separate repo; no 6-generator/version-bump churn).

## License

BSD-3-Clause (same as wmshare).
