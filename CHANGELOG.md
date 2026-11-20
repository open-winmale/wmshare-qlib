# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While on `0.x`, minor versions may change public API; patch versions will not.

## [Unreleased]

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
