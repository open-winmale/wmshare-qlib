"""Fetch layer: demo (offline snapshot) and live (credentialed) data acquisition.

Design rules (from the plan):
  * History panel: per-symbol calls. demo does NOT support batch_iter; live uses
    batch_iter in batches of 500. Never per-symbol indicator.get (EC explosion).
  * Cross section: live uses screener.query (1 request, EC not scaled by symbols).
    demo has no full-market index, so we derive a snapshot score from
    kline.batch_live to validate the PIPELINE offline.
  * Every fetch result carries a metering record; EC is passed through, never hidden.
  * M3-lite: per-symbol fetch is isolated (``_fetch_one_symbol``) so it can be
    concurrent; each worker owns its client so ``last_ec_cost`` has no race.
    A hard EC ceiling is checked at batch granularity (see ``max_ec`` doc).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

from . import config, normalize


@dataclass
class Metering:
    """EC / request accounting. Batch saves requests, NOT EC - we state both."""

    mode: str = "demo"
    requests: int = 0
    ec_cost: float | None = None  # None = demo has no EC accounting
    note: str = "demo offline snapshot: no EC meter; live bills by symbol/compute"

    def as_meta(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class FetchResult:
    frame: pd.DataFrame
    metering: Metering = field(default_factory=Metering)
    warnings: list[str] = field(default_factory=list)


def _client(live: bool, max_limit: int | None = None, timeout: float | None = None) -> Any:
    import wmshare

    if live:
        # Live requires credentials in env; fail loud with a clear message.
        # max_limit is forwarded (e.g. 10000 lets screener.query fetch the
        # whole universe in ONE request - pagination re-pays EC). timeout is
        # forwarded because full-universe column materialization can exceed
        # the SDK's 30s default (measured: TransportError read timeout).
        kw: dict[str, Any] = {}
        if max_limit:
            kw["max_limit"] = max_limit
        if timeout:
            kw["timeout"] = timeout
        return wmshare.Client.from_env(**kw)
    return wmshare.demo_client()


def _fetch_one_symbol(
    c: Any,
    symbol: str,
    instrument: str,
    fields: tuple[str, ...],
    n: int,
    live: bool,
) -> tuple[list[pd.DataFrame], int, float | None, list[str]]:
    """Fetch ALL history fields for one symbol via one dedicated client.

    Returns ``(frames, requests, ec_cost, warnings)``. ``ec_cost`` is None in
    demo (no ``last_ec_cost`` attribute) and the SUM of per-call EC in live.
    Owning the client per call keeps ``last_ec_cost`` free of write races when
    the caller runs several symbols concurrently.
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    requests = 0
    ec_total = 0.0
    has_meter = False

    def _meter() -> None:
        nonlocal ec_total, has_meter
        last = getattr(c, "last_ec_cost", None)
        if last is not None:
            ec_total += float(last)
            has_meter = True

    # --- kline (OHLCV + amount) ---
    if live:
        kraw = c.kline.bars(symbol, n=n, adj="qfq", _df=True)
    else:
        # demo supports n<=120, no adj, no begin/end
        kraw = c.kline.bars(symbol, n=min(n, 120), _df=True)
    requests += 1
    _meter()
    n_raw = len(kraw)
    kdf = normalize.kline_to_frame(kraw, symbol=symbol)
    if len(kdf) < n_raw:
        warnings.append(
            f"{symbol}/kline: upstream returned {n_raw - len(kdf)} duplicated "
            f"date row(s) with identical values; deduplicated "
            f"(conflicting duplicates would have raised)"
        )
    kdf["instrument"] = instrument
    # wmshare returns qfq-adjusted prices directly (no factor column).
    # Qlib requires `factor` as a standard field; under our declared
    # qfq_const convention the adjustment factor is 1 for every date.
    # Emit it explicitly (never silently omit a declared field).
    if config.qfq_factor_mode() == "qfq_const" and "factor" not in kdf.columns:
        kdf["factor"] = 1.0
    keep = [c0 for c0 in ("date", "symbol", "instrument", *config.BASE_FIELDS) if c0 in kdf.columns]
    frames.append(kdf[keep].melt(
        id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
    ))

    # --- valuation (pe/pb/ps/pr) ---
    for vfield in config.VALUATION_FIELDS:
        if vfield not in fields:
            continue
        # demo snapshot only stores pe/3y; other metrics raise UsageError.
        # Skip unsupported metrics with an explicit warning (cold-cache guard:
        # never fabricate, never fill 0). Live supports all four.
        try:
            payload = c.valuation.hist_stats(symbol, metric=vfield, window="3y")
        except Exception as exc:  # wmshare.errors.UsageError and friends
            requests += 1
            _meter()
            warnings.append(
                f"{symbol}/{vfield}: skipped ({type(exc).__name__}: "
                f"{str(exc)[:60]})"
            )
            continue
        requests += 1
        _meter()
        if normalize.annotate_missing(payload):
            warnings.append(f"{symbol}/{vfield}: ok=False skipped (cold cache)")
            continue
        n_series = len(payload.get("series") or [])
        vdf = normalize.valuation_series_to_frame(payload, symbol=symbol)
        vdf = vdf.rename(columns={"pe": "value"})
        if vdf.empty:
            warnings.append(f"{symbol}/{vfield}: empty series skipped")
            continue
        if len(vdf) < n_series:
            warnings.append(
                f"{symbol}/{vfield}: upstream returned {n_series - len(vdf)} "
                f"duplicated date row(s) with identical values; deduplicated"
            )
        vdf["instrument"] = instrument
        vdf["field"] = vfield
        frames.append(vdf[["date", "symbol", "instrument", "field", "value"]])

    # --- moneyflow ---
    if any(f in fields for f in config.MONEYFLOW_FIELDS):
        # demo snapshot keeps at most 30 bars; live has no such cap.
        mf_n = n if live else min(n, 30)
        try:
            mraw = c.moneyflow.bars(symbol, n=mf_n, _df=True)
        except Exception as exc:
            requests += 1
            _meter()
            warnings.append(f"{symbol}/moneyflow: skipped ({type(exc).__name__}: {str(exc)[:60]})")
            mraw = None
        if mraw is not None:
            requests += 1
            _meter()
            n_mf_raw = len(mraw)
            mdf = normalize.moneyflow_to_frame(mraw, symbol=symbol)
            if len(mdf) < n_mf_raw:
                warnings.append(
                    f"{symbol}/moneyflow: upstream returned {n_mf_raw - len(mdf)} "
                    f"duplicated date row(s) with identical values; deduplicated"
                )
            mdf["instrument"] = instrument
            mf_keep = [c0 for c0 in ("date", "symbol", "instrument", *config.MONEYFLOW_FIELDS)
                       if c0 in mdf.columns]
            frames.append(mdf[mf_keep].melt(
                id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
            ))
        if not live and n > 30:
            warnings.append(f"{symbol}/moneyflow: demo capped at 30 bars (live={n})")

    return frames, requests, (ec_total if has_meter else None), warnings


def fetch_history_panel(
    symbols: Iterable[str],
    *,
    live: bool = False,
    fields: tuple[str, ...] = config.HISTORY_FIELDS,
    n: int = 120,
    client: Any | None = None,
    max_workers: int = 1,
    max_ec: float | None = None,
    on_symbol: Callable[[str, str, list[pd.DataFrame], float | None], None] | None = None,
) -> FetchResult:
    """Fetch per-symbol history and merge into a long panel.

    M3-lite additions (all backward compatible - defaults keep old behavior):
      * ``max_workers``: concurrent workers. Each worker owns a fresh client
        (or reuses ``client`` when workers<=1) so ``last_ec_cost`` never races.
        Results are merged in the ORIGINAL symbol order for determinism.
      * ``max_ec``: hard EC ceiling for THIS call, enforced BEFORE submitting each
        batch. A probe batch of 1 symbol measures real per-symbol EC; later batches
        are sized as floor(remaining_budget / per_symbol_upper_bound), where the
        bound only ever increases. Honest worst case: the single probe symbol may
        itself exceed the ceiling (cost is unknowable before the first call).
      * ``on_symbol(symbol, instrument, frames, ec)``: called right after a
        symbol succeeds, so a caller can persist a resumable fragment before an
        interruption. The callback must be cheap and side-effect-local.

    Anti-pitfall #5: symbols with cold-cache / short window are skipped and
    listed in warnings, never filled with zeros.
    """
    c_main = client if client is not None else _client(live)
    instrument_map = normalize.build_instrument_map(c_main)
    symbols = list(symbols)
    warnings: list[str] = []

    # The demo snapshot client is NOT concurrency-safe: parallel calls corrupt
    # its shared kline/moneyflow cursor (we observed 60 rows -> 3). Demo is free
    # (zero EC), so concurrency buys nothing there. Force serial for demo; only
    # live (credentialed, billable) uses workers. The clamp is EXPLICIT in
    # warnings, never silent.
    effective_workers = 1 if not live else max(1, int(max_workers))
    if not live and max_workers > 1:
        warnings.append(
            f"demo mode is not concurrency-safe; max_workers={max_workers} "
            f"clamped to 1 (serial)"
        )
    if client is not None and effective_workers > 1:
        # A caller-supplied client is shared across the pool and NOT known to be
        # thread-safe; the per-worker fresh-client path only applies to clients
        # we create ourselves. Clamp to serial instead of silently ignoring the
        # caller's client (P1-2).
        effective_workers = 1
        warnings.append(
            f"caller-supplied client cannot be shared across workers "
            f"(thread-safety unknown); max_workers={max_workers} clamped to 1"
        )
    unknown = [s for s in symbols if s not in instrument_map]
    if unknown:
        raise normalize.NormalizeError(
            f"symbols outside universe: {unknown[:5]} (limit universe to stocks.list codes)"
        )

    meter = Metering(
        mode="live" if live else "demo",
        note=("live credentialed: EC billed by symbol/compute, surfaced per call"
              if live else "demo offline snapshot: no EC meter; live bills by symbol/compute"),
    )
    collected: dict[str, tuple[str, tuple[list[pd.DataFrame], int, float | None, list[str]]]] = {}
    batch_size = effective_workers
    idx = 0
    stopped_at: int | None = None

    def _run(symbol: str):
        wc = c_main if batch_size == 1 else _client(live)
        instrument = normalize.to_qlib_instrument(symbol, instrument_map)
        return symbol, instrument, _fetch_one_symbol(
            wc, symbol, instrument, fields, n, live
        )

    def _absorb(symbol: str, instrument: str, payload) -> None:
        frames, reqs, ec, warns = payload
        meter.requests += reqs
        if ec is not None:
            meter.ec_cost = (meter.ec_cost or 0.0) + ec
        warnings.extend(warns)
        collected[symbol] = (instrument, payload)
        if on_symbol is not None:
            on_symbol(symbol, instrument, frames, ec)

    # Per-symbol EC upper bound: unknown until the first (probe) symbol finishes.
    # Only ever increases, so a later batch can never be under-reserved.
    per_symbol_ub: float | None = None

    def _affordable() -> int:
        """How many symbols may we still submit before hitting max_ec?"""
        if max_ec is None:
            return len(symbols) - idx
        spent = meter.ec_cost or 0.0
        left = max_ec - spent
        if left <= 0:
            return 0
        if per_symbol_ub is None or per_symbol_ub <= 0:
            return 1  # probe: cost unknowable before the first call
        return max(0, int(left // per_symbol_ub))

    if batch_size == 1:
        while idx < len(symbols):
            if _affordable() < 1:
                stopped_at = idx
                break
            symbol = symbols[idx]
            _, instrument, payload = _run(symbol)
            _absorb(symbol, instrument, payload)
            ec = payload[2]
            if ec is not None:
                per_symbol_ub = ec if per_symbol_ub is None else max(per_symbol_ub, ec)
            idx += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            while idx < len(symbols):
                # NOTE: named batch_n, NOT n - ``n`` is the requested rows per
                # symbol read by the _run closure. Reassigning n here (the
                # original P0-1 bug) silently shrank every concurrent fetch to
                # n=batch_size rows while valuation 3y series kept the row
                # count looking plausible.
                batch_n = min(batch_size, _affordable())
                if batch_n < 1:
                    stopped_at = idx
                    break
                batch = symbols[idx:idx + batch_n]
                futures = {pool.submit(_run, s): s for s in batch}
                for fut in as_completed(futures):
                    symbol, instrument, payload = fut.result()
                    _absorb(symbol, instrument, payload)
                    ec = payload[2]
                    if ec is not None:
                        per_symbol_ub = ec if per_symbol_ub is None else max(per_symbol_ub, ec)
                idx += len(batch)

    if stopped_at is not None:
        remaining = len(symbols) - stopped_at
        if remaining > 0:
            warnings.append(
                f"EC hard ceiling {max_ec}: stopped after {stopped_at} symbol(s) "
                f"at {meter.ec_cost:.1f} EC; {remaining} symbol(s) not submitted "
                f"(per-symbol upper bound={per_symbol_ub})"
            )

    frames_all: list[pd.DataFrame] = []
    for symbol in symbols:  # original order -> deterministic concat
        if symbol in collected:
            frames_all.extend(collected[symbol][1][0])

    panel = pd.concat(frames_all, ignore_index=True) if frames_all else pd.DataFrame(
        columns=["date", "symbol", "instrument", "field", "value"]
    )
    if not panel.empty:
        panel["value"] = panel["value"].astype(float)
    return FetchResult(frame=panel, metering=meter, warnings=list(dict.fromkeys(warnings)))


def fetch_cross_section(
    *,
    live: bool = False,
    as_of: str | None = None,
    client: Any | None = None,
    metric_codes: tuple[Any, ...] = config.CROSS_SECTION_METRICS,
    universe: Iterable[str] | None = None,
    max_ec: float | None = None,
    where: str | None = None,
    idx: str | None = None,
) -> FetchResult:
    """Fetch today's cross-sectional scores.

    LIVE path: screener.query(fields=[codes], ...) -> wide frame -> composite score.

    EC MODEL (final calibration 2026-11-17, three measured anchors):
    billing DOES scale with the scanned/returned universe - the SDK's
    "not per-symbol" note is about request count, not EC:
      * full A-share universe, 4 light columns, 5,617 rows -> 204,430 EC
        (~9 EC/row/column);
      * same columns, limit=10 probe -> 349 EC (probes DO NOT predict
        full-universe cost);
      * computed columns ($PCT_*_10Y) -> ~110 EC/row (621,399 EC full universe).
    SUSTAINABILITY: a full-market daily score is ~200k EC/day - NOT viable.
    Use ``idx`` (e.g. "index.hs300", ~300 names) for daily accumulation;
    cost scales down with the index size. ``max_ec`` is REQUIRED for live:
    materialization cannot be pre-stopped, so this is a post-hoc ceiling +
    pre-flight budget ack. Stock-domain pre-materialized codes only
    (see config.CROSS_SECTION_METRICS); unknown codes drop with a warning.

    DEMO path: demo has no full-market index (its screener.query honestly
    raises demo_unsupported), so use kline.batch_live snapshot fields to build
    an equivalent score frame offline; marked mode='demo', NOT real metrics.
    """
    # EC budget gate: screener.query bills per scanned/returned row per column
    # (full market x 4 light columns = ~204k EC; an hs300 subset ~11k). A
    # limit=10 probe does NOT predict full-universe cost. Budget required
    # fail-loud; the ceiling is checked post-hoc (cost is paid once the
    # response returns) via client.last_ec_cost.
    if live and max_ec is None:
        raise normalize.NormalizeError(
            "live cross-section requires an explicit max_ec: screener.query "
            "bills column materialization in one request (light columns "
            "70~500 EC, heavy ~48k EC measured); the cost is only knowable "
            "once paid, so set a budget before firing"
        )
    c = client if client is not None else _client(
        live, max_limit=10000 if live else None, timeout=120 if live else None
    )
    day = as_of or dt.date.today().isoformat()
    meter = Metering(
        mode="live" if live else "demo",
        note=("live credentialed: screener.query bills column materialization "
              "(measured 884 EC light / 621,399 EC with 10y-percentile columns; "
              "cost is paid once the response returns)"
              if live else "demo offline snapshot: no EC meter"),
    )
    warnings: list[str] = []

    if live:
        codes = [m.code for m in metric_codes]
        where_cond = where or config.CROSS_SECTION_WHERE
        # ONE request at the 10000-row hard cap - the universe (~5.7k) fits,
        # and offset pagination would re-materialize every column per page.
        # Never paginate this path.
        sdf = c.screener.query(
            where=where_cond, fields=codes, limit=10000, offset=0,
            idx=idx, _df=True
        )
        meter.requests += 1
        # EC accounting: the gateway surfaces cost via client.last_ec_cost
        # (response attrs do NOT carry it). This is the post-hoc ceiling's
        # EYES - without it max_ec is blind (measured: 621,399 EC slipped a
        # 3,000 ceiling on 2026-11-17 exactly because of this read miss).
        ec = getattr(sdf, "attrs", {}).get("ec_cost")
        if ec is None:
            ec = getattr(c, "last_ec_cost", None)
        if ec is not None:
            meter.ec_cost = float(ec)
        if max_ec is not None and (meter.ec_cost or 0.0) > max_ec:
            raise normalize.NormalizeError(
                f"cross-section EC ceiling {max_ec} exceeded: one request "
                f"cost {meter.ec_cost:.1f} EC (materialization is paid once "
                f"the response returns; rows returned: {len(sdf)})"
            )
        present = set(sdf.columns) if hasattr(sdf, "columns") else set()
        for code in codes:
            if code not in present:
                warnings.append(f"metric code {code} absent from screener output (dropped)")
        # Instrument normalization (bug fixed 2026-11-17): screener symbols are
        # BARE 6-digit codes; Qlib needs <SUFFIX><code>. Map via the exchange
        # field (never infer from digit ranges - wrong for BJ/SZ edge cases);
        # unmapped codes are dropped with a warning, never passed through raw
        # (a bare code silently poisons ScoreFileModel/Alpha instrument keys).
        instrument_map = normalize.build_instrument_map(c)
        meter.requests += 1  # stocks.list
        unmapped = [s for s in sdf["symbol"].astype(str) if s not in instrument_map]
        if unmapped:
            warnings.append(
                f"{len(unmapped)} symbol(s) not in stocks.list (dropped, e.g. "
                f"{unmapped[:5]})"
            )
        sdf = sdf[sdf["symbol"].astype(str).isin(instrument_map)].copy()
        sdf["instrument"] = sdf["symbol"].astype(str).map(instrument_map)
        frame = _score_from_wide(sdf, metric_codes, day, warnings=warnings)
    else:
        instrument_map = normalize.build_instrument_map(c)
        syms = list(universe) if universe is not None else list(instrument_map)[:20]
        snap = c.kline.batch_live(syms)
        meter.requests += 1
        rows = []
        n_skipped = 0
        for sym, rec in snap.items():
            if not isinstance(rec, dict):
                n_skipped += 1
                continue
            raw = rec.get("chg_20d")
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                n_skipped += 1
                continue
            # Use snapshot momentum as a stand-in score purely to test the pipeline.
            rows.append((instrument_map.get(sym, sym), day, float(raw)))
        if n_skipped:
            warnings.append(
                f"demo snapshot score: skipped {n_skipped} symbol(s) with missing "
                f"chg_20d (never filled 0)"
            )
        frame = pd.DataFrame(rows, columns=["instrument", "datetime", "score"])
        warnings.append(
            "demo snapshot score uses chg_20d as stand-in; NOT real precomputed metrics"
        )

    return FetchResult(frame=frame, metering=meter, warnings=warnings)


def _score_from_wide(
    wide: pd.DataFrame, metrics: tuple[Any, ...], day: str,
    warnings: list[str] | None = None,
) -> pd.DataFrame:
    """Composite z-score of selected metrics -> ScoreFileModel input frame.

    ScoreFileModel reads index=[instrument, datetime] with a 'score' column.
    Direction is applied per metric (lower_better flips sign).

    Anti-pitfall #5: rows with a missing metric value are DROPPED and counted
    in ``warnings`` - NEVER filled with 0. A 0 z-score pretends "exactly
    average" and silently distorts the cross-sectional ranking.
    """
    df = wide.copy()
    used = [m.code for m in metrics if m.code in df.columns]
    if not used:
        raise normalize.NormalizeError("no selected metric codes present in screener output")
    for c0 in used:
        df[c0] = pd.to_numeric(df[c0], errors="coerce")
    valid = df[used].notna().all(axis=1)
    n_dropped = int((~valid).sum())
    if n_dropped:
        if warnings is not None:
            warnings.append(
                f"score: dropped {n_dropped}/{len(df)} rows with missing metric "
                f"values (never filled 0)"
            )
        df = df.loc[valid]
    if df.empty:
        raise normalize.NormalizeError(
            "all rows dropped for missing metric values; no score can be produced"
        )
    score = pd.Series(0.0, index=df.index)
    for m in metrics:
        if m.code not in df.columns:
            continue
        col = df[m.code]
        z = (col - col.mean()) / (col.std(ddof=0) or 1.0)
        if m.direction == "lower_better":
            z = -z
        score = score.add(z, fill_value=0.0)
    out = pd.DataFrame({"score": score})
    # Prefer the mapped Qlib instrument column; fall back to raw symbol only
    # in paths that never had a map (kept for the demo/stub contract tests).
    if "instrument" in df.columns:
        sym_col = "instrument"
    else:
        sym_col = "symbol" if "symbol" in df.columns else df.columns[0]
    out["instrument"] = df[sym_col].astype(str)
    out["datetime"] = day
    return out[["instrument", "datetime", "score"]]
