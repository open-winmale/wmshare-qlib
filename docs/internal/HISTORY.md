# Design history & milestone log

> Archived from the README (2026-11-17 restructure): the README now stays
> user-facing (positioning → install → quickstart → boundaries → cost → gates);
> the engineering narrative below is kept here verbatim, oldest milestone first.
> For the forward-looking plan see [optimization-roadmap.md](optimization-roadmap.md).

## M3-lite: concurrent fetch + resumable fragments + EC hard ceiling

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
  exceed the ceiling — its cost is unknowable before the call. (EC volatility was
  later explained deterministically: it comes from DIFFERENT served bar counts,
  not cache states — see the EC cost model in the README and roadmap §2.5.)
- **Fragments, not per-symbol `.bin`.** Qlib `.bin` offsets are relative to a global
  calendar, so writing one instrument at a time would break seek offsets. We cache
  long-panel fragments and merge + dump once all symbols are present. A manifest
  guards against mixing panels fetched with different `n`/`fields`/`live`/`freq`.

## M6-lite: incremental daily updates (`update-history`, live + hfq only)

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
  Cost ≈ `2×(156+67k)` EC; a daily run (k≈6) ≈ **1.1k EC/票/天** vs the full
  bundle → **~6.5×**. The dual pull is REQUIRED: `kline.batch_live`
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

## M7-lite: sustainable daily cross-section (`--via snapshot`, P0-3)

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
  per-ticket ~66-100 EC). The metering note labels the family so the
  score meta never mixes them.
- **Universe hygiene gates**: universe intersected with `stocks.list`;
  ST / suspended rows DROPPED with warnings (never scored); rows missing any
  metric value DROPPED (anti-pitfall #5 — never filled 0).
- **Guards**: `>= 3` symbols (fewer would silently fall back to per-ticket
  evaluation at ~10,250 EC each); `--max-ec` REQUIRED, checked post-hoc via
  `client.last_ec_cost`.
- **Cost anchors**: ~100 EC/票 measured at N=3, **66.2 EC/票 at N=81**
  (probe 2026-11-17, docstring claims ~58 at scale); a stray
  out-of-index ticket falls back per-ticket (~10k EC) which the ceiling
  catches. ~81-ticket batches fail server-side → client-side chunks of 30
  (`config.SNAPSHOT_BATCH_CHUNK`).

## P1-1: universe files (`write-universe`)

`D.instruments("<name>")` needs `instruments/<name>.txt` — Qlib convention
`<INSTRUMENT>\t<start>\t<end>` per line. The adapter writes them:

```bash
# from a real index membership (live; indices.members — cost probed: 12,754 EC
# full pull, index.hs300 is a 100-name universe upstream)
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --index index.hs300 --live

# from an operator codes file (offline; bare codes map via stocks.list,
# qlib-style SH600519 passes through — never digit-range guesses)
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --codes-file csi300_codes.txt

# optional hygiene pre-pass: drop ST/suspended BEFORE fetching history
# (~66-100 EC/票 snapshot batch, --max-ec required)
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
  mapping works offline.
- The example scripts' hand-written `backtest_universe.txt` hack is
  superseded: `D.instruments("csi300")` works directly on a dumped dir.

## P1-2: deep history (`--years N`, opt-in — read this first)

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

## On `factor` (Qlib-required field)

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

## M2 backtest result (demo, 10 names × 120 days — pipeline proof, not alpha claim)

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
(The honest replacement anchor is the roadmap's P2-2 benchmark card:
csi300 × 3y Alpha158-lite with published EC cost.)

## M5: vnpy.alpha injection channel — EXTRACTED (2026-11-17 restructure)

The vnpy.alpha injection adapter (panel → `AlphaDataset.add_feature`, zero EC,
two-way roundtrip gate) originally lived here as module `vnpy_alpha.py` /
`tests/test_vnpy_alpha.py` / `examples/vnpy_alpha_demo.py`. Per single-downstream
adapter convention (one repo = one downstream ecosystem; see akshare→AKTools,
tushare→qlib adapters), it is extracted to its own repository
**`open-winmale/wmshare-vnpy`** (public repo creation pending — this note is
the placeholder). Extraction is lossless: the module's only dependency here was
`panel.to_wide` (7 lines) plus the long-panel `[date, instrument, field, value]`
schema; it never imported wmshare itself, and vnpy/polars were never declared
in this repo's dependencies.

The verified contract (kept for reference, now owned by the new repo):
- a RESULT feature is a `pl.DataFrame[datetime, vt_symbol, data]`; the `data`
  column is renamed to the feature name and left-joined on
  `["datetime", "vt_symbol"]` during `prepare_data()`;
- `vt_symbol` mapping: SH→`.SSE`, SZ→`.SZSE`, BJ→`.BSE` (vnpy `Exchange` values);
- feature names must NOT collide with base-frame columns (`open/high/low/close/
  volume/amount`) — a duplicate-name join corrupts `prepare_data`'s column
  selection, so the adapter rejects it up front;
- the gate is a two-way reconciliation: every learn value must match the
  source panel (zero mismatch) and every learn row must exist in the panel
  (zero phantom). Label injected as a result feature keeps vnpy's expression
  label empty; `show_feature_performance()` needs an expression label and is
  out of scope for the data gate (documented, not hidden).
