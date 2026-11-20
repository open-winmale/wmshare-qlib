# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While on `0.x`, minor versions may change public API; patch versions will not.

## [Unreleased]

### Added

- **`config.VALUATION_WINDOW`** (default `"3y"`). The valuation `pe/pb/ps/pr`
  series window is now a knob. The engine itself has no year cap — kernel
  `parseWindow` accepts any `Ny/Nq/Nm`; live probe 2026-11-20 returned 2,450
  daily pe points (2016-11-21 → now) for `window="10y"`. The old "3y cap" was
  an SDK allowlist, relaxed upstream in lockstep. Flip to `"10y"` once
  wmshare ≥ 2.5.0 (relaxed allowlist) ships; the window is recorded in the
  store manifest params for audit.

### Added

- **PIT index-universe episodes.** `write-universe --index` now prefers the
  upstream materialized membership-snapshot table (`index.members_history`,
  wmshare ≥ next release): daily membership snapshots are folded into
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
