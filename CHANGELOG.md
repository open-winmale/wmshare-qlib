# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While on `0.x`, minor versions may change public API; patch versions will not.

## [Unreleased]

### Removed (BREAKING)

- **The EC cost/estimation layer is gone from the user-facing surface.** The
  adapter no longer documents or quotes upstream billing — docs explain usage
  only. Concretely:
  - `wmshare_qlib.estimate` (the pre-flight EC estimator) and the
    `config.EC_*` anchor constants are **deleted**. `--dry-run` now prints a
    run-scope preview (symbols / n / fields / store) instead of an EC quote.
  - `fetch_meta.json`, `*.meta.json` and the resume **manifest no longer
    persist EC fields** (`metering.ec_cost`, `ec_spent`); the `[metering]`
    line prints scope + `auth_mode` only. Old stores whose manifest still
    carries `ec_spent` load unchanged (the key is ignored).
  - Internals unchanged: the `--max-ec` budget ceiling, its probe-based batch
    sizing and post-hoc enforcement still run on live upstream metering —
    `Metering.ec_cost` remains internal guard state. If a run stops early
    with a ceiling message, raise the ceiling and re-run; `--resume` picks up
    where it left off.
- Docs: the README "EC cost model" section and data-model "EC costs" /
  "Why the free trial is not free" sections are removed (a compact
  "Budgets" usage section replaces them). `docs/internal/` engineering
  records are unchanged.

### Changed

- Layer-2 wording clarified across README / data-model / package docstring:
  the ~2,750 precomputed metric codes belong to the screener/indicator domain,
  whose query API accepts no date parameter — historical reads are
  inexpressible upstream. This is a different catalog from the layer-1
  valuation fields (`valuation.hist_stats` / `daily_basic`), which DO carry
  dated series; the FAQ adds an entry on exactly this distinction (including
  the partial `$PE_TTM_LAST`→`pe_ttm` / `$PB_TTM_LAST`→`pb` bridge via
  daily_basic).

## [0.8.1] - 2026-11-24

### Fixed

- **A re-dump prunes stale `.bin` files: the output dir is exactly the panel.**
  `dump_panel` only ever wrote/overwrote files, so an instrument left from an
  earlier dump with a DIFFERENT calendar stayed on disk and was served by Qlib
  against the new calendar — silently wrong dates, no error. Measured on this
  machine: `out/qlib_data` held seven demo-era files (726-value payloads) next
  to a 2,427-day calendar. `dump_panel` now removes `features/*/*.<freq>.bin`
  files it did not write (and empty instrument dirs), and the dump manifest
  reports `removed`. The output dir is now canonical for the store.
- **Upstream short kline windows are reported, never silent.** Observed
  2026-11-23: `kline.bars(n=60)` served 59 bars for every symbol, so the live
  store's 60-bar panel started at 08-24 instead of 08-21 — nothing filled (the
  merge's date-set guard held), but nothing said so either. Both the bulk and
  tail kernels now emit a warning naming the served/requested counts and the
  realized window (`tail` adds the seam-hole caveat).

### Changed

- README/getting-started bulk examples use `--max-ec 30000` for two symbols
  × 60 bars (quote 19,346 EC; measured 11-23: 9,872 EC/symbol = 19,744; a
  compute-priced valuation (712/metric) would make it 12,505/symbol = 25,010 —
  so the old 20,000 could silently skip the second symbol).
- Unified the precomputed-metric-code count to **~2,750** (was 2,752 in
  `__init__.py`, ~2,750 in README/data-model).
- CI: new `floor` job pins `wmshare==3.3.0` before the package install (the
  other jobs' `wmshare>=3.3.0,<4` resolves to the newest 3.x — measured 3.5.0),
  so the declared floor is actually exercised. Verified locally: the full
  Tier-1 suite passes on 3.3.0 (180 passed).

## [0.8.0] - 2026-11-24

### Added

- **`update-history --dump-only`** — re-dump the merged store (bulk + tails) to
  `.bin` with zero requests and zero EC; no `--max-ec` needed. This is the
  migration path after an on-disk layout change (see Fixed below) and the
  honest way to refresh `.bin` right after a tail merge without paying for
  another fetch.
- **`accumulate-margin` — Layer-3 margin (两融) accumulation** (roadmap P2-2
  unblocked: pricing was probed 2026-11-18/11-24, the 2.0 P1 margin face ships
  in the required SDK floor). One `margin.latest` per symbol per day
  (~182 EC/票, metered) lands as `out/margin_accum/<date>.parquet` — six real
  columns (fin_* CNY yuan, loan_* shares), forward-only PIT, never backfilled.
  `--catchup K` is the only sanctioned backfill: an explicit, budgeted
  `margin.query(limit=K+2)` read (~156 base + ~119 EC/row probed; cheaper than
  daily snapshots from 3 missed days up). New `config.MARGIN_FIELDS` +
  `FIELD_UNITS` declarations, `normalize.margin_rows_to_frame` (unit guard,
  duplicate-row fail-loud), `fetch.fetch_margin_snapshot` / 
  `fetch_margin_history`, and a `margin_store` module — promotion to a .bin
  panel reuses `dump.dump_panel` directly (no score-guard semantics on raw
  captures).

### Changed

- **EC price anchors re-probed (2026-11-24, clean symbol + cross-check,
  7,236 EC total):** `kline.bars` base 156 → 242 (per-bar 67 unchanged;
  n=10 → 912, n=15 → 1,247 measured), `moneyflow.bars` base 156 → 173
  (per-bar 16 unchanged), `stocks.list` 66 → 109, `kline.snapshot` N=3 →
  309 (103/票). **`valuation.hist_stats` dropped 712 → 4 EC per call** —
  flat across symbols/metrics/windows since the 11-18 materialized wide
  table turned the series into a cheap read; noted in config that a
  compute-pricing revert would jump it back ~178×. Range-query and screener
  anchors kept at their measured values with coherence notes. All
  `estimate.py` formulas and the dry-run quotes follow the new anchors;
  `--max-ec` and paid `metering.ec_cost` remain the only binding numbers.
- `estimate.margin_query` re-anchored to the 2026-11-24 probes (base 156 +
  119/row; the old flat ~124/row model over-counted tiny limits).

### Fixed

- **`.bin` payload now starts at `start_index` — Qlib's actual contract.**
  `dump_panel` reindexed every series onto the FULL global calendar while
  writing the instrument's `start_index` header, but Qlib resolves
  `calendar[i]` at file offset `4*(i-start_index)+4`: every instrument whose
  first date was not `calendar[0]` had its series shifted later by
  `start_index` slots (and `instrument_date_ranges` over-reported coverage to
  the calendar end). The payload is now sliced to the instrument's own
  `[first, last]` date — byte-for-byte the shape Qlib's `scripts/dump_bin.py`
  writes — and `FORMAT_CONTRACT` is 2.0. Verified with Qlib 0.9.7's own reader
  (`D.features`) and a raw-float32 layout assertion on staggered instruments;
  the old suite could not see it because every stub gave all instruments the
  same dates (`start_index == 0`). **Upgrade action:** re-dump any
  multi-instrument `out/` built by ≤ 0.7.0 (local, zero EC):
  `wmshare-qlib update-history --dump-only --store <store> --out <out>`.
  Dumps where all instruments share the calendar's first date are unchanged
  (verified byte-identical on the 3-symbol live store).
- **Incremental tails are merged, never overwritten.** `record_tail_fragment`
  replaced `tail/<symbol>.parquet` with the new window, and a window reaches
  back only `gap + overlap` bars: after a catch-up tail, the next update
  destroyed the older post-bulk bars (a silent hole the following full re-dump
  wrote into the `.bin`). Tails are now merged with the on-disk tail under the
  same tolerance gate as bulk/tail (`_merge_rows`; disagreement raises, NaN
  never shadows a value). A rebuilt bulk fragment supersedes its tail (state and
  file), only symbols with recorded tail state are read, and `dump-history`
  re-dumps the merged store — a `--resume` re-dump used to drop every tail row.
- **`accumulate` is dated: `--as-of` defaults to today.** It defaulted to
  `None`, so every run wrote `None.parquet` — one rolling file,
  `available_from="None"`, and the PIT promotion guard could never pass. The
  library now rejects a missing/non-ISO `as_of`; non-date stems (legacy
  `None.parquet`) are excluded from `history_days` and promotion, and the CLI
  reports them. `dump-cross-section`'s rolling `snapshot.csv` is intentionally
  unchanged.
- **`--anonymous` reaches every worker client.** With `--workers > 1` the pool
  built clients via `_client()` without `anonymous`, so a credentialed run
  billed the private balance while metering (built from the anonymous main
  client) reported `public_pool`. Both the bulk and tail kernels now pass
  `anonymous` to worker clients.
- **`update-history --daily-basic` actually fetches daily_basic.** A
  `fields = config.HISTORY_FIELDS` reset right before the fetch dropped the
  flag's fields, while the dry-run kept quoting the read.
- **`write-universe` bills only when it must, and honours `--max-ec`.**
  `--codes-file` mapped through `stocks.list` (~109 EC) even when every code
  was already Qlib-style — with no budget and no metering line — and `--index`
  treated `--max-ec` as an entry ticket with no ceiling. All-Qlib-style codes
  are now a zero-request path; a billed mapping requires `--max-ec` and reports
  its EC; mapping + index/hygiene spend is checked against the ceiling
  post-hoc.
- **Gate B no longer passes on an empty comparison.** `ok` was
  `n_mismatch == 0`, so a disjoint overlap (or a budget-truncated pull that
  produced no rows) printed PASS. `ok` now requires `n_compared > 0`, and
  `--from-upstream` exits INCOMPLETE when the budget stopped the pull short.
  Zero comparisons is absence of evidence, not agreement.
- `format.decode_series` on a missing `.bin` file now returns an all-NaN
  series (symmetric with `read_bin_selfcheck`'s None) instead of raising
  `FileNotFoundError`.

### Docs

- Full-sample Gate A equivalence audit recorded (2026-11-24): all 5,694
  symbols, 2,363,314 exact matches (99.936%), 1,484 mismatches decomposed
  into intraday-basis transition artifacts (136, known dates) and a
  window-length dependence in the engine's `HIST_STATS` on 2-3 tickets
  (1,348; registered as upstream request 4). Measured full-market daily fuel
  (307,340 EC/day ≈ 54 EC/ticket, 5.8× below the review estimate) folded
  into the expansion review as a dated addendum. Internal docs only.
- Corrected stale cost notes: `indices.members` is probed (~12,754 EC full
  pull) — the "not yet probed" wording in `fetch.fetch_index_members` and the
  `write-universe --index` help is replaced, and the roadmap P2-2 margin
  section now reflects the completed pricing probe (124 EC/row, 182 EC
  latest).
- Removed a duplicated FAQ heading; unified the benchmark-card horizon
  wording to 3y (n=750=250×3, matching the recorded cost structure); closed
  the roadmap HK item (README is CN-scoped; the `hk` exchange suffix stays).
- Documented the pyqlib packaging defect behind the `read_score` fallback:
  0.9.6/0.9.7/main all ship without `qlib/contrib/model/base.py`, so the
  official ScoreFileModel import is unreachable on every published version.

### Added

- **Local automation kit** (zero EC): `tools/freshness.py` — an offline
  store check with two-level semantics (exit 1 = internal corruption,
  exit 2 = stale vs weekdays, CN holidays not modeled by design) emitting a
  one-line JSON verdict; `tools/daily_update.sh` — the soft-fail daily chain
  (update-history → accumulate → accumulate-margin → freshness, freshness
  always last and its exit code is the chain's); a launchd plist template for
  a 17:30 local run. See getting-started §8.
- Direct test coverage for `format.decode_series` (round-trip, missing file,
  truncated tail, custom freq) and the `verify` CLI argparse path.

## [0.7.0] - 2026-11-24

### Added

- **Public-pool free-trial integration (wmshare >=3.3).** Without credentials
  the SDK's `from_env()` returns a free-trial client (no token, shared quota,
  metered); wmshare-qlib surfaces WHO paid in every metering record: the new
  `metering.auth_mode` field (`credentials` / `public_pool` / `cache_hit`)
  lands in `fetch_meta.json` / `*.meta.json`, and the CLI prints a one-line
  `[free-trial]` stderr hint on public-pool runs.
- **`--anonymous` on every billed command** (dump-history / update-history /
  dump-cross-section / accumulate / write-universe / reconcile-daily-basic
  `--from-upstream`): force the free trial and ignore configured credentials,
  mirroring the SDK CLI.
- Public-pool terminal refusals are **never retried**: `LimitError(code=
  "ip_rate_limited")` (plus `pool_unavailable` / `ip_banned`) now bypasses the
  bounded transient retry — upstream's shared quota is not refilled by
  retrying. Regular paid-app 429 rate limiting keeps its retry.

### Changed

- **`wmshare>=3.3.0,<4`** (was `>=2.5.0,<3`). The public pool, `auth_mode`,
  and the free-trial fallback ship in 3.x; 2.x is past EOL for this adapter.
- **Demo mode retired; live is the only data mode.** The SDK removed the
  offline `demo_client()` in wmshare 3.0, so 0.7.0 drops the demo path: the
  `--live` flag still parses (no-op; main prints a one-time note) and every
  billed command now requires `--max-ec` unconditionally. The default
  `--symbols` (a 10-code starter universe) is kept so a bare dump targets a
  budget-sized set. Zero-credential work continues on the free trial.
- `_credential_hint` messages now name the free trial, `WMSHARE_DISABLE_ANON=1`,
  and the shared-vs-private balance distinction (registration pointers kept).

### Fixed

- **Live test suite can no longer silently burn the shared quota.** The
  `live_client` fixture (wmshare >=2.8 auto-fallback era) relied on
  `from_env()` raising without credentials; under the free-trial fallback it
  would instead REALLY run on a public-pool client. The fixture now checks
  `auth_mode` and skips loudly unless it is `"credentials"`; the demo-vs-live
  direction test became credentials-vs-public-pool.
- Guard-ordering hardened where the old `--live` guards used to be: guards on
  `write-universe` and the snapshot cross-section still fire BEFORE any client
  is built (a constructed client no longer raises on missing credentials — it
  would silently select the free trial), enforced by offline tripwire tests.

## [0.6.0] - 2026-11-20

### Fixed

- **`[qlib]` extra installs a working environment.** The extra now pins
  `pyarrow<26`: pyarrow 26.x requires numpy>=2 at runtime while declaring no
  numpy constraint in its metadata, so combined with the extra's `numpy<2`
  (pyqlib is built against 1.x) a plain `pip install 'wmshare-qlib[qlib]'`
  resolved to `ImportError: pyarrow requires NumPy 2.0 or newer` and every
  dump crashed in `to_parquet`. pyarrow 25.x is verified compatible with
  numpy 1.26 (full suite green inside the extra). CI now proves it: the
  `qlib-extra` job installs the extras with NO manual pins and runs the FULL
  suite (the 0.5.0 job ran only the single qlib-marked test, which is how the
  break shipped green).
- **`--max-ec` is now actually required on every billed live path.** 0.5.0
  documented it as required but only `update-history` enforced it. Missing
  gates added: `dump-history --live` (guard sits after `--dry-run` — quoting
  stays free), `reconcile-daily-basic --from-upstream` (the 0.5.0 changelog
  claimed this gate existed; it ships now), and `write-universe --index
  --live` (cost not yet probed upstream). Error messages now point at the
  public README EC table instead of an internal docs path.

### Changed

- **Python 3.11 only.** `requires-python` is now `>=3.11,<3.12`: pyqlib 0.9.7
  ships wheels only for 3.10–3.12, so the previous ">=3.11" claim let 3.13+
  users install the core and then fail on the recommended `[qlib]` extra.
  3.12+ is refused at install time instead. CI matrix collapsed accordingly.
- Default `--symbols` universe fixed: the old `csi300_universe()` list
  contained `002415`, which the demo snapshot does not serve, so a bare
  `dump-history` failed on the very run it was meant to serve. Renamed to
  `panel.demo_universe()` and set to the demo snapshot's real 10 codes.

### Added

- `wmshare-qlib --version` (CONTRIBUTING already told users to run it; it used
  to exit 2).

### Docs

- README links are absolute GitHub URLs: PyPI renders only the README, where
  every repo-relative link was dead.
- `docs/data-model.md` corrected the `ScoreFileModel` import path and states
  plainly that pyqlib 0.9.7's class is unreachable (broken upstream import);
  the adapter's read-back uses the equivalent local reader, labelled
  `local-equivalent`. FAQ now lists the demo universe's 10 codes.
- CI installs wmshare at the declared floor (`>=2.5.0`) instead of the stale
  `>=2.2.0`.

## [0.5.0] - 2026-11-20

### Added

- **10-year valuation history.** `config.VALUATION_WINDOW` now defaults to
  `"10y"` (was `"3y"`): the `pe/pb/ps/pr` panel series reaches back to 2016-11
  (live probe 2026-11-20: 600519 pe 10y = 2,450 daily points). The engine never
  had a year cap — kernel `parseWindow` accepts any `Ny/Nq/Nm`; the old "3y
  cap" was an SDK allowlist, relaxed in wmshare 2.5.0. The window is recorded
  in the store manifest params for audit.
- **PIT index-universe episodes.** `write-universe --index` now prefers the
  upstream materialized membership-snapshot table (`index.members_history`,
  wmshare ≥ 2.5.0): daily membership snapshots are folded into
  per-instrument episodes (`dump.universe_episodes_from_history`) and written as
  multi-row Qlib instruments files — `D.instruments("<name>")` resolves
  membership point-in-time, and dropped members keep their closed episodes
  (survivorship-safe). Forward-PIT only: earlier history does not exist
  upstream. SDKs without the facade fall back to the previous single-snapshot
  path. Hygiene flags are ignored (and warned) on this path — filtering by
  today's ST/suspension state would rewrite history.
- **`$vwap` field.** The dumper derives `vwap = amount / volume` at dump time
  (`dump._append_vwap`); the upstream kline surface has no vwap. Stored volume
  is already divided by the adjustment factor, so the quotient is consistent
  with the adjusted (hfq) prices and preserves the `close × volume == amount`
  invariant. Suspension days (zero volume) stay NaN. Qlib's bundled cn_data
  ships `$vwap` and Alpha158 consumes it, so the full Alpha158 surface is now
  reachable (`examples/alpha158_lgbm.py` includes a `$vwap` feature).

### Changed

- Requires `wmshare >= 2.5.0`: that release relaxed the `hist_stats` window
  allowlist (5y/10y) and added the `index.members_history` facade. Older SDKs
  would reject `window="10y"` client-side.

## [0.4.0] - 2026-11-19

First public release: installable from PyPI, with user-facing documentation and a
documented public surface.

### Added

- **PyPI distribution.** `pip install wmshare-qlib` now works. Packaging metadata
  completed (authors, maintainers, keywords, classifiers, project URLs, `py.typed`
  for downstream type checkers) and the version is single-sourced from
  `wmshare_qlib.__version__`.
- **User documentation:** `docs/getting-started.md` (install → demo → credentials →
  live, with verified command output), `docs/data-model.md` (the three availability
  tiers and the EC cost model), `docs/faq.md` (demo boundaries, hfq vs qfq, error
  reference), `CONTRIBUTING.md`, and this changelog.
- **`--max-ec` on `reconcile-daily-basic --from-upstream`.** The one remaining billed
  path without a budget gate now refuses to spend without an explicit ceiling.
- **Actionable credential/quota errors.** A missing credential no longer surfaces as
  a raw SDK traceback: it names `WMSHARE_CLIENT_ID` / `WMSHARE_CLIENT_SECRET` and
  points at the application portal. Quota exhaustion says what to do rather than
  just failing.
- `pyproject.toml` `qlib` extra pins `numpy<2` (pyqlib 0.9.x is built against 1.x).

### Changed

- **`__all__` now lists the full public surface** (`config`, `normalize`, `fetch`,
  `panel`, `cross_section`, `dump`, `format`, `qlib_io`, `resume`, `estimate`,
  `reconcile`). Previously `qlib_io` / `estimate` / `resume` / `format` /
  `reconcile` were importable but undeclared.
- `dump-cross-section --dry-run` now honours `--idx` instead of always quoting the
  full-market 204,430 EC anchor.
- README rewritten for newcomers: two explicit quick-start paths (zero-cost demo,
  then live), correct credential variable names, no dependency on the local-only
  `.qlibvendor` directory, and the demo snapshot anchors labelled as such.
- Scope statement corrected to **CN**; HK is declared in the exchange mapping but
  not end-to-end validated.

### Fixed

- `fetch_cross_section(live=True, universe=...)` no longer ignores `universe`
  silently — it warns and directs the caller to `idx=`.
- Blocking auth/quota errors are no longer downgraded to warnings by the
  per-field fetch wrappers (which could previously produce a panel with silently
  missing fields and exit code 0).
- `config.HISTORY_FIELDS` was defined twice (dead duplicate removed).
- Internal engineering records moved to `docs/internal/` so they are no longer
  linked as user-facing entry points.

## [0.3.3] - 2026-11-19

### Fixed

- `__version__` was out of sync with `pyproject.toml` (0.3.0 vs 0.3.2); the two
  sources are now aligned and covered by the single-source version scheme.

### Changed

- `update-history` tail reads of `valuation.daily_basic` are windowed from the
  resume pointer instead of re-reading full history (≈2k EC/symbol/run instead of
  ≈18.3k); `estimate.tail_symbol` and the `--dry-run` quote gained the matching
  daily_basic line item.
- `reconcile-daily-basic --from-upstream` can pull the official side over the
  accumulated window directly, removing the hand-fed file step.

## [0.3.2] - 2026-11-18

### Changed

- Adapted to wmshare 2.2.0: `_fields` projection, `BatchResult.truncated`, and
  measured range-query billing (range queries force-pay the anchor window).

## [0.3.0] - 2026-11-18

### Added

- `--daily-basic` upstream valuation consumption (gated): `pe_ttm`, `total_mv`,
  `circ_mv`, `turnover_rate`, `dv_ttm` added to the `.bin` panel when the upstream
  materialized table is reachable; degrades to an explicit skip warning otherwise.
- `reconcile-daily-basic` (Gate B): accumulated PIT values cross-checked against
  official `daily_basic` history, 100% or fail.

## [0.2.2] - 2026-11-17

### Changed

- wmshare 2.x adaptation: ascending series, unified `{ok, as_of, data, meta}`
  envelopes, `_df` removal.

## [0.2.1] - 2026-11-17

### Changed

- Documentation restructure; the vnpy injection channel extracted to a separate
  project (`wmshare-vnpy`).
- PIT series `available_from` semantics documented: a series is available from its
  first captured day, with a configurable cold-start guard gating promotion.

## [0.2.0] - 2026-11-17

### Added

- Incremental daily updates (`update-history`, live + hfq only) with a
  reconciliation gate against stored history.
- Universe files (`write-universe`) and deep history (`--years N`).
- Sustainable daily cross-section via `--via snapshot`.

## [0.1.0] - 2026-11-16

### Added

- Initial pipeline: history panel → Qlib `.bin`, cross-section score CSV,
  concurrent per-symbol fetch with resumable fragments and a hard EC ceiling.
