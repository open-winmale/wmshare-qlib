# wmshare-qlib

[![CI](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml/badge.svg)](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml)

**wmshare-qlib turns [wmshare](https://github.com/open-winmale/wmshare) CN/HK market
data into Microsoft Qlib-native format** — backtestable `.bin` panels,
`ScoreFileModel` score CSVs, and instrument universe files — so you can run Qlib's
research pipeline on wmshare data without writing the format bridge yourself.
Status: Phase 1 MVP, closed-loop validated; forward plan in
[docs/optimization-roadmap.md](docs/optimization-roadmap.md).

> [!IMPORTANT]
> **Data availability comes in three tiers — only the first is backtestable today.**
> 1. **Real history** — kline (OHLCV+amount), moneyflow, PE/PB/PS/PR (≤3y, server
>    cap): dumped as Qlib `.bin`, backtestable immediately. Plus the
>    **upstream `daily_basic` fields** (`pe_ttm/total_mv/circ_mv/
>    turnover_rate/dv_ttm`) via `--daily-basic` — the server-side materialized
>    table (plan: [docs/upstream-daily-basic-plan.md](docs/upstream-daily-basic-plan.md))
>    **is LIVE since 2026-11-18** (600519 verified end-to-end; sample group
>    hs300, daily cadence ramps after sample acceptance). Reads bill by rows
>    scanned (measured: filtered read ≈2,038 EC, full-history ≈18,317 EC per
>    symbol) — see the EC table. If upstream is unreachable the flag degrades
>    to an explicit skip warning and the panel is unchanged.
> 2. **Today only** — wmshare's 2,752 precomputed metric codes expose a CURRENT
>    value and have NO history-series endpoint (verified against the SDK's
>    screener/indicator surface, which accepts no date parameters). They are
>    emitted as `score/<date>.csv` for **online scoring**, marked `history=false`.
>    This adapter never pastes today's value across past dates.
> 3. **Self-built PIT series** — `accumulate` appends one real snapshot per day;
>    a series is available **from its first captured day** and grows one real day
>    at a time (the same pattern behind Qlib's PIT store and tushare-based daily
>    datasets). A configurable cold-start guard (`--min-days`) gates promotion to
>    a backtestable panel. Once upstream serves the table, `reconcile-daily-basic`
>    checks the accumulated PIT values against official history (Gate B, 100%).

## Install

```bash
pip install -e .
pip install -e '.[qlib]'     # for .bin read-back verification / examples
```

The adapter imports wmshare. No credentials are needed for demo mode; live mode uses
`wmshare.Client.from_env()` (set wmshare AppId/API Key in env).

## Quick start

```bash
# 1) History panel -> Qlib .bin (demo offline snapshot, zero EC)
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --out ./out/qlib_data

# 2) Verify .bin with Qlib's own reader (anchors: instrument:field:date:value)
#    Without pyqlib this prints UNAVAILABLE (ok=None), never a false PASS.
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-11-16:1453.9

# 3) Today's cross section -> score CSV (ScoreFileModel input)
wmshare-qlib dump-cross-section --as-of 2026-11-16 --out ./out/score

# 4) Accumulate one daily PIT snapshot (the series grows one real day at a time)
wmshare-qlib accumulate --as-of 2026-11-16 --out ./out/cs_accum

# 5) Daily incremental update (live, hfq only: fetches NEW bars only, ~1.1k EC/票/天)
wmshare-qlib update-history --live --max-ec 20000 --out ./out/qlib_data

# 6) Universe file so D.instruments("csi300") works
wmshare-qlib write-universe --qlib-dir ./out/qlib_data --name csi300 \
  --codes-file csi300_codes.txt

# 7) End-to-end Alpha158 + LGBM backtest on the dumped data
#    (needs the optional pyqlib wheel on PYTHONPATH)
PYTHONPATH=src:.qlibvendor python examples/alpha158_lgbm.py --qlib-dir ./out/qlib_data
```

Every command takes `--dry-run` for a pre-flight EC estimate (no request, no store
write). Design rationale per feature lives in [docs/HISTORY.md](docs/HISTORY.md);
the probed cost model in [docs/optimization-roadmap.md](docs/optimization-roadmap.md).

## The three-layer data model

| Layer | Data | Qlib landing | Backtestable |
|---|---|---|---|
| ① History panel | OHLCV+amount, pe(≤3y), moneyflow | `.bin` via our dumper | ✅ yes |
| ② Cross section | precomputed metric snapshot | `score/YYYY-MM-DD.csv` → `ScoreFileModel` | ❌ no (today only) |
| ③ Accumulation | daily snapshots | parquet, available from first captured day | ⏳ grows over time |

## Commands

| Command | Purpose | Key flags |
|---|---|---|
| `dump-history` | fetch history → Qlib `.bin` | `--live --symbols --n/--years --workers --max-ec --resume --adjust --store --daily-basic` |
| `update-history` | incremental daily tails → reconcile → re-dump (live + hfq only) | `--max-ec` (required) `--tail-overlap --valuation-days` |
| `dump-cross-section` | today's score CSV | `--via screener/snapshot --idx --max-ec` |
| `accumulate` | append one daily PIT snapshot (prints availability + promotion status) | same dispatch + `--min-days` |
| `write-universe` | `instruments/<name>.txt` from index membership or codes file | `--index/--codes-file --exclude-st --exclude-suspended` |
| `verify` | read `.bin` back with Qlib's own reader | `--qlib-dir --check inst:field:date:value` |
| `reconcile-daily-basic` | Gate B: accumulated PIT vs official daily_basic history (100% or fail) | `--accumulated --upstream --tol` |

## EC cost model (deterministic, probed 2026-11-17)

Batching saves **requests**, not **EC** — wmshare bills by symbol/compute. The
anchors below are the single source of truth (`config.EC_*`; full probed table in
roadmap §2.5). They are estimates — the paid `metering.ec_cost` is the only real quote.

| Call | Formula / anchor |
|---|---|
| `kline.bars` | `156 + 67 × served_bars` EC (hfq_factor live path pulls twice: adj + raw) |
| `moneyflow.bars` | `156 + 16 × n` EC |
| `valuation.hist_stats` | `712` EC per metric per 3y window (4 metrics = 2,848) |
| `kline.snapshot` batch | ≈ `66–100 EC/票` (client-side chunks of 30; N=81 measured 66.2) |
| `kline.batch_live` | ≈ `45 EC/票` (daily OHLCV; percent/ex-div semantics unverified — not used) |
| `screener.query` | full market × 4 light columns = **204,430 EC**; small-limit probes do NOT predict full-universe cost |
| `valuation.daily_basic` | bills by rows scanned (NOT cached): filtered read ≈ **2,038 EC**, full-history ≈ **18,317 EC** per symbol (measured 2026-11-18, prod) |
| range queries (`begin/end`) | force-pay `max_limit` (500 bars) — measured 33,656 EC for a ~60-bar window; never use for increments |

Every fetch result carries `metering` (`requests`, `ec_cost`, `note`) written to
`fetch_meta.json` / `*.meta.json`; demo mode has no EC meter and says so explicitly.
Live billed paths REQUIRE `--max-ec` (checked post-hoc via `client.last_ec_cost`).

Sustainable cadence: full-market screener stays weekly/monthly (~200k EC/query);
the daily cross-section path is `--via snapshot` over an explicit ~300-name universe
(~17–30k EC/day, technical composite family); full-market daily remains not viable
on any current endpoint (a server-side fix is requested in
[docs/upstream-requests.md](docs/upstream-requests.md)).

> Note on SDK pricing models: the SDK's own `estimate_cost` (wmshare 2.x) uses
> optimistic models for some surfaces (e.g. screener light columns 70–1,000 EC)
> that diverge from our MEASURED anchors (full market × 4 light columns =
> 204,430 EC). Both are estimates; the paid `metering.ec_cost` is the only real
> quote, and `--max-ec` stays the binding ceiling (see roadmap §5 probe #3).

## Data-correctness gates

Two P0 bugs once passed every milestone gate; the root cause was missing
request-response reconciliation, not any single test. The gates that now close
that class of hole:

- **Row-count reconciliation.** Under concurrency, every symbol must receive
  EXACTLY `n` kline rows (total = n × symbols).
- **Param-sensitive stubs.** Every test stub MUST consume its arguments (written
  stub discipline in `tests/test_resume.py`) — a fixed-size fixture is blind to
  parameter pollution.
- **Cross-field date alignment.** Per instrument, valuation dates must cover the
  anchor (`close`) dates ~100%; a systematic offset (the UTC-vs-CST bug that once
  flipped a demo IC from +0.05 to -0.06) fails the dump loudly.
- **Unit magnitude guard.** Values outside their declared unit's range
  (`config.unit_value_range`) never enter Qlib.
- **(date, instrument, field) uniqueness.** Identical upstream duplicates are
  deduped WITH a warning; conflicting duplicates raise loud.
- **Factor monotonicity.** `factor` must be positive and non-decreasing per
  instrument (hfq is IPO-anchored cumulative).
- **Bulk/tail reconciliation.** Tail rows overlapping stored history must agree
  within float tolerance; an upstream history revision fails loud — history is
  never silently rewritten.
- **Cold-start guard (layer ③).** `promote_to_panel` refuses below `--min-days`
  real days; a PIT series is available from its first captured day, never before.

## Qlib compatibility & low-intrusion design

Qlib is an **optional verification dependency**, not a runtime one. The pipeline
(dump / score / accumulate) runs with **zero** `import qlib`. Only `qlib_io.py`
may import it, and even then:

- **No `qlib.init()` in the caller's process.** Read-back runs in a short-lived
  child process, so a host application that already initialized Qlib against its
  own provider is never polluted.
- **Public API first.** Verification prefers `qlib.data.D.features`; the private
  `FileFeatureStorage` is a fallback whose constructor kwargs are discovered via
  `inspect.signature`, never hard-coded.
- **Graceful absence.** Without pyqlib, `verify` reports `status=unavailable`
  (`ok=None`, explicitly NOT a pass) and every other command still works.
- **Format in one place.** The `.bin` layout lives in `format.FORMAT_CONTRACT`;
  a future Qlib format change touches only that file plus `format.py`.

Verified against **pyqlib 0.9.x**. We do not patch, vendor into the wheel, or fork
Qlib.

## Module map

- `config.py` — fields, units, metric-code selection, EC anchors: single source of truth
- `normalize.py` — the anti-pitfall guards (direction, code mapping, unit, cold cache); `merge_adjusted_klines` (hfq+raw → factor)
- `fetch.py` — demo/live acquisition + metering + EC-ceiling executor + bounded transient retries
- `panel.py` — integrity assertions (incl. the factor gate) + wide transform
- `dump.py` / `format.py` — Qlib-compatible `.bin` dumper and its on-disk contract (Qlib-free)
- `qlib_io.py` — **the only module that imports Qlib**; child-process read-back + score reader
- `resume.py` — fragment store (bulk + incremental tails) + manifest with the convention guard and the reconciliation gate
- `cross_section.py` — score CSV + PIT accumulation/promotion
- `estimate.py` — pre-flight EC estimator over the measured anchors (`--dry-run`)
- `cli.py` — the six commands

## Tests

```bash
# Tier 1 (mandatory, zero Qlib/credentials/EC) — the CI default
PYTHONPATH=src python -m pytest tests -q

# Tier 2 (optional) — adds the child-process Qlib read-back cross-check
PYTHONPATH=src:.qlibvendor python -m pytest tests -q
```

Offline tests use `wmshare.demo_client()` (fixed snapshot, no credentials). Tier 1
verifies the dumped `.bin` with pure numpy (`format.read_bin_selfcheck`). Tier 2
(marked `qlib`) additionally reads it back with Qlib's own reader and asserts the
parent process config is untouched; `.qlibvendor` is a local convenience only.

## What we deliberately do NOT do

- Fake history from today's snapshot (red line).
- Recompute upstream precomputed metrics client-side from raw primitives and
  present them as wmshare values (formula fidelity is unverifiable against the
  server; the fix is requested upstream — see
  [docs/upstream-requests.md](docs/upstream-requests.md)).
- Per-symbol `indicator.get` loops (EC explosion + 429).
- Run a live metric-code `screener.query` without an explicit EC budget.
- Call `qlib.init()` in the caller's process (global side effect).
- Touch the wmshare main repo (this is a separate repo; no 6-generator/version-bump churn).

## See also

- [wmshare](https://github.com/open-winmale/wmshare) — the data SDK this adapter translates.
- [wmshare-vnpy](https://github.com/open-winmale/wmshare-vnpy) — the same history panel
  injected into vnpy.alpha (`AlphaDataset.add_feature`), extracted from this repo's
  former M5 module.

## Acknowledgements

Built on [wmshare](https://github.com/open-winmale/wmshare) (BSD-3-Clause).
Not affiliated with or endorsed by Microsoft; Qlib/pyqlib is used unmodified as an
optional verification dependency only.

## License

BSD-3-Clause (same as wmshare).
