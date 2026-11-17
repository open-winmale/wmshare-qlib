# wmshare-qlib

> **Powered by [wmshare](https://github.com/) — this repository turns wmshare's CN/HK
> data into a Qlib-native data layer.** The goal is to bring wmshare to the largest
> A-share AI-quant research community (Qlib) and earn GitHub stars for wmshare.

## Status: Phase 1 MVP (closed-loop validated, 2026-11-16)

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

### On `factor` (Qlib-required field)

wmshare returns **qfq-adjusted prices directly** (no factor column). Under our
declared `qfq_const` convention the adapter emits `factor = 1.0` explicitly for
every date, so Qlib's standard field set is complete and the declaration is
self-consistent — never silently omitted. No double-adjustment risk.

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

## EC transparency

Batching saves **requests**, not **EC** — wmshare bills by symbol/compute. Every
fetch result carries `metering` (`requests`, `ec_cost`, `note`) written to
`fetch_meta.json` / `*.meta.json`. Demo mode has no EC meter and says so explicitly.

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
- Sustainable daily cadence today: **not full-market** (~200k/day). Either run
  the cross-section weekly (~200k/week), or rebuild the score offline from the
  cached wide frame, or wait for a server-side fixed-cost board endpoint.

## Module map

- `config.py` — field lists, units, direction expectations, metric-code selection
- `normalize.py` — the five anti-pitfall guards (direction, code mapping, unit, cold cache)
- `fetch.py` — demo/live acquisition + metering
- `panel.py` — integrity assertions + wide transform
- `dump.py` — Qlib-compatible `.bin` dumper (Qlib-free)
- `format.py` — the `.bin` on-disk contract (single source of truth) + pure-numpy self-check reader
- `qlib_io.py` — **the only module that imports Qlib**; child-process read-back + score reader
- `vnpy_alpha.py` — **the only module that imports vnpy**; panel → `AlphaDataset.add_feature` injection
- `cross_section.py` — score CSV + accumulation/promotion
- `cli.py` — command entry points

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
