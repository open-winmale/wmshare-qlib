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
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

from . import config, normalize

_sleep = time.sleep  # module-level so tests can inject the clock

# P1-3 retry policy: per-symbol HISTORY calls get one bounded retry on
# TRANSIENT failures. Transient = TransportError (network/timeout),
# ServerError (5xx), or a rate-limit LimitError (status 429, honoring
# retry_after). NEVER retried: QuotaError (exhausted quota - retrying is
# pointless; it subclasses LimitError so must be excluded explicitly),
# AuthError / UsageError / ApiError / DataError (permanent), and client-side
# LimitError (code="limit"/"range" - our own parameter mistakes).
# The big one-shot queries (screener.query, kline.snapshot, batch_live) are
# deliberately OUT of scope: a timeout mid-materialization must not
# double-bill - the --max-ec ceiling is their only guard.
RETRY_ATTEMPTS = 2
RETRY_BASE_DELAY = 0.5


def _is_transient(exc: Exception) -> bool:
    try:
        from wmshare import errors as _werr
    except ImportError:
        return False  # SDK absent: nothing is retryable
    if isinstance(exc, _werr.QuotaError):
        return False
    if isinstance(exc, (_werr.TransportError, _werr.ServerError)):
        return True
    if isinstance(exc, _werr.LimitError) and getattr(exc, "status", None) == 429:
        return True
    return False


def call_with_retry(
    fn: Callable[[], Any],
    *,
    label: str,
    meter_cb: Callable[[], None],
    warn_cb: Callable[[str], None],
    attempts: int = RETRY_ATTEMPTS,
) -> Any:
    """One SDK call with per-attempt metering and a bounded transient retry.

    EC transparency: ``meter_cb`` fires after EVERY attempt - a failed attempt
    may still have billed, and pretending otherwise is exactly the class of
    silent loss the roadmap forbids. Retries are surfaced as warnings, never
    silent. ``attempts`` counts logical calls; the caller's ``requests``
    counter stays per logical call (EC is the billing unit, not requests).
    """
    for i in range(max(1, attempts)):
        try:
            result = fn()
            meter_cb()
            return result
        except Exception as exc:
            meter_cb()
            if i + 1 >= max(1, attempts) or not _is_transient(exc):
                raise
            delay = RETRY_BASE_DELAY * (2 ** i)
            ra = getattr(exc, "retry_after", None)
            if ra:
                delay = max(delay, float(ra))
            warn_cb(
                f"{label}: transient {type(exc).__name__} "
                f"(attempt {i + 1}/{attempts}), retrying in {delay:.2f}s"
            )
            _sleep(delay + random.uniform(0.0, 0.25))
    raise AssertionError("unreachable")  # pragma: no cover


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


def _valuation_frames(
    c: Any, symbol: str, instrument: str, fields: tuple[str, ...], meter_cb: Callable[[], None]
) -> tuple[list[pd.DataFrame], list[str], int]:
    """Fetch the pe/pb/ps/pr 3y series (shared by bulk and tail paths).

    Returns ``(frames, warnings, requests_used)``. Demo snapshot only stores
    pe/3y; unsupported metrics are skipped with an explicit warning (cold-cache
    guard: never fabricate, never fill 0). EC metering goes through the
    caller's ``meter_cb`` so per-path accounting stays exact.
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    requests = 0
    for vfield in config.VALUATION_FIELDS:
        if vfield not in fields:
            continue
        try:
            payload = call_with_retry(
                lambda vf=vfield: c.valuation.hist_stats(symbol, metric=vf, window="3y"),
                label=f"{symbol}/{vfield}", meter_cb=meter_cb, warn_cb=warnings.append,
            )
        except Exception as exc:  # wmshare.errors.UsageError and friends
            requests += 1
            warnings.append(
                f"{symbol}/{vfield}: skipped ({type(exc).__name__}: "
                f"{str(exc)[:60]})"
            )
            continue
        requests += 1
        if normalize.annotate_missing(payload):
            warnings.append(f"{symbol}/{vfield}: ok=False skipped (cold cache)")
            continue
        env_data = payload.get("data")
        env_series = env_data.get("series") if isinstance(env_data, dict) else None
        n_series = 0 if env_series is None else len(env_series)
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
    return frames, warnings, requests


def _daily_basic_frames(
    c: Any, symbol: str, instrument: str, fields: tuple[str, ...], meter_cb: Callable[[], None],
    *, begin: str | None = None,
) -> tuple[list[pd.DataFrame], list[str], int]:
    """Fetch the materialized daily_basic series (upstream-gated, plan v3 §8).

    Requests ONLY the panel-extension fields (config.PANEL_DAILY_BASIC_FIELDS);
    daily_basic's pb/ps/pr duplicate the hist_stats series by design and are
    left to the reconcile gate, never double-written. The endpoint may be
    absent upstream (demo_unsupported / CatalogError until the server table
    ships) - that is an explicit SKIP warning, never an error and never a
    silent omission: the .bin panel stays exactly as before.
    """
    want = tuple(f for f in config.PANEL_DAILY_BASIC_FIELDS if f in fields)
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    requests = 0
    if not want:
        return frames, warnings, requests
    try:
        payload = call_with_retry(
            lambda: c.valuation.daily_basic(symbol, begin=begin or "", fields=list(want)),
            label=f"{symbol}/daily_basic", meter_cb=meter_cb, warn_cb=warnings.append,
        )
    except Exception as exc:  # endpoint not live yet / demo_unsupported: honest skip
        requests += 1
        warnings.append(
            f"{symbol}/daily_basic: unavailable, skipped ({type(exc).__name__}: "
            f"{str(exc)[:60]})"
        )
        return frames, warnings, requests
    requests += 1
    try:
        df = normalize.daily_basic_to_frame(payload, symbol=symbol)
    except normalize.NormalizeError as exc:
        warnings.append(f"{symbol}/daily_basic: malformed envelope skipped ({exc})")
        return frames, warnings, requests
    if df.empty:
        warnings.append(f"{symbol}/daily_basic: empty series skipped (no rows upstream)")
        return frames, warnings, requests
    df = df[df["field"].isin(want)]
    df["instrument"] = instrument
    frames.append(df[["date", "symbol", "instrument", "field", "value"]])
    return frames, warnings, requests


def _fetch_one_symbol(
    c: Any,
    symbol: str,
    instrument: str,
    fields: tuple[str, ...],
    n: int,
    live: bool,
    adjust_mode: str = "qfq_const",
) -> tuple[list[pd.DataFrame], int, float | None, list[str]]:
    """Fetch ALL history fields for one symbol via one dedicated client.

    Returns ``(frames, requests, ec_cost, warnings)``. ``ec_cost`` is None in
    demo (no ``last_ec_cost`` attribute) and the SUM of per-call EC in live.
    Owning the client per call keeps ``last_ec_cost`` free of write races when
    the caller runs several symbols concurrently.

    ``adjust_mode``: 'hfq_factor' (live default) pulls kline TWICE
    (``adj="hfq"`` + raw) and emits factor = hfq/raw with volume/factor
    (Qlib yahoo-collector convention, see normalize.merge_adjusted_klines);
    'qfq_const' keeps the v1 single-call path with factor=1.0. Demo only
    supports qfq_const (upstream rejects adj=).
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
    if live and adjust_mode == "hfq_factor":
        # Dual pull: back-adjusted + raw. Same n, deterministic EC (156+67n
        # each, measured 2026-11-17). Dates must align 1:1 (probed: they do);
        # a mismatch raises inside the merge - never merge on a best-effort
        # subset.
        kadj = call_with_retry(
            lambda: c.kline.bars(symbol, n=n, adj="hfq"),
            label=f"{symbol}/kline(adj)", meter_cb=_meter, warn_cb=warnings.append,
        )
        requests += 1
        kraw = call_with_retry(
            lambda: c.kline.bars(symbol, n=n, adj=None),
            label=f"{symbol}/kline(raw)", meter_cb=_meter, warn_cb=warnings.append,
        )
        requests += 1
        n_adj_raw, n_raw = len(kadj), len(kraw)
        adj_df = normalize.kline_to_frame(kadj, symbol=symbol)
        raw_df = normalize.kline_to_frame(kraw, symbol=symbol)
        kdf = normalize.merge_adjusted_klines(adj_df, raw_df, symbol=symbol)
        dropped = (n_adj_raw - len(adj_df)) + (n_raw - len(raw_df))
        if dropped:
            warnings.append(
                f"{symbol}/kline: upstream returned {dropped} duplicated "
                f"date row(s) with identical values; deduplicated "
                f"(conflicting duplicates would have raised)"
            )
    else:
        if live:
            # Explicit qfq_const on live: single pull, factor=1.0 (v1 behaviour).
            kraw = call_with_retry(
                lambda: c.kline.bars(symbol, n=n, adj="qfq"),
                label=f"{symbol}/kline(qfq)", meter_cb=_meter, warn_cb=warnings.append,
            )
        else:
            # demo supports n<=120, no adj, no begin/end
            kraw = call_with_retry(
                lambda: c.kline.bars(symbol, n=min(n, 120)),
                label=f"{symbol}/kline(demo)", meter_cb=_meter, warn_cb=warnings.append,
            )
        requests += 1
        n_raw = len(kraw)
        kdf = normalize.kline_to_frame(kraw, symbol=symbol)
        if len(kdf) < n_raw:
            warnings.append(
                f"{symbol}/kline: upstream returned {n_raw - len(kdf)} duplicated "
                f"date row(s) with identical values; deduplicated "
                f"(conflicting duplicates would have raised)"
            )
        # factor is emitted explicitly on every path (never silently omitted).
        # qfq_const: prices are qfq, the declared convention says factor=1.0.
        if "factor" not in kdf.columns:
            kdf["factor"] = 1.0
    kdf["instrument"] = instrument
    keep = [c0 for c0 in ("date", "symbol", "instrument", *config.BASE_FIELDS) if c0 in kdf.columns]
    frames.append(kdf[keep].melt(
        id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
    ))

    # --- valuation (pe/pb/ps/pr) ---
    vframes, vwarnings, vrequests = _valuation_frames(
        c, symbol, instrument, fields, _meter
    )
    requests += vrequests
    warnings.extend(vwarnings)
    frames.extend(vframes)

    # --- daily_basic (upstream-gated: materialized wide table, plan v3) ---
    dbframes, dbwarnings, dbrequests = _daily_basic_frames(
        c, symbol, instrument, fields, _meter
    )
    requests += dbrequests
    warnings.extend(dbwarnings)
    frames.extend(dbframes)

    # --- moneyflow ---
    if any(f in fields for f in config.MONEYFLOW_FIELDS):
        # demo snapshot keeps at most 30 bars; live has no such cap.
        mf_n = n if live else min(n, 30)
        try:
            mraw = call_with_retry(
                lambda: c.moneyflow.bars(symbol, n=mf_n),
                label=f"{symbol}/moneyflow", meter_cb=_meter, warn_cb=warnings.append,
            )
        except Exception as exc:
            requests += 1
            warnings.append(f"{symbol}/moneyflow: skipped ({type(exc).__name__}: {str(exc)[:60]})")
            mraw = None
        if mraw is not None:
            requests += 1
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


def _execute_per_symbol(
    symbols: list[str],
    *,
    run_fn: Callable[[str], tuple[str, str, tuple]],
    absorb_fn: Callable[[str, str, tuple], None],
    effective_workers: int,
    max_ec: float | None,
    meter: Metering,
    warnings: list[str],
) -> int | None:
    """Generic per-symbol executor: serial or pooled, with the EC hard ceiling.

    Shared by fetch_history_panel (bulk) and fetch_history_tail (incremental)
    so the probe -> upper-bound -> batch-sizing discipline exists exactly once
    (P0-1 extraction; behaviour preserved verbatim - the panel test matrix is
    the regression net). ``run_fn(symbol)`` returns ``(symbol, instrument,
    payload)`` where payload is ``(frames, requests, ec, warnings)``;
    ``absorb_fn`` books the payload (metering, warnings, caller-side storage).

    Returns ``stopped_at`` (index into symbols) when the ceiling stopped the
    run early, else None.
    """
    batch_size = effective_workers
    idx = 0
    stopped_at: int | None = None
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
            symbol, instrument, payload = run_fn(symbols[idx])
            absorb_fn(symbol, instrument, payload)
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
                futures = {pool.submit(run_fn, s): s for s in batch}
                for fut in as_completed(futures):
                    symbol, instrument, payload = fut.result()
                    absorb_fn(symbol, instrument, payload)
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
    return stopped_at


def fetch_history_panel(
    symbols: Iterable[str],
    *,
    live: bool = False,
    fields: tuple[str, ...] = config.HISTORY_FIELDS,
    n: int = 120,
    client: Any | None = None,
    max_workers: int = 1,
    max_ec: float | None = None,
    adjust_mode: str | None = None,
    max_limit: int | None = None,
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

    P0-2' addition:
      * ``adjust_mode``: None -> auto ('hfq_factor' live / 'qfq_const' demo,
        see config.adjustment_mode). hfq_factor doubles the kline calls per
        symbol (adj + raw) so kline EC doubles; the emitted ``factor`` column
        is the real cumulative adjustment and volume is divided by it.

    P1-2 addition (deep history):
      * ``max_limit``: raises the client's per-call served-bar cap (default
        500) so a single call can serve n > 500 bars. n-mode has NO backward
        paging (every call returns the most recent n bars), so deep history
        MUST be one call with a raised cap - range queries are falsified
        (they force-pay max_limit bars regardless of window). Passed to both
        the main and per-worker clients; ignored when ``client`` is supplied.

    Anti-pitfall #5: symbols with cold-cache / short window are skipped and
    listed in warnings, never filled with zeros.
    """
    adjust_mode = config.adjustment_mode(live, adjust_mode)
    c_main = client if client is not None else _client(live, max_limit=max_limit)
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
        note=(
            f"live credentialed: EC billed by symbol/compute, surfaced per call; "
            f"adjust_mode={adjust_mode}"
            + (" (kline pulled twice: adj + raw)" if adjust_mode == "hfq_factor" else "")
            if live
            else "demo offline snapshot: no EC meter; live bills by symbol/compute"
        ),
    )
    collected: dict[str, tuple[str, tuple[list[pd.DataFrame], int, float | None, list[str]]]] = {}

    def _run(symbol: str):
        wc = (c_main if effective_workers == 1
              else _client(live, max_limit=max_limit))
        instrument = normalize.to_qlib_instrument(symbol, instrument_map)
        return symbol, instrument, _fetch_one_symbol(
            wc, symbol, instrument, fields, n, live, adjust_mode
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
    # (Executor extracted in P0-1 so the tail fetch shares the exact discipline.)
    _execute_per_symbol(
        symbols,
        run_fn=_run,
        absorb_fn=_absorb,
        effective_workers=effective_workers,
        max_ec=max_ec,
        meter=meter,
        warnings=warnings,
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


def _fetch_tail_one_symbol(
    c: Any,
    symbol: str,
    instrument: str,
    k: int,
    want_valuation: bool,
    fields: tuple[str, ...],
) -> tuple[list[pd.DataFrame], int, float | None, list[str]]:
    """Incremental tail for one symbol (P0-1): dual kline pull + moneyflow,
    valuation only when its cadence is due.

    The dual pull (adj + raw) is REQUIRED even for the tail:
    ``kline.batch_live`` serves the raw live price with UNVERIFIED ex-div
    semantics (its ``percent`` basis on ex-div days cannot be probed on
    demand), while ``bars(n=k)`` returns hfq AND raw AND the exact factor in
    one merge. Deterministic pricing: 156 + 67k EC per call (roadmap §2.5);
    k = gap_days + overlap, so a daily run costs ~1.1k EC/symbol at
    overlap=5 - still ~6.5x cheaper than the full-bundle re-pull.
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

    # --- kline tail (hfq + raw) ---
    kadj = call_with_retry(
        lambda: c.kline.bars(symbol, n=k, adj="hfq"),
        label=f"{symbol}/kline-tail(adj)", meter_cb=_meter, warn_cb=warnings.append,
    )
    requests += 1
    kraw = call_with_retry(
        lambda: c.kline.bars(symbol, n=k, adj=None),
        label=f"{symbol}/kline-tail(raw)", meter_cb=_meter, warn_cb=warnings.append,
    )
    requests += 1
    n_adj_raw, n_raw = len(kadj), len(kraw)
    adj_df = normalize.kline_to_frame(kadj, symbol=symbol)
    raw_df = normalize.kline_to_frame(kraw, symbol=symbol)
    kdf = normalize.merge_adjusted_klines(adj_df, raw_df, symbol=symbol)
    dropped = (n_adj_raw - len(adj_df)) + (n_raw - len(raw_df))
    if dropped:
        warnings.append(
            f"{symbol}/kline-tail: upstream returned {dropped} duplicated "
            f"date row(s) with identical values; deduplicated"
        )
    kdf["instrument"] = instrument
    keep = [c0 for c0 in ("date", "symbol", "instrument", *config.BASE_FIELDS)
            if c0 in kdf.columns]
    frames.append(kdf[keep].melt(
        id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
    ))

    # --- moneyflow tail ---
    if any(f in fields for f in config.MONEYFLOW_FIELDS):
        mraw = call_with_retry(
            lambda: c.moneyflow.bars(symbol, n=k),
            label=f"{symbol}/moneyflow-tail", meter_cb=_meter, warn_cb=warnings.append,
        )
        requests += 1
        n_mf_raw = len(mraw)
        mdf = normalize.moneyflow_to_frame(mraw, symbol=symbol)
        if len(mdf) < n_mf_raw:
            warnings.append(
                f"{symbol}/moneyflow-tail: upstream returned {n_mf_raw - len(mdf)} "
                f"duplicated date row(s) with identical values; deduplicated"
            )
        mdf["instrument"] = instrument
        mf_keep = [c0 for c0 in ("date", "symbol", "instrument", *config.MONEYFLOW_FIELDS)
                   if c0 in mdf.columns]
        frames.append(mdf[mf_keep].melt(
            id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
        ))

    # --- valuation (cadence-gated: the 3y window re-pulls the whole series,
    # which the bulk/tail reconciliation then verifies row-by-row) ---
    if want_valuation:
        vframes, vwarnings, vrequests = _valuation_frames(
            c, symbol, instrument, fields, _meter
        )
        requests += vrequests
        warnings.extend(vwarnings)
        frames.extend(vframes)

    # --- daily_basic tail (cached_eval read; unavailable upstream = skip) ---
    dbframes, dbwarnings, dbrequests = _daily_basic_frames(
        c, symbol, instrument, fields, _meter
    )
    requests += dbrequests
    warnings.extend(dbwarnings)
    frames.extend(dbframes)

    return frames, requests, (ec_total if has_meter else None), warnings


def fetch_history_tail(
    symbols: Iterable[str],
    *,
    last_dates: dict[str, str | None],
    valuation_last_dates: dict[str, str | None] | None = None,
    live: bool = True,
    today: str | None = None,
    overlap: int = 5,
    tail_max_n: int = 30,
    valuation_days: int = 7,
    fields: tuple[str, ...] = config.HISTORY_FIELDS,
    client: Any | None = None,
    max_workers: int = 1,
    max_ec: float | None = None,
    on_symbol: Callable[[str, str, list[pd.DataFrame], float | None], None] | None = None,
) -> FetchResult:
    """Fetch incremental tails for symbols that already have bulk fragments.

    P0-1 incremental mode (fetch side). Per-symbol plan:
      * gap = calendar days between ``today`` and the symbol's last stored bar;
        gap <= 0 -> skip (already current, ZERO EC);
      * k = gap + ``overlap`` bars requested (overlap bridges non-trading days
        between the stored tail and now, and forces row-level reconciliation
        against the stored series: immutable history must agree - see
        resume.load_panel_with_tail);
      * k > ``tail_max_n`` -> skip loud: the tail stops being cheaper than a
        full re-pull, so the caller is told to bulk-refetch the symbol;
      * valuation refresh fires only when ``valuation_days`` elapsed since the
        symbol's last valuation snapshot.

    LIVE-ONLY by design: the demo snapshot has neither adj= nor bar windows
    (probe 2026-11-17), and an incremental mode cannot honestly fake either.
    EC ceiling and concurrency share the exact bulk executor discipline
    (``_execute_per_symbol``).
    """
    if not live:
        raise normalize.NormalizeError(
            "incremental tail fetch is live-only: the demo snapshot supports "
            "neither adj= nor n-window refetch, and an incremental mode cannot "
            "fake either (use dump-history for the demo full path)"
        )
    c_main = client if client is not None else _client(live)
    instrument_map = normalize.build_instrument_map(c_main)
    symbols = list(symbols)
    warnings: list[str] = []
    today_date = dt.date.fromisoformat(today) if today else dt.date.today()

    unknown = [s for s in symbols if s not in instrument_map]
    if unknown:
        raise normalize.NormalizeError(
            f"symbols outside universe: {unknown[:5]} (limit universe to stocks.list codes)"
        )

    # --- plan phase (pure bookkeeping, zero EC) ---
    plan: dict[str, tuple[str, int, bool]] = {}
    v_last_map = valuation_last_dates or {}
    for s in symbols:
        last = last_dates.get(s)
        if not last:
            warnings.append(
                f"{s}: skipped - no bulk fragment on record; run dump-history first"
            )
            continue
        gap = (today_date - dt.date.fromisoformat(str(last))).days
        if gap <= 0:
            warnings.append(f"{s}: up to date (last {last}), 0 EC spent")
            continue
        k = gap + overlap
        if k > tail_max_n:
            warnings.append(
                f"{s}: skipped - gap {gap}d needs k={k} > tail_max_n={tail_max_n}; "
                f"tail no longer cheaper than a full re-pull, bulk-refetch this symbol"
            )
            continue
        v_last = v_last_map.get(s)
        v_due = v_last is None or (
            (today_date - dt.date.fromisoformat(str(v_last))).days >= valuation_days
        )
        plan[s] = (normalize.to_qlib_instrument(s, instrument_map), k, v_due)

    meter = Metering(
        mode="live",
        note=("live incremental tail: kline adj+raw per symbol "
              "(156+67k EC each, deterministic), moneyflow tail, valuation "
              "on cadence; overlap rows reconcile against stored history"),
    )
    collected: dict[str, tuple[str, tuple[list[pd.DataFrame], int, float | None, list[str]]]] = {}

    def _run(symbol: str):
        wc = c_main if max_workers <= 1 else _client(live)
        instrument, k, v_due = plan[symbol]
        return symbol, instrument, _fetch_tail_one_symbol(
            wc, symbol, instrument, k, v_due, fields
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

    _execute_per_symbol(
        list(plan.keys()),
        run_fn=_run,
        absorb_fn=_absorb,
        effective_workers=max(1, int(max_workers)),
        max_ec=max_ec,
        meter=meter,
        warnings=warnings,
    )

    frames_all: list[pd.DataFrame] = []
    for s in symbols:  # original order -> deterministic concat
        if s in collected:
            frames_all.extend(collected[s][1][0])
    panel = pd.concat(frames_all, ignore_index=True) if frames_all else pd.DataFrame(
        columns=["date", "symbol", "instrument", "field", "value"]
    )
    if not panel.empty:
        panel["value"] = panel["value"].astype(float)
    return FetchResult(frame=panel, metering=meter, warnings=list(dict.fromkeys(warnings)))


def _supports_fields(client: Any) -> bool:
    """Whether the installed wmshare's snapshot facade accepts ``_fields=``.

    ``_fields`` is an underscore client option (client-side column projection,
    applied AFTER unit conversion) from the wmshare latest spec. wmshare's
    COMPATIBILITY.md explicitly places "_"-prefixed client options OUTSIDE the
    public-surface promise, so this adapter detects support from the signature
    instead of gating on a version (the in-flight SDK still reports
    ``__version__="2.1.0"``, so version gating would misfire both ways).
    Unsupported SDKs get the unprojected call - identical data, wider dicts.
    """
    try:
        import inspect

        return "_fields" in inspect.signature(client.kline.snapshot).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _snapshot_chunk_warnings(
    snap: Any, chunk: list[str], warnings: list[str], label: str
) -> None:
    """Surface silent ticket omissions from a native snapshot batch.

    ``BatchResult.truncated`` (wmshare latest): full-market shortcuts omit
    suspended/non-member tickets WITHOUT an error - before the flag existed a
    symbol missing from ``results`` simply never got scored/hygiene-checked,
    invisibly. Diffs the request against THIS chunk's results so per-chunk
    gaps stay attributable.
    """
    if not getattr(snap, "truncated", False):
        return
    got = getattr(snap, "results", None) or {}
    missing = [s for s in chunk if s not in got]
    if not missing:
        return  # flagged but fully served: nothing to report
    warnings.append(
        f"{label}: server silently omitted {len(missing)} ticket(s) "
        f"(truncated, e.g. {missing[:3]}) - results are a SUBSET of the request"
    )


def fetch_cross_section_snapshot(
    symbols: Iterable[str],
    *,
    live: bool = True,
    as_of: str | None = None,
    client: Any | None = None,
    metric_codes: tuple[Any, ...] = config.SNAPSHOT_METRICS,
    max_ec: float | None = None,
    exclude_st: bool = True,
    exclude_suspended: bool = True,
) -> FetchResult:
    """Cross-section score from the kline.snapshot BATCH (P0-3).

    Why this path: the server does not serve screener index subsets
    (`index.hs300` -> "not loadable", probe 2026-11-17) and screener.query
    bills the SCANNED universe, so the scan size cannot be narrowed
    client-side. ``kline.snapshot`` accepts an explicit symbol list and bills
    per ticket (~100 EC/票 at N=3, ~58 claimed at scale) - the only path that
    makes an hs300-sized DAILY cross-section sustainable (~17-30k EC/day for
    ~300 names vs ~204k for a full-market screener query).

    Score family: TECHNICAL COMPOSITE (see config.SNAPSHOT_METRICS) - declared
    directions, z-score composite. NOT the screener fundamentals family;
    the metering note labels it so the score meta never blurs the two.

    Guards:
      * LIVE-only (demo snapshot has no snapshot-batch endpoint);
      * >= 3 symbols (the SDK's native batch route); fewer would silently
        fall back to per-ticket evaluation at 10,250 EC each;
      * universe intersected with stocks.list (66 EC) - unmapped codes are
        dropped with a warning, never scored under a bare code;
      * ST / suspended rows are DROPPED with a warning (universe hygiene,
        anti-pitfall #5: never scored, never filled);
      * rows missing any metric value are DROPPED with a warning
        (anti-pitfall #5: a 0 z-score pretends "exactly average");
      * silent server-side omissions (``BatchResult.truncated``) are reported
        as a warning - a missing ticket is never silently scored-over;
      * ``max_ec`` REQUIRED (pre-flight ack), enforced post-hoc via
        ``client.last_ec_cost`` - the batch result itself does not carry EC.
    """
    if not live:
        raise normalize.NormalizeError(
            "snapshot cross-section is live-only (the demo snapshot has no "
            "snapshot-batch endpoint); the demo score path is fetch_cross_section"
        )
    if max_ec is None:
        raise normalize.NormalizeError(
            "snapshot cross-section requires an explicit max_ec: the batch "
            "bills per ticket (measured ~100 EC/票 at N=3) and out-of-index "
            "tickets would fall back to per-ticket evaluation (~10k EC each); "
            "set the budget before firing"
        )
    symbols = list(symbols)
    if len(symbols) < 3:
        raise normalize.NormalizeError(
            f"snapshot batch needs >= 3 symbols (got {len(symbols)}): fewer "
            f"falls back to per-ticket evaluation at ~10,250 EC each"
        )
    c = client if client is not None else _client(live=True)
    day = as_of or dt.date.today().isoformat()
    meter = Metering(
        mode="live",
        note=("live snapshot-batch cross-section: TECHNICAL COMPOSITE family "
              "(config.SNAPSHOT_METRICS), per-ticket billing (~100 EC/票 at N=3), "
              "ST/suspended and missing-metric rows dropped, never filled"),
    )
    warnings: list[str] = []

    instrument_map = normalize.build_instrument_map(c)  # stocks.list: 66 EC
    meter.requests += 1
    unmapped = [s for s in symbols if s not in instrument_map]
    if unmapped:
        warnings.append(
            f"{len(unmapped)} symbol(s) not in stocks.list (dropped, e.g. "
            f"{unmapped[:5]}) - universe must be equity codes"
        )
    universe = [s for s in symbols if s in instrument_map]
    if not universe:
        raise normalize.NormalizeError("no scorable symbols left in universe")

    # Client-side chunking (config.SNAPSHOT_BATCH_CHUNK, measured 2026-11-17:
    # ~81-ticket single batches fail server-side, <=30-ticket chunks succeed).
    # EC is the SUM of per-chunk last_ec_cost readings (per-call semantics,
    # same pattern as the per-symbol meter).
    #
    # _fields adoption (wmshare latest spec, signature-gated - see
    # _supports_fields): projects columns CLIENT-side after unit conversion.
    # It does NOT lower EC (wmshare bills scanned materialized rows; only the
    # response body narrows). The field set is a SUPERSET of everything the
    # downstream loop touches (is_st/is_suspend + every metric code): the SDK
    # projection raises UsageError on unknown fields, so omitting a needed
    # column would fail loud - requesting extras is the safe direction.
    fields_want = sorted({"is_st", "is_suspend"} | {m.code for m in metric_codes})
    use_fields = _supports_fields(c)
    results: dict[str, dict] = {}
    n_requests = 0
    ec_total = 0.0
    has_meter = False
    for i in range(0, len(universe), config.SNAPSHOT_BATCH_CHUNK):
        chunk = universe[i:i + config.SNAPSHOT_BATCH_CHUNK]
        snap = (c.kline.snapshot(chunk, _fields=fields_want)
                if use_fields else c.kline.snapshot(chunk))
        n_requests += int(getattr(snap, "requests", 1) or 1)
        results.update(getattr(snap, "results", None) or {})
        _snapshot_chunk_warnings(
            snap, chunk, warnings,
            label=f"chunk {i // config.SNAPSHOT_BATCH_CHUNK}")
        errs = getattr(snap, "errors", None) or {}
        if errs:
            warnings.append(f"{len(errs)} ticket(s) errored in chunk "
                            f"{i // config.SNAPSHOT_BATCH_CHUNK} (dropped, "
                            f"e.g. {list(errs)[:3]})")
        v = getattr(c, "last_ec_cost", None)
        if v is not None:
            ec_total += float(v)
            has_meter = True
    meter.requests += n_requests
    meter.ec_cost = ec_total if has_meter else None
    if (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"snapshot cross-section EC ceiling {max_ec} exceeded: chunks cost "
            f"{meter.ec_cost:.1f} EC for {len(universe)} tickets - check the "
            f"universe for out-of-index codes (per-ticket fallback ~10k EC each)"
        )
    results = results or {}
    errs = getattr(snap, "errors", None) or {}
    if errs:
        warnings.append(f"{len(errs)} ticket(s) errored in the batch (dropped, "
                        f"e.g. {list(errs)[:3]})")

    rows = []
    n_st, n_susp = 0, 0
    for sym, fields in results.items():
        if not isinstance(fields, dict):
            continue
        if exclude_st and fields.get("is_st"):
            n_st += 1
            continue
        if exclude_suspended and fields.get("is_suspend"):
            n_susp += 1
            continue
        rows.append({"symbol": str(sym), **{k: v for k, v in fields.items()
                                            if k not in ("is_st", "is_suspend")}})
    if n_st:
        warnings.append(f"dropped {n_st} ST row(s) (universe hygiene, never scored)")
    if n_susp:
        warnings.append(f"dropped {n_susp} suspended row(s) (universe hygiene)")
    if not rows:
        raise normalize.NormalizeError("snapshot batch returned no scorable rows")

    wide = pd.DataFrame(rows)
    codes = [m.code for m in metric_codes]
    present = set(wide.columns)
    for code in codes:
        if code not in present:
            warnings.append(f"metric field {code} absent from snapshot output (dropped)")
    wide["instrument"] = wide["symbol"].astype(str).map(instrument_map)
    frame = _score_from_wide(wide, metric_codes, day, warnings=warnings)
    return FetchResult(frame=frame, metering=meter, warnings=warnings)


def fetch_index_members(client: Any, key: str) -> FetchResult:
    """Real index membership via ``indices.members`` (P1-1 universe source).

    LIVE-ONLY: the demo client has no ``indices`` namespace (verified
    offline). Cost is NOT yet probed (a pending roadmap item) - pass --max-ec
    at the CLI layer before firing. Returns bare codes; instrument mapping
    happens at the caller via ``stocks.list`` (66 EC).
    """
    try:
        raw = client.indices.members(key)
    except AttributeError as exc:
        raise normalize.NormalizeError(
            f"client has no indices namespace (demo snapshot is unsupported; "
            f"index membership requires a live client): {exc}"
        ) from exc
    meter = Metering(mode="live", note=f"live indices.members({key}); "
                                      "cost not yet probed (roadmap §5)")
    warnings: list[str] = []
    if isinstance(raw, pd.DataFrame):
        df = raw
    else:
        df = pd.DataFrame(raw)
    sym_col = next((c0 for c0 in ("symbol", "code", "sym") if c0 in df.columns),
                   df.columns[0])
    codes = df[sym_col].astype(str).tolist()
    ec = getattr(client, "last_ec_cost", None)
    if ec is not None:
        meter.ec_cost = float(ec)
    meter.requests = 1
    if not codes:
        raise normalize.NormalizeError(f"index {key!r} returned no members")
    return FetchResult(frame=pd.DataFrame({"symbol": codes}),
                       metering=meter, warnings=warnings)


def filter_universe_hygiene(
    symbols: Iterable[str],
    *,
    client: Any,
    exclude_st: bool = True,
    exclude_suspended: bool = True,
    max_ec: float | None = None,
) -> FetchResult:
    """Drop ST / suspended symbols from a universe via kline.snapshot (P1-1).

    LIVE-only hygiene pre-pass: ~100 EC/票 (plus the 66 EC stocks.list) buys
    the is_st/is_suspend flags BEFORE any expensive history fetch. Shares the
    snapshot-batch guards with the P0-3 cross-section: >= 3 symbols, universe
    intersected with stocks.list, --max-ec post-hoc ceiling.
    """
    if max_ec is None:
        raise normalize.NormalizeError(
            "universe hygiene requires an explicit max_ec (snapshot batch "
            "bills per ticket; out-of-index tickets fall back to ~10k EC each)"
        )
    symbols = list(symbols)
    if len(symbols) < 3:
        raise normalize.NormalizeError(
            f"snapshot batch needs >= 3 symbols (got {len(symbols)})"
        )
    instrument_map = normalize.build_instrument_map(client)
    universe = [s for s in symbols if s in instrument_map]
    dropped_unmapped = [s for s in symbols if s not in instrument_map]
    warnings: list[str] = []
    if dropped_unmapped:
        warnings.append(
            f"{len(dropped_unmapped)} symbol(s) not in stocks.list (dropped, "
            f"e.g. {dropped_unmapped[:5]})"
        )
    if not universe:
        raise normalize.NormalizeError("no hygiene-checkable symbols left")
    # _fields adoption (signature-gated, see _supports_fields): the hygiene
    # pass only touches is_st/is_suspend - projection narrows the response
    # body client-side, EC unchanged.
    if _supports_fields(client):
        snap = client.kline.snapshot(universe, _fields=["is_st", "is_suspend"])
    else:
        snap = client.kline.snapshot(universe)
    _snapshot_chunk_warnings(snap, list(universe), warnings, label="hygiene batch")
    ec = getattr(client, "last_ec_cost", None)
    meter = Metering(mode="live", requests=2,  # stocks.list + batch
                     ec_cost=float(ec) if ec is not None else None,
                     note="live snapshot hygiene pass (is_st/is_suspend)")
    if (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"hygiene EC ceiling {max_ec} exceeded: batch cost "
            f"{meter.ec_cost:.1f} EC"
        )
    results = getattr(snap, "results", None) or {}
    errs = getattr(snap, "errors", None) or {}
    if errs:
        warnings.append(f"{len(errs)} ticket(s) errored in the batch (dropped)")
    keep, n_st, n_susp = [], 0, 0
    for sym, fields in results.items():
        if not isinstance(fields, dict):
            continue
        if exclude_st and fields.get("is_st"):
            n_st += 1
            continue
        if exclude_suspended and fields.get("is_suspend"):
            n_susp += 1
            continue
        keep.append(instrument_map[str(sym)])
    if n_st:
        warnings.append(f"dropped {n_st} ST symbol(s)")
    if n_susp:
        warnings.append(f"dropped {n_susp} suspended symbol(s)")
    return FetchResult(frame=pd.DataFrame({"instrument": keep}),
                       metering=meter, warnings=warnings)


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
        # Never paginate this path. 2.x: unified envelope {ok, as_of, data,
        # meta} with the row DataFrame under data.rows.
        env = c.screener.query(
            where=where_cond, fields=codes, limit=10000, offset=0, idx=idx,
        )
        meter.requests += 1
        sdf = (env.get("data") or {}).get("rows") if isinstance(env, dict) else env
        if sdf is None:
            raise normalize.NormalizeError(
                "screener.query envelope missing data.rows (unexpected 2.x payload)"
            )
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
