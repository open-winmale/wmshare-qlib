# wmshare-qlib

[![CI](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml/badge.svg)](https://github.com/open-winmale/wmshare-qlib/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wmshare-qlib.svg)](https://pypi.org/project/wmshare-qlib/)
[![Python](https://img.shields.io/pypi/pyversions/wmshare-qlib.svg)](https://pypi.org/project/wmshare-qlib/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](https://github.com/open-winmale/wmshare-qlib/blob/main/LICENSE)

**wmshare-qlib turns [wmshare](https://github.com/open-winmale/wmshare) A-share market
data into Microsoft Qlib-native format** — backtestable `.bin` panels,
`ScoreFileModel` score CSVs, and instrument universe files — so you can run Qlib's
research pipeline without writing the format bridge yourself.

The adapter is a translation layer, not a data source: it never computes factors and
never fabricates history. What upstream serves with dates becomes a `.bin` series;
what upstream only serves as a *current* value is emitted as a dated score snapshot,
never pasted across history.

## Install

```bash
pip install wmshare-qlib                 # core
pip install 'wmshare-qlib[qlib]'         # + pyqlib, for .bin read-back verification
```

Requires **Python 3.11** — the only supported interpreter (3.12+ is refused at
install time; pyqlib ships no wheels for it). From source:

```bash
git clone https://github.com/open-winmale/wmshare-qlib.git
cd wmshare-qlib && pip install -e '.[qlib]'
```

## Quick start A — zero cost, zero credentials (start here)

The adapter defaults to a **demo snapshot** (`wmshare.demo_client()`): a fixed
offline dataset, no network, no EC. Use it to confirm your install, then switch to
live.

```bash
# 1) History panel -> Qlib .bin
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --out ./out/qlib_data

# 2) Read one value back with Qlib's own reader
#    Without pyqlib this prints UNAVAILABLE (ok=None) — never a false PASS.
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-11-16:1453.9

# 3) Today's cross section -> score CSV (ScoreFileModel input)
wmshare-qlib dump-cross-section --as-of 2026-11-16 --out ./out/score

# 4) Append one daily PIT snapshot
wmshare-qlib accumulate --as-of 2026-11-16 --out ./out/cs_accum
```

> **The demo snapshot has fixed contents.** `2026-11-16` and the `1453.9` anchor are
> values from that snapshot, not market facts; they change when the wmshare SDK
> re-captures it. Re-derive the date with `tail -1 out/qlib_data/calendars/day.txt`.
> The demo covers 10 symbols, 120 kline bars and 30 moneyflow bars, stores only `pe`
> (not `pb/ps/pr`), and is single-threaded — outside those bounds it fails loudly on
> purpose. See [docs/getting-started.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/getting-started.md) and
> [docs/faq.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/faq.md).

## Quick start B — live (real data, real EC)

Live mode needs credentials from <https://open.winmale.com> (我的 → 开放平台 →
我的应用 → 查看应用信息 — copy both `client_id` and `api-key`):

```bash
export WMSHARE_CLIENT_ID=your_client_id
export WMSHARE_CLIENT_SECRET=your_api_key
# or write them to ~/.wmshare/credentials.env (the SDK reads that file)
```

Then:

```bash
# One-time bulk: builds the fragment store under hfq_factor
wmshare-qlib dump-history --live --symbols 600519 000858 \
  --n 60 --max-ec 20000 --out ./out/qlib_data

# Daily incremental after that (fetches only NEW bars, then re-dumps)
wmshare-qlib update-history --live --max-ec 20000 --out ./out/qlib_data
```

Two rules worth knowing before you type them:

- **`--max-ec` is required** on billed paths. Without it the command exits before
  spending anything. It is a budget ceiling, not a hint.
- **A demo store cannot be grown incrementally.** Demo builds under `qfq_const`;
  appends need an immutable basis, so `update-history` refuses and tells you to
  rebuild with `dump-history --live`. See [docs/faq.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/faq.md#hfq-vs-qfq--which-one-and-why).

`--dry-run` exists on `dump-history`, `update-history`, `dump-cross-section` and
`accumulate`:

```bash
wmshare-qlib dump-history --live --symbols 600519 --n 60 --dry-run --max-ec 20000
```

## What it produces

```
out/qlib_data/
├── calendars/day.txt              # ascending trading dates
├── instruments/all.txt            # INST\tstart\tend (write-universe adds named ones)
├── features/sh600519/*.day.bin    # Qlib float32 format, first element = start index
│                                  # (includes $vwap, derived as amount/volume)
├── _resume/                       # fragment store: manifest + per-symbol parquet
└── fetch_meta.json                # params, EC metering, warnings
```

`format.py` is the single source of truth for the on-disk contract and is
Qlib-free (pure numpy). Only `qlib_io.py` imports Qlib, and only to read back — in a
child process, so importing this package never reconfigures your Qlib session.

## The three-layer data model

Only the first layer is backtestable today:

1. **Real history** — kline (OHLCV+amount), moneyflow, `pe/pb/ps/pr` (10y
   window, `config.VALUATION_WINDOW`), plus the materialized `valuation.daily_basic` family
   (`pe_ttm/total_mv/circ_mv/turnover_rate/dv_ttm`) via `--daily-basic`. Dumped
   straight into Qlib `.bin`. `$vwap` is derived at dump time (`amount/volume`;
   the stored volume is already factor-adjusted, so it is consistent with the
   adjusted prices) — Alpha158 can consume it unmodified.
2. **Today only** — wmshare's ~2,750 precomputed metric codes expose a *current*
   value with **no history-series endpoint**. Emitted as `score/<date>.csv` for
   online scoring, marked `history=false`. Never pasted across past dates.
3. **Self-built PIT series** — `accumulate` appends one real snapshot per day; the
   series is available from its first captured day and grows one real day at a
   time, with `--min-days` gating promotion to a backtestable panel.

Full detail, including why layer 2 cannot be fixed client-side:
[docs/data-model.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/data-model.md).

## Commands

| Command | Purpose | Key flags |
|---|---|---|
| `dump-history` | fetch history → Qlib `.bin` | `--live --symbols --n/--years --workers --max-ec --resume --adjust --store --daily-basic` |
| `update-history` | incremental daily tails → reconcile → re-dump (live + hfq only) | `--max-ec` (required) `--tail-overlap --valuation-days` |
| `dump-cross-section` | today's score CSV | `--via screener/snapshot --idx --max-ec` |
| `accumulate` | append one daily PIT snapshot (prints availability + promotion status) | same dispatch + `--min-days` |
| `write-universe` | `instruments/<name>.txt` from index membership or a codes file; `--index` prefers PIT membership episodes when the SDK exposes `index.members_history` (one episode per membership run; dropped members keep their closed episode — survivorship-safe) | `--index/--codes-file --exclude-st --exclude-suspended` |
| `verify` | read `.bin` back with Qlib's own reader | `--qlib-dir --check inst:field:date:value` |
| `reconcile-daily-basic` | Gate B: accumulated PIT vs official daily_basic history (100% or fail) | `--accumulated --upstream --tol`, or `--from-upstream --store --max-ec` |

`--dry-run` is available on four of them: `dump-history`, `update-history`,
`dump-cross-section`, `accumulate`.

## EC cost model

**EC is wmshare's billing unit.** The platform default application has a weekly
quota refilled every Monday; a self-created additional application holds a one-time
quota that is not refilled. Demo mode spends nothing.

Batching saves **requests**, not **EC** — wmshare bills by symbol and compute. The
anchors below are our measured values (`config.EC_*`) and the single source of truth
for estimates:

| Call | Formula / anchor |
|---|---|
| `kline.bars` | `156 + 67 × served_bars` EC (the live `hfq_factor` path pulls adj + raw, so it doubles) |
| `moneyflow.bars` | `156 + 16 × n` EC |
| `valuation.hist_stats` | 712 EC per metric (3y-window fit; the default is now 10y, which bills more) |
| `kline.snapshot` batch | ≈66–100 EC/票 |
| `kline.batch_live` | ≈45 EC/票 (percent/ex-div semantics unverified — not used) |
| `screener.query` | full market × 4 light columns = **204,430 EC**; small probes do NOT predict full-universe cost |
| `valuation.daily_basic` | bills by rows scanned (not cached): 1-day floor ≈1,707 EC, full history ≈18,317 EC, incremental ≈312 EC/票/日; `update-history --daily-basic` reads are **windowed** from the resume pointer |
| `margin.query` / `margin.latest` | ≈124 EC/行 / ≈182 EC for the one-row read |
| range queries (`begin/end`) | **force-pay the anchor window, not the returned rows** — a 49-row window measured 33,688 EC (= 156 + 67×500). Use `--n` for increments |

Every fetch result carries `metering` (`requests`, `ec_cost`, `note`) written to
`fetch_meta.json` / `*.meta.json`. Estimates are estimates; the paid
`metering.ec_cost` is the only real quote, and `--max-ec` stays the binding ceiling.

Sustainable cadence: full-market screener weekly/monthly (~200k EC/query); the daily
cross-section path is `--via snapshot` over an explicit ~300-name universe
(~17–30k EC/day). Full-market daily is not viable on any current endpoint.

## Data-correctness gates

- **Direction** — every series is asserted ascending (2.x contract); a silently
  descending series would flip every rolling computation.
- **Unit invariants** — amounts in CNY, volumes in shares, ratios dimensionless;
  absurd magnitudes raise rather than flow into a backtest.
- **Duplicate/conflict** — duplicated dates with identical values are deduplicated
  with a warning; **conflicting** duplicates raise.
- **Reconciliation gate** — incremental tails must agree with stored history within
  float tolerance. An upstream revision fails loud; history is never silently
  rewritten.
- **Gate B** — accumulated PIT values are cross-checked against official
  `daily_basic` history, 100% agreement or fail.

## Qlib compatibility & low-intrusion design

The dumper targets Qlib's own on-disk layout, verified against
`microsoft/qlib`'s data collector conventions:

- `factor = hfq/raw` per date, with adjusted prices stored (live default);
  upstream volume is not adjusted, so it is divided by factor to keep
  `close × volume == amount` invariant across ex-dividend dates.
- `qlib.init()` runs only in a **child process** during read-back; the parent
  process's `qlib.config.C` is asserted untouched in the test suite.
- `pyqlib` is an optional extra; the dumper and its self-check need only numpy.

## Module map

- `config.py` — fields, units, metric-code selection, EC anchors (single source of truth)
- `normalize.py` — anti-pitfall guards (direction, code mapping, unit, cold cache); `merge_adjusted_klines` (hfq+raw → factor)
- `fetch.py` — demo/live acquisition + metering + EC-ceiling executor + bounded transient retries
- `panel.py` — integrity assertions (incl. the factor gate) + wide transform
- `dump.py` / `format.py` — Qlib-compatible `.bin` dumper and its on-disk contract (Qlib-free)
- `qlib_io.py` — **the only module that imports Qlib**; child-process read-back + score reader
- `resume.py` — fragment store (bulk + incremental tails) + manifest with convention guard and reconciliation gate
- `cross_section.py` — score CSV + PIT accumulation/promotion
- `estimate.py` — pre-flight EC estimator over the measured anchors (`--dry-run`)
- `reconcile.py` — Gate B comparison
- `cli.py` — the seven commands

## Tests

```bash
# Tier 1 (mandatory in CI, offline, no credentials, no EC)
pytest tests -q -m "not live"

# Tier 2 (optional) — adds the child-process Qlib read-back cross-check
pip install 'wmshare-qlib[qlib]'
pytest tests -q -m qlib
```

Offline tests use `wmshare.demo_client()`, so they need no credentials. Tier 1
verifies the dumped `.bin` with pure numpy (`format.read_bin_selfcheck`); Tier 2
additionally reads it back through Qlib itself. Live tests are opt-in and gated by
`WMSHARE_QLIB_LIVE=1` — see [CONTRIBUTING.md](https://github.com/open-winmale/wmshare-qlib/blob/main/CONTRIBUTING.md).

## What we deliberately do NOT do

- Fabricate history from today's snapshot (red line — see layer 2 above).
- Recompute upstream precomputed metrics client-side and present them as wmshare values.
- Per-symbol `indicator.get` loops (EC explosion + 429).
- Run a live metric-code `screener.query` without an explicit EC budget.
- Call `qlib.init()` in the caller's process (global side effect).
- Touch the wmshare main repo (separate repo, no version-bump churn).

## See also

- [Getting started](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/getting-started.md) — install to first `.bin`, demo then live.
- [Data model](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/data-model.md) — the three availability tiers and the EC model.
- [FAQ](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/faq.md) — demo boundaries, hfq vs qfq, error reference.
- [Contributing](https://github.com/open-winmale/wmshare-qlib/blob/main/CONTRIBUTING.md) — tests, lint, and the red lines.
- [Changelog](https://github.com/open-winmale/wmshare-qlib/blob/main/CHANGELOG.md)
- [wmshare](https://github.com/open-winmale/wmshare) — the data SDK this adapter translates.
- [wmshare-vnpy](https://github.com/open-winmale/wmshare-vnpy) — the same history panel
  injected into vnpy.alpha.
- [Examples](https://github.com/open-winmale/wmshare-qlib/blob/main/examples/) — `dump_to_qlib.py`, `online_score.py`,
  `alpha158_lgbm.py`, `benchmark/`.
- Design history and forward plans (engineering records, not user docs):
  [docs/internal/](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/internal/).

## Acknowledgements

Built on [wmshare](https://github.com/open-winmale/wmshare) (BSD-3-Clause).
Not affiliated with or endorsed by Microsoft; Qlib/pyqlib is used unmodified as an
optional verification dependency only.

## License

BSD-3-Clause — see [LICENSE](https://github.com/open-winmale/wmshare-qlib/blob/main/LICENSE).
