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

## Quick start A — free trial, no credentials (start here)

Since wmshare 3.x there is **one data mode (live)** — and it works with **no
credentials at all**: the SDK's `from_env()` returns a **public-pool free-trial
client** (zero network at construction; requests carry no token). Configure
nothing and start pulling real data:

```bash
# 0) Preview the run scope — fires nothing
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --dry-run

# 1) History panel -> Qlib .bin
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --max-ec 30000 --out ./out/qlib_data

# 2) Read one value back with Qlib's own reader
#    Without pyqlib this prints UNAVAILABLE (ok=None) — never a false PASS.
wmshare-qlib verify --qlib-dir ./out/qlib_data --check SH600519:close:<date>:<close>

# 3) Today's cross section -> score CSV (ScoreFileModel input)
wmshare-qlib dump-cross-section --via snapshot --symbols 600519 000858 601318 \
  --max-ec 1000 --out ./out/score
```

Two things to know about the free trial:

- **It is a shared pool** — every unregistered caller of your network draws
  from the same quota, so a quota/rate refusal is terminal (retrying does not
  help). Real work wants real credentials (Quick start B).
- **`--anonymous` forces this mode** even when credentials exist; set
  `WMSHARE_DISABLE_ANON=1` to forbid the fallback entirely (missing credentials
  then fail loud again).

> The offline demo snapshot (`wmshare.demo_client()`) was removed by the SDK in
> wmshare 3.0 and retired here in 0.7.0 — the free trial replaced it as the
> zero-credential path.

## Quick start B — production (real data, your own account)

Credentials from <https://open.winmale.com> (我的 → 开放平台 → 我的应用 → 查看应用信息
— copy both `client_id` and `api-key`):

```bash
export WMSHARE_CLIENT_ID=your_client_id
export WMSHARE_CLIENT_SECRET=your_api_key
# or write them to ~/.wmshare/credentials.env (the SDK reads that file)
```

Then the same commands run against your own account:

```bash
# One-time bulk: builds the fragment store under hfq_factor
wmshare-qlib dump-history --symbols 600519 000858 \
  --n 60 --max-ec 30000 --out ./out/qlib_data

# Daily incremental after that (fetches only NEW bars, then re-dumps)
wmshare-qlib update-history --max-ec 20000 --out ./out/qlib_data
```

Two rules worth knowing before you type them:

- **`--max-ec` is required** on every live path. It is a budget ceiling, not a
  hint: the run stops submitting work once the ceiling is reached. If a run
  stops early with a ceiling message, raise the ceiling — `--resume` picks up
  where it left off.
- **Incremental appends need the immutable hfq basis.** Stores dumped under an
  older convention (or predating the `adjust` param) are refused by
  `update-history` — rebuild once with `dump-history`. See
  [docs/faq.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/faq.md#hfq-vs-qfq--which-one-and-why).

Want this to run itself? `tools/daily_update.sh` + a launchd template do the
daily update, PIT snapshot and an offline freshness verdict on a schedule —
see [docs/getting-started.md](docs/getting-started.md#8-optional-local-automation-launchd-macos).

`--dry-run` exists on `dump-history`, `update-history`, `dump-cross-section` and
`accumulate` — it prints the run scope and fires nothing:

```bash
wmshare-qlib dump-history --symbols 600519 --n 60 --dry-run --max-ec 30000
```

## What it produces

```
out/qlib_data/
├── calendars/day.txt              # ascending trading dates
├── instruments/all.txt            # INST\tstart\tend (write-universe adds named ones)
├── features/sh600519/*.day.bin    # Qlib float32: [start_index, values...] where
│                                  # values[0] is calendar[start_index]
│                                  # (includes $vwap, derived as amount/volume)
├── _resume/                       # fragment store: manifest + per-symbol parquet
└── fetch_meta.json                # params, metering, warnings
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
2. **Today only** — the screener/indicator domain's ~2,750 precomputed metric
   codes expose a *current* value, and their querying surface accepts **no date
   parameter at all** — requesting their past values is inexpressible upstream.
   This is a different upstream catalog from the valuation fields of layer 1
   (which do carry dated series). Emitted as `score/<date>.csv` for online
   scoring, marked `history=false`. Never pasted across past dates.
3. **Self-built PIT series** — `accumulate` appends one real snapshot per day; the
   series is available from its first captured day and grows one real day at a
   time, with `--min-days` gating promotion to a backtestable panel.

Full detail, including why layer 2 cannot be fixed client-side:
[docs/data-model.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/data-model.md).

## Commands

| Command | Purpose | Key flags |
|---|---|---|
| `dump-history` | fetch history → Qlib `.bin` | `--symbols --n/--years --workers --max-ec --resume --adjust --store --daily-basic --anonymous` |
| `update-history` | incremental daily tails → reconcile → re-dump (hfq only) | `--max-ec` (required unless `--dump-only`) `--tail-overlap --valuation-days --daily-basic --dump-only --anonymous` |
| `dump-cross-section` | today's score CSV | `--via screener/snapshot --idx --max-ec --anonymous` |
| `accumulate` | append one daily PIT snapshot (prints availability + promotion status) | same dispatch + `--as-of` (default: today) `--min-days` |
| `accumulate-margin` | append one daily margin (两融) snapshot — Layer-3 forward-only PIT; `--catchup K` is the only sanctioned backfill (a real `margin.query` read) | `--symbols --catchup --max-ec --anonymous` |
| `write-universe` | `instruments/<name>.txt` from index membership or a codes file; `--index` prefers PIT membership episodes when the SDK exposes `index.members_history` (one episode per membership run; dropped members keep their closed episode — survivorship-safe) | `--index/--codes-file --exclude-st --exclude-suspended --max-ec --anonymous` |
| `verify` | read `.bin` back with Qlib's own reader | `--qlib-dir --check inst:field:date:value` |
| `reconcile-daily-basic` | Gate B: accumulated PIT vs official daily_basic history (100% or fail) | `--accumulated --upstream --tol`, or `--from-upstream --store --max-ec` |

`--dry-run` is available on four of them: `dump-history`, `update-history`,
`dump-cross-section`, `accumulate`.

## Budgets

Every live command takes a required `--max-ec` — a **budget ceiling**, not a
hint. The run stops submitting work once the ceiling is reached: per-symbol
paths stop between batches, one-shot paths (screener/snapshot cross-section,
margin) check post-hoc after the single request returns. If a run stops early
with a ceiling message, raise the ceiling and re-run — `--resume` picks up
where it left off. `--dry-run` prints the run scope before anything fires.

Two usage rules the ceiling exists to protect:

- Use `--n` for increments, never range (`begin/end`) queries — a range query
  is bounded by the server's anchor window, not by the rows you asked for
  (see [docs/data-model.md](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/data-model.md)).
- Keep a daily cadence on a scoped universe — `--via snapshot` over an explicit
  ~300-name list, or `--idx index.hs300` on the screener path. Full-market
  scans are a weekly/monthly cadence at most; full-market daily is not viable
  on any current endpoint.

Every fetch result carries a `metering` record (`requests`, `note`,
`auth_mode`) written to `fetch_meta.json` / `*.meta.json`.

## Data-correctness gates

- **`.bin` layout** — `features/<inst>/<field>.day.bin` is
  `[start_index, values…]` where `values[0]` is the value for
  `calendar[start_index]` and the payload ends at the instrument's own last
  date — exactly what Qlib's reader resolves (`seek(4*(i-start_index)+4)`) and
  what Qlib's `dump_bin` writes. Dumps made by ≤ 0.7.0 whose instruments did not
  all start on the calendar's first day were shifted; re-dump them locally,
  offline: `wmshare-qlib update-history --dump-only --store <store> --out <out>`.
- **Direction** — every series is asserted ascending (2.x contract); a silently
  descending series would flip every rolling computation.
- **Unit invariants** — amounts in CNY, volumes in shares, ratios dimensionless;
  absurd magnitudes raise rather than flow into a backtest.
- **Duplicate/conflict** — duplicated dates with identical values are deduplicated
  with a warning; **conflicting** duplicates raise. Incremental tails are merged
  into the store, never overwritten, so a gap between updates cannot drop bars.
- **Reconciliation gate** — incremental tails must agree with stored history within
  float tolerance. An upstream revision fails loud; history is never silently
  rewritten.
- **Gate B** — accumulated PIT values are cross-checked against official
  `daily_basic` history, 100% agreement on a **non-empty** overlap or fail (an
  empty comparison, e.g. a budget-truncated pull, is reported as INCOMPLETE).

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

- `config.py` — fields, units, metric-code selection
- `normalize.py` — anti-pitfall guards (direction, code mapping, unit, cold cache); `merge_adjusted_klines` (hfq+raw → factor)
- `fetch.py` — live acquisition (credentials or public-pool free trial) + metering + budget-ceiling executor + bounded transient retries
- `panel.py` — integrity assertions (incl. the factor gate) + wide transform
- `dump.py` / `format.py` — Qlib-compatible `.bin` dumper and its on-disk contract (Qlib-free)
- `qlib_io.py` — **the only module that imports Qlib**; child-process read-back + score reader
- `resume.py` — fragment store (bulk + incremental tails) + manifest with convention guard and reconciliation gate
- `cross_section.py` — score CSV + PIT accumulation/promotion
- `reconcile.py` — Gate B comparison
- `cli.py` — the seven commands

## Tests

```bash
# Tier 1 (mandatory in CI, offline, no credentials, no network)
pytest tests -q -m "not live"

# Tier 2 (optional) — adds the child-process Qlib read-back cross-check
pip install 'wmshare-qlib[qlib]'
pytest tests -q -m qlib
```

Offline tests run against synthetic SDK-shaped stubs, so they need no
credentials and never touch the network. Tier 1 verifies the dumped `.bin` with
pure numpy (`format.read_bin_selfcheck`); Tier 2 additionally reads it back
through Qlib itself. Live tests are opt-in (gated by `WMSHARE_QLIB_LIVE=1`) and
require REAL credentials — a public-pool free-trial client is skipped loudly.
See [CONTRIBUTING.md](https://github.com/open-winmale/wmshare-qlib/blob/main/CONTRIBUTING.md).

## What we deliberately do NOT do

- Fabricate history from today's snapshot (red line — see layer 2 above).
- Recompute upstream precomputed metrics client-side and present them as wmshare values.
- Per-symbol `indicator.get` loops (slow and 429-prone).
- Run a live metric-code `screener.query` without an explicit `--max-ec` budget.
- Call `qlib.init()` in the caller's process (global side effect).
- Touch the wmshare main repo (separate repo, no version-bump churn).

## See also

- [Getting started](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/getting-started.md) — install to first `.bin`, free trial then production credentials.
- [Data model](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/data-model.md) — the three availability tiers and why layer 2 cannot be fixed client-side.
- [FAQ](https://github.com/open-winmale/wmshare-qlib/blob/main/docs/faq.md) — free-trial boundaries, hfq vs qfq, error reference.
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
