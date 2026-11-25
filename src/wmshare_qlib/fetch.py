"""Fetch layer: live data acquisition over the wmshare SDK.

Since wmshare 3.x there is ONE data mode: live. Without credentials the SDK's
``from_env()`` returns a public-pool free-trial client (``auth_mode ==
"public_pool"``); with credentials it uses the caller's own account.
``_client(anonymous=True)`` forces the free trial and ignores configured
credentials. The retired offline demo snapshot (``demo_client``) was removed
by the SDK in 3.0.

Design rules (from the plan):
  * History panel: per-symbol calls. Never per-symbol indicator.get loops.
  * Cross section: screener.query (1 request, cost scales with the SCANNED
    universe) or the kline.snapshot batch (per-ticket, operator-controlled
    universe) - see fetch_cross_section / fetch_cross_section_snapshot.
  * Every fetch result carries a metering record.
  * M3-lite: per-symbol fetch is isolated (``_fetch_one_symbol``) so it can be
    concurrent; each worker owns its client so ``last_ec_cost`` has no race.
    A hard budget ceiling is checked at batch granularity (see ``max_ec`` doc).
"""

from __future__ import annotations

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
    if isinstance(exc, _werr.LimitError):
        # Public-pool free-trial refusals are TERMINAL upstream (wmshare
        # LIMITS.md: the shared quota is not refilled by retrying) - the SDK
        # maps IP_RATE_LIMITED to LimitError(code="ip_rate_limited"); the
        # sibling public refusals (pool_unavailable / ip_banned) ride AuthError,
        # which is never transient. Regular paid-app 429 rate limiting keeps
        # its bounded retry (retry_after is honored by call_with_retry).
        if str(getattr(exc, "code", "") or "") == "ip_rate_limited":
            return False
        if getattr(exc, "status", None) == 429:
            return True
    return False


def is_fatal(exc: Exception) -> bool:
    """True for errors that must abort the symbol, never degrade to a warning.

    A per-field fetch that swallows ``AuthError``/``QuotaError`` produces a panel
    with silently missing fields and exit code 0 - the user reads a thinner
    result and believes it. These two are account-level conditions: they affect
    every subsequent call and retrying the next field cannot help.
    """
    try:
        from wmshare import errors as _werr
    except ImportError:
        return False
    return isinstance(exc, (_werr.AuthError, _werr.QuotaError))


def _credential_hint(exc: Exception) -> str:
    """Turn an account-level failure into something a user can act on."""
    name = type(exc).__name__
    try:
        from wmshare import errors as _werr

        if isinstance(exc, _werr.AuthError):
            return (
                f"{name} - wmshare credentials are missing or rejected. "
                "Without credentials every client is a public-pool FREE-TRIAL "
                "client (shared quota, metered); set WMSHARE_DISABLE_ANON=1 to "
                "forbid that, or configure real credentials: WMSHARE_CLIENT_ID "
                "and WMSHARE_CLIENT_SECRET (or write ~/.wmshare/credentials.env), "
                "apply at https://open.winmale.com (我的 -> 开放平台 -> 我的应用). "
                f"Upstream said: {str(exc)[:160]}"
            )
        if isinstance(exc, _werr.QuotaError):
            return (
                f"{name} - wmshare quota exhausted (retrying will not help). "
                "On the public-pool free trial the quota is SHARED by every "
                "unregistered caller of your network - configure your own "
                "credentials to use your own account. Switch credentials or "
                "manage your plan at "
                f"https://open.winmale.com. Upstream said: {str(exc)[:160]}"
            )
    except ImportError:  # pragma: no cover - SDK is a hard dependency
        pass
    return f"{name}: {str(exc)[:200]}"


def call_with_retry(
    fn: Callable[[], Any],
    *,
    label: str,
    meter_cb: Callable[[], None],
    warn_cb: Callable[[str], None],
    attempts: int = RETRY_ATTEMPTS,
) -> Any:
    """One SDK call with per-attempt metering and a bounded transient retry.

    ``meter_cb`` fires after EVERY attempt - a failed attempt may still have
    been metered upstream, and pretending otherwise is exactly the class of
    silent loss the roadmap forbids. Retries are surfaced as warnings, never
    silent. ``attempts`` counts logical calls; the caller's ``requests``
    counter stays per logical call.
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
            delay = RETRY_BASE_DELAY * (2**i)
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
    """Request/metering accounting for one fetch run.

    ``auth_mode`` records WHICH client ran: ``credentials`` (the caller's own
    account), ``public_pool`` (free trial), or ``cache_hit`` (bookkeeping-only
    metering, no client was built). wmshare >=3.3.0 exposes the same
    discriminator as ``client.auth_mode``.

    ``ec_cost`` is the upstream-reported spend accumulator - INTERNAL guard
    state read by the ``max_ec`` executor. It is deliberately NOT part of
    ``as_meta()`` (nothing user-visible or persisted carries it).
    """

    mode: str = "live"
    requests: int = 0
    ec_cost: float | None = None
    note: str = "live"
    auth_mode: str = "credentials"

    def as_meta(self) -> dict[str, Any]:
        """User-facing serialization: scope + who ran, no internal guard state."""
        return {
            "mode": self.mode,
            "requests": self.requests,
            "auth_mode": self.auth_mode,
            "note": self.note,
        }


def _auth_mode(client: Any) -> str:
    """Which client pays for this call (client-side probe, never a guess).

    wmshare >=3.3.0 exposes ``auth_mode`` ("credentials" / "public_pool");
    older SDKs and stubs without the attribute are treated as credentialed.
    """
    return str(getattr(client, "auth_mode", "") or "credentials")


@dataclass
class FetchResult:
    frame: pd.DataFrame
    metering: Metering = field(default_factory=Metering)
    warnings: list[str] = field(default_factory=list)


def _client(
    max_limit: int | None = None, timeout: float | None = None, anonymous: bool = False
) -> Any:
    """Build the live wmshare client (the only data mode since wmshare 3.x).

    Credential resolution is entirely the SDK's: explicit args > env vars >
    credentials file. With NO credentials at all, ``Client.from_env`` returns
    a public-pool free-trial client (wmshare >=3.3.0; ``auth_mode ==
    "public_pool"``, shared quota, metered like any other client).
    ``anonymous=True`` forces that mode and IGNORES configured credentials
    (mirrors the SDK CLI's ``--anonymous``).

    ``max_limit`` is forwarded (e.g. 10000 lets screener.query fetch the
    whole universe in ONE request - pagination would re-scan everything).
    ``timeout`` is
    forwarded because full-universe column materialization can exceed the
    SDK's 30s default (measured: TransportError read timeout).

    NOTE: ``validate`` is deliberately LEFT AT ITS DEFAULT (True). The SDK's
    unit invariants (volume x close / amount) are the only client-side check
    that catches an upstream returning the wrong adjustment basis - e.g.
    2026-11-19: ``kline.bars("600519", n=5, adj=None)`` served back-adjusted
    prices (12473.67 vs a true 1442.66), tripping this very invariant.
    Turning validation off here would silently feed wrong prices into a
    backtest (see wmshare docs/BACKLOG.md).
    """
    import wmshare

    kw: dict[str, Any] = {}
    if max_limit:
        kw["max_limit"] = max_limit
    if timeout:
        kw["timeout"] = timeout
    builder = wmshare.Client.anonymous if anonymous else wmshare.Client.from_env
    try:
        return builder(**kw)
    except Exception as exc:
        # AuthError reaches here when credentials exist but are REJECTED, or
        # when WMSHARE_DISABLE_ANON=1 forbids the free-trial fallback (SDK
        # raises instead of degrading). Fail loud with an actionable hint.
        if is_fatal(exc):
            raise RuntimeError(_credential_hint(exc)) from exc
        raise


def _valuation_frames(
    c: Any,
    symbol: str,
    instrument: str,
    fields: tuple[str, ...],
    meter_cb: Callable[[], None],
    window: str = config.VALUATION_WINDOW,
) -> tuple[list[pd.DataFrame], list[str], int]:
    """Fetch the pe/pb/ps/pr valuation-window series (shared by bulk and tail paths).

    Returns ``(frames, warnings, requests_used)``. The demo snapshot only
    stores pe/3y (callers pass ``window="3y"`` for demo); unsupported metrics
    are skipped with an explicit warning (cold-cache guard: never fabricate,
    never fill 0). Metering goes through the caller's ``meter_cb`` so
    per-path accounting stays exact.
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    requests = 0
    for vfield in config.VALUATION_FIELDS:
        if vfield not in fields:
            continue
        try:
            payload = call_with_retry(
                lambda vf=vfield: c.valuation.hist_stats(
                    symbol, metric=vf, window=window),
                label=f"{symbol}/{vfield}",
                meter_cb=meter_cb,
                warn_cb=warnings.append,
            )
        except Exception as exc:  # wmshare.errors.UsageError and friends
            requests += 1
            if is_fatal(exc):
                raise RuntimeError(_credential_hint(exc)) from exc
            warnings.append(f"{symbol}/{vfield}: skipped ({type(exc).__name__}: {str(exc)[:60]})")
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
    c: Any,
    symbol: str,
    instrument: str,
    fields: tuple[str, ...],
    meter_cb: Callable[[], None],
    *,
    begin: str | None = None,
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
            label=f"{symbol}/daily_basic",
            meter_cb=meter_cb,
            warn_cb=warnings.append,
        )
    except Exception as exc:  # endpoint not deployed yet / catalog gap: honest skip
        requests += 1
        if is_fatal(exc):
            raise RuntimeError(_credential_hint(exc)) from exc
        warnings.append(
            f"{symbol}/daily_basic: unavailable, skipped ({type(exc).__name__}: {str(exc)[:60]})"
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
    adjust_mode: str = "hfq_factor",
) -> tuple[list[pd.DataFrame], int, float | None, list[str]]:
    """Fetch ALL history fields for one symbol via one dedicated client.

    Returns ``(frames, requests, ec_cost, warnings)``. ``ec_cost`` is the SUM
    of per-call upstream metering readings (None when the client carries no
    meter - stubs). Owning the client per call keeps ``last_ec_cost`` free of
    write races when the caller runs several symbols concurrently.

    ``adjust_mode``: 'hfq_factor' (default) pulls kline TWICE (``adj="hfq"`` +
    raw) and emits factor = hfq/raw with volume/factor (Qlib yahoo-collector
    convention, see normalize.merge_adjusted_klines); 'qfq_const' keeps the
    v1 single-call path with factor=1.0.
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
    if adjust_mode == "hfq_factor":
        # Dual pull: back-adjusted + raw, same n. Dates must align 1:1
        # (probed: they do); a mismatch raises inside the merge - never merge
        # on a best-effort subset.
        kadj = call_with_retry(
            lambda: c.kline.bars(symbol, n=n, adj="hfq"),
            label=f"{symbol}/kline(adj)",
            meter_cb=_meter,
            warn_cb=warnings.append,
        )
        requests += 1
        kraw = call_with_retry(
            lambda: c.kline.bars(symbol, n=n, adj=None),
            label=f"{symbol}/kline(raw)",
            meter_cb=_meter,
            warn_cb=warnings.append,
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
        # Explicit qfq_const: single pull, factor=1.0 (v1 behaviour).
        kraw = call_with_retry(
            lambda: c.kline.bars(symbol, n=n, adj="qfq"),
            label=f"{symbol}/kline(qfq)",
            meter_cb=_meter,
            warn_cb=warnings.append,
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
    if n and len(kdf) < int(n):
        # Upstream can serve FEWER bars than requested (observed 2026-11-23:
        # n=60 -> 59 for every symbol). The window's oldest bar(s) are simply
        # absent upstream; never fill them, but say so - a silent short window
        # is how the 08-21 bar went missing from the store's 60-bar panel.
        warnings.append(
            f"{symbol}/kline: upstream served {len(kdf)} of {n} requested bar(s) "
            f"({kdf['date'].iloc[0]}..{kdf['date'].iloc[-1]}); the oldest bar(s) "
            f"are missing upstream, not filled"
        )
    keep = [c0 for c0 in ("date", "symbol", "instrument", *config.BASE_FIELDS) if c0 in kdf.columns]
    frames.append(
        kdf[keep].melt(
            id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
        )
    )

    # --- valuation (pe/pb/ps/pr) ---
    vframes, vwarnings, vrequests = _valuation_frames(
        c, symbol, instrument, fields, _meter, window=config.VALUATION_WINDOW
    )
    requests += vrequests
    warnings.extend(vwarnings)
    frames.extend(vframes)

    # --- daily_basic (upstream-gated: materialized wide table, plan v3) ---
    dbframes, dbwarnings, dbrequests = _daily_basic_frames(c, symbol, instrument, fields, _meter)
    requests += dbrequests
    warnings.extend(dbwarnings)
    frames.extend(dbframes)

    # --- moneyflow ---
    if any(f in fields for f in config.MONEYFLOW_FIELDS):
        try:
            mraw = call_with_retry(
                lambda: c.moneyflow.bars(symbol, n=n),
                label=f"{symbol}/moneyflow",
                meter_cb=_meter,
                warn_cb=warnings.append,
            )
        except Exception as exc:
            requests += 1
            if is_fatal(exc):
                raise RuntimeError(_credential_hint(exc)) from exc
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
            mf_keep = [
                c0
                for c0 in ("date", "symbol", "instrument", *config.MONEYFLOW_FIELDS)
                if c0 in mdf.columns
            ]
            frames.append(
                mdf[mf_keep].melt(
                    id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
                )
            )

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
    """Generic per-symbol executor: serial or pooled, with the hard budget ceiling.

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
                batch = symbols[idx : idx + batch_n]
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
                f"budget ceiling {max_ec} reached: stopped after {stopped_at} "
                f"symbol(s); {remaining} symbol(s) not submitted - raise "
                f"--max-ec and re-run to resume"
            )
    return stopped_at


def fetch_history_panel(
    symbols: Iterable[str],
    *,
    fields: tuple[str, ...] = config.HISTORY_FIELDS,
    n: int = 120,
    client: Any | None = None,
    anonymous: bool = False,
    max_workers: int = 1,
    max_ec: float | None = None,
    adjust_mode: str | None = None,
    max_limit: int | None = None,
    on_symbol: Callable[[str, str, list[pd.DataFrame], float | None], None] | None = None,
) -> FetchResult:
    """Fetch per-symbol history and merge into a long panel (live).

    Without credentials the client is a public-pool free-trial client;
    ``anonymous=True`` forces that mode. ``max_ec`` stays the binding
    ceiling either way.

    M3-lite additions (all backward compatible - defaults keep old behavior):
      * ``max_workers``: concurrent workers. Each worker owns a fresh client
        (or reuses ``client`` when workers<=1) so ``last_ec_cost`` never races.
        Results are merged in the ORIGINAL symbol order for determinism.
      * ``max_ec``: hard budget ceiling for THIS call, enforced BEFORE submitting
        each batch. A probe batch of 1 symbol measures real per-symbol metering;
        later batches are sized as floor(remaining_budget / per_symbol_upper_bound),
        where the bound only ever increases. Honest worst case: the single probe
        symbol may itself exceed the ceiling (cost is unknowable before the
        first call).
      * ``on_symbol(symbol, instrument, frames, ec)``: called right after a
        symbol succeeds, so a caller can persist a resumable fragment before an
        interruption. The callback must be cheap and side-effect-local.

    P0-2' addition:
      * ``adjust_mode``: None -> auto ('hfq_factor', see config.adjustment_mode).
        hfq_factor issues two kline calls per symbol (adj + raw); the emitted
        ``factor`` column is the real cumulative adjustment and volume is
        divided by it.

    P1-2 addition (deep history):
      * ``max_limit``: raises the client's per-call served-bar cap (default
        500) so a single call can serve n > 500 bars. n-mode has NO backward
        paging (every call returns the most recent n bars), so deep history
        MUST be one call with a raised cap - range queries are bounded by the
        server's anchor window (a falsified probe; see docs/data-model.md).
        Passed to both the main and per-worker clients; ignored when
        ``client`` is supplied.

    Anti-pitfall #5: symbols with cold-cache / short window are skipped and
    listed in warnings, never filled with zeros.
    """
    adjust_mode = config.adjustment_mode(adjust_mode)
    c_main = (
        client
        if client is not None
        else _client(anonymous=anonymous, max_limit=max_limit)
    )
    instrument_map = normalize.build_instrument_map(c_main)
    symbols = list(symbols)
    warnings: list[str] = []

    effective_workers = max(1, int(max_workers))
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

    auth = _auth_mode(c_main)
    meter = Metering(
        mode="live",
        auth_mode=auth,
        note=(
            f"live ({auth}); adjust_mode={adjust_mode}"
            + (" (kline pulled twice: adj + raw)" if adjust_mode == "hfq_factor" else "")
        ),
    )
    collected: dict[str, tuple[str, tuple[list[pd.DataFrame], int, float | None, list[str]]]] = {}

    def _run(symbol: str):
        # A per-worker client is built the SAME way as the main one: dropping
        # ``anonymous`` here billed the configured private balance while
        # metering (derived from c_main) reported public_pool.
        wc = (
            c_main
            if effective_workers == 1
            else _client(max_limit=max_limit, anonymous=anonymous)
        )
        instrument = normalize.to_qlib_instrument(symbol, instrument_map)
        return (
            symbol,
            instrument,
            _fetch_one_symbol(wc, symbol, instrument, fields, n, adjust_mode),
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

    # Per-symbol metering upper bound: unknown until the first (probe) symbol
    # finishes. Only ever increases, so a later batch can never be
    # under-reserved.
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

    panel = (
        pd.concat(frames_all, ignore_index=True)
        if frames_all
        else pd.DataFrame(columns=["date", "symbol", "instrument", "field", "value"])
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
    db_begin: str | None = None,
) -> tuple[list[pd.DataFrame], int, float | None, list[str]]:
    """Incremental tail for one symbol (P0-1): dual kline pull + moneyflow,
    valuation only when its cadence is due.

    The dual pull (adj + raw) is REQUIRED even for the tail:
    ``kline.batch_live`` serves the raw live price with UNVERIFIED ex-div
    semantics (its ``percent`` basis on ex-div days cannot be probed on
    demand), while ``bars(n=k)`` returns hfq AND raw AND the exact factor in
    one merge. k = gap_days + overlap, so a daily run stays far smaller than
    a full re-pull.

    ``db_begin`` windows the daily_basic read (P3-1): the endpoint scans the
    requested window server-side, so without a begin it re-scans the full
    history every single run - with the resume pointer the scan stays inside
    the tail window. First-ever tail uses the tail window start.
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
        label=f"{symbol}/kline-tail(adj)",
        meter_cb=_meter,
        warn_cb=warnings.append,
    )
    requests += 1
    kraw = call_with_retry(
        lambda: c.kline.bars(symbol, n=k, adj=None),
        label=f"{symbol}/kline-tail(raw)",
        meter_cb=_meter,
        warn_cb=warnings.append,
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
    if len(kdf) < k:
        # A short tail can leave a seam HOLE against the previous tail (the
        # window no longer reaches back `overlap` bars). Missing is honest;
        # silent is not.
        warnings.append(
            f"{symbol}/kline-tail: upstream served {len(kdf)} of {k} requested "
            f"bar(s) ({kdf['date'].iloc[0]}..{kdf['date'].iloc[-1]}); the merge "
            f"may not reach the previous tail - re-check continuity"
        )
    keep = [c0 for c0 in ("date", "symbol", "instrument", *config.BASE_FIELDS) if c0 in kdf.columns]
    frames.append(
        kdf[keep].melt(
            id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
        )
    )

    # --- moneyflow tail ---
    if any(f in fields for f in config.MONEYFLOW_FIELDS):
        mraw = call_with_retry(
            lambda: c.moneyflow.bars(symbol, n=k),
            label=f"{symbol}/moneyflow-tail",
            meter_cb=_meter,
            warn_cb=warnings.append,
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
        mf_keep = [
            c0
            for c0 in ("date", "symbol", "instrument", *config.MONEYFLOW_FIELDS)
            if c0 in mdf.columns
        ]
        frames.append(
            mdf[mf_keep].melt(
                id_vars=["date", "symbol", "instrument"], var_name="field", value_name="value"
            )
        )

    # --- valuation (cadence-gated: the window re-pulls the whole series,
    # which the bulk/tail reconciliation then verifies row-by-row) ---
    if want_valuation:
        vframes, vwarnings, vrequests = _valuation_frames(c, symbol, instrument, fields, _meter)
        requests += vrequests
        warnings.extend(vwarnings)
        frames.extend(vframes)

    # --- daily_basic tail (windowed read; unavailable upstream = skip) ---
    dbframes, dbwarnings, dbrequests = _daily_basic_frames(
        c, symbol, instrument, fields, _meter, begin=db_begin
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
    daily_basic_last_dates: dict[str, str | None] | None = None,
    today: str | None = None,
    overlap: int = 5,
    tail_max_n: int = 30,
    valuation_days: int = 7,
    fields: tuple[str, ...] = config.HISTORY_FIELDS,
    client: Any | None = None,
    anonymous: bool = False,
    max_workers: int = 1,
    max_ec: float | None = None,
    on_symbol: Callable[[str, str, list[pd.DataFrame], float | None], None] | None = None,
) -> FetchResult:
    """Fetch incremental tails for symbols that already have bulk fragments.

    P0-1 incremental mode (fetch side). Per-symbol plan:
      * gap = calendar days between ``today`` and the symbol's last stored bar;
        gap <= 0 -> skip (already current, nothing to fetch);
      * k = gap + ``overlap`` bars requested (overlap bridges non-trading days
        between the stored tail and now, and forces row-level reconciliation
        against the stored series: immutable history must agree - see
        resume.load_panel_with_tail);
      * k > ``tail_max_n`` -> skip loud: the tail stops being smaller than a
        full re-pull, so the caller is told to bulk-refetch the symbol;
      * valuation refresh fires only when ``valuation_days`` elapsed since the
        symbol's last valuation snapshot;
      * daily_basic read is windowed from ``daily_basic_last_dates[s]``
        (resume pointer; first-ever tail falls back to the tail window start,
        never an unbounded full-history re-read - P3-1).

    The budget ceiling and concurrency share the exact bulk executor
    discipline (``_execute_per_symbol``).
    """
    c_main = client if client is not None else _client(anonymous=anonymous)
    instrument_map = normalize.build_instrument_map(c_main)
    symbols = list(symbols)
    warnings: list[str] = []
    today_date = dt.date.fromisoformat(today) if today else dt.date.today()

    unknown = [s for s in symbols if s not in instrument_map]
    if unknown:
        raise normalize.NormalizeError(
            f"symbols outside universe: {unknown[:5]} (limit universe to stocks.list codes)"
        )

    # --- plan phase (pure bookkeeping, zero requests) ---
    plan: dict[str, tuple[str, int, bool, str | None]] = {}
    v_last_map = valuation_last_dates or {}
    db_last_map = daily_basic_last_dates or {}
    want_daily_basic = any(f in fields for f in config.PANEL_DAILY_BASIC_FIELDS)
    for s in symbols:
        last = last_dates.get(s)
        if not last:
            warnings.append(f"{s}: skipped - no bulk fragment on record; run dump-history first")
            continue
        gap = (today_date - dt.date.fromisoformat(str(last))).days
        if gap <= 0:
            warnings.append(f"{s}: up to date (last {last}), nothing to fetch")
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
        db_begin: str | None = None
        if want_daily_basic:
            # Windowed read (P3-1): resume pointer when present, tail window
            # start on first-ever read. Never an unbounded full-history scan.
            db_last = db_last_map.get(s)
            db_begin = (
                str(db_last)
                if db_last
                else (today_date - dt.timedelta(days=tail_max_n)).isoformat()
            )
        plan[s] = (normalize.to_qlib_instrument(s, instrument_map), k, v_due, db_begin)

    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(c_main),
        note=(
            "live incremental tail: kline adj+raw per symbol, moneyflow "
            "tail, valuation on cadence, daily_basic windowed from the "
            "resume pointer; overlap rows reconcile against stored history"
        ),
    )
    collected: dict[str, tuple[str, tuple[list[pd.DataFrame], int, float | None, list[str]]]] = {}

    def _run(symbol: str):
        # Same discipline as the bulk kernel: worker clients inherit
        # ``anonymous`` (a bare _client() billed credentials while metering
        # said public_pool).
        wc = c_main if max_workers <= 1 else _client(anonymous=anonymous)
        instrument, k, v_due, db_begin = plan[symbol]
        return (
            symbol,
            instrument,
            _fetch_tail_one_symbol(wc, symbol, instrument, k, v_due, fields, db_begin=db_begin),
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
    panel = (
        pd.concat(frames_all, ignore_index=True)
        if frames_all
        else pd.DataFrame(columns=["date", "symbol", "instrument", "field", "value"])
    )
    if not panel.empty:
        panel["value"] = panel["value"].astype(float)
    return FetchResult(frame=panel, metering=meter, warnings=list(dict.fromkeys(warnings)))


def _supports_fields(client: Any) -> bool:
    """Whether the installed wmshare's snapshot facade accepts ``_fields=``.

    ``_fields`` is an underscore client option (client-side column projection,
    applied AFTER unit conversion) shipped in wmshare 2.2.0. wmshare's
    COMPATIBILITY.md explicitly places "_"-prefixed client options OUTSIDE the
    public-surface promise (they may change in any MINOR), so this adapter
    detects support from the signature instead of gating on a version - a
    future SDK dropping the kwarg degrades to the unprojected call instead of
    crashing. Unsupported SDKs get identical data, wider dicts.
    """
    try:
        import inspect

        return "_fields" in inspect.signature(client.kline.snapshot).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _snapshot_chunk_warnings(snap: Any, chunk: list[str], warnings: list[str], label: str) -> None:
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
    as_of: str | None = None,
    client: Any | None = None,
    anonymous: bool = False,
    metric_codes: tuple[Any, ...] = config.SNAPSHOT_METRICS,
    max_ec: float | None = None,
    exclude_st: bool = True,
    exclude_suspended: bool = True,
) -> FetchResult:
    """Cross-section score from the kline.snapshot BATCH (P0-3).

    Why this path: the server does not serve screener index subsets
    (`index.hs300` -> "not loadable", probe 2026-11-17) and screener.query
    scans the whole universe it is pointed at, so the scan size cannot be
    narrowed client-side. ``kline.snapshot`` accepts an explicit symbol list
    and works per ticket - the only path that keeps an hs300-sized DAILY
    cross-section sustainable.

    Score family: TECHNICAL COMPOSITE (see config.SNAPSHOT_METRICS) - declared
    directions, z-score composite. NOT the screener fundamentals family;
    the metering note labels it so the score meta never blurs the two.

    Guards:
      * >= 3 symbols (the SDK's native batch route); fewer silently falls
        back to per-ticket evaluation;
      * universe intersected with stocks.list - unmapped codes are
        dropped with a warning, never scored under a bare code;
      * ST / suspended rows are DROPPED with a warning (universe hygiene,
        anti-pitfall #5: never scored, never filled);
      * rows missing any metric value are DROPPED with a warning
        (anti-pitfall #5: a 0 z-score pretends "exactly average");
      * silent server-side omissions (``BatchResult.truncated``) are reported
        as a warning - a missing ticket is never silently scored-over;
      * ``max_ec`` REQUIRED (pre-flight ack), enforced post-hoc via
        ``client.last_ec_cost`` - the batch result itself does not carry it.
    """
    if max_ec is None:
        raise normalize.NormalizeError(
            "snapshot cross-section requires an explicit max_ec (budget "
            "ceiling): the batch works per ticket and out-of-index tickets "
            "fall back to per-ticket evaluation; set the budget before firing"
        )
    symbols = list(symbols)
    if len(symbols) < 3:
        raise normalize.NormalizeError(
            f"snapshot batch needs >= 3 symbols (got {len(symbols)}): fewer "
            f"falls back to per-ticket evaluation"
        )
    c = client if client is not None else _client(anonymous=anonymous)
    day = as_of or dt.date.today().isoformat()
    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(c),
        note=(
            "live snapshot-batch cross-section: TECHNICAL COMPOSITE family "
            "(config.SNAPSHOT_METRICS), ST/suspended and missing-metric "
            "rows dropped, never filled"
        ),
    )
    warnings: list[str] = []

    instrument_map = normalize.build_instrument_map(c)
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
    # Spend is the SUM of per-chunk last_ec_cost readings (per-call semantics,
    # same pattern as the per-symbol meter).
    #
    # _fields adoption (wmshare latest spec, signature-gated - see
    # _supports_fields): projects columns CLIENT-side after unit conversion.
    # It does NOT change what the server scans (only the response body
    # narrows). The field set is a SUPERSET of everything the downstream loop
    # touches (is_st/is_suspend + every metric code): the SDK projection
    # raises UsageError on unknown fields, so omitting a needed column would
    # fail loud - requesting extras is the safe direction.
    fields_want = sorted({"is_st", "is_suspend"} | {m.code for m in metric_codes})
    use_fields = _supports_fields(c)
    results: dict[str, dict] = {}
    n_requests = 0
    ec_total = 0.0
    has_meter = False
    for i in range(0, len(universe), config.SNAPSHOT_BATCH_CHUNK):
        chunk = universe[i : i + config.SNAPSHOT_BATCH_CHUNK]
        snap = (
            c.kline.snapshot(chunk, _fields=fields_want) if use_fields else c.kline.snapshot(chunk)
        )
        n_requests += int(getattr(snap, "requests", 1) or 1)
        results.update(getattr(snap, "results", None) or {})
        _snapshot_chunk_warnings(
            snap, chunk, warnings, label=f"chunk {i // config.SNAPSHOT_BATCH_CHUNK}"
        )
        errs = getattr(snap, "errors", None) or {}
        if errs:
            warnings.append(
                f"{len(errs)} ticket(s) errored in chunk "
                f"{i // config.SNAPSHOT_BATCH_CHUNK} (dropped, "
                f"e.g. {list(errs)[:3]})"
            )
        v = getattr(c, "last_ec_cost", None)
        if v is not None:
            ec_total += float(v)
            has_meter = True
    meter.requests += n_requests
    meter.ec_cost = ec_total if has_meter else None
    if (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"snapshot cross-section budget ceiling {max_ec} exceeded for "
            f"{len(universe)} tickets - check the universe for out-of-index "
            "codes (they fall back to per-ticket evaluation) or raise --max-ec"
        )
    results = results or {}
    errs = getattr(snap, "errors", None) or {}
    if errs:
        warnings.append(
            f"{len(errs)} ticket(s) errored in the batch (dropped, e.g. {list(errs)[:3]})"
        )

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
        rows.append(
            {
                "symbol": str(sym),
                **{k: v for k, v in fields.items() if k not in ("is_st", "is_suspend")},
            }
        )
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

    Full pull of ``index.hs300`` scanned live 2026-11-17 - pass --max-ec at
    the CLI layer before firing. Returns bare codes; instrument mapping
    happens at the caller via ``stocks.list``.
    """
    try:
        raw = client.indices.members(key)
    except AttributeError as exc:
        raise normalize.NormalizeError(
            f"client has no indices namespace (index membership needs a real "
            f"wmshare client): {exc}"
        ) from exc
    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(client),
        note=f"live indices.members({key})",
    )
    warnings: list[str] = []
    if isinstance(raw, pd.DataFrame):
        df = raw
    else:
        df = pd.DataFrame(raw)
    sym_col = next((c0 for c0 in ("symbol", "code", "sym") if c0 in df.columns), df.columns[0])
    codes = df[sym_col].astype(str).tolist()
    ec = getattr(client, "last_ec_cost", None)
    if ec is not None:
        meter.ec_cost = float(ec)
    meter.requests = 1
    if not codes:
        raise normalize.NormalizeError(f"index {key!r} returned no members")
    return FetchResult(frame=pd.DataFrame({"symbol": codes}), metering=meter, warnings=warnings)


def filter_universe_hygiene(
    symbols: Iterable[str],
    *,
    client: Any,
    exclude_st: bool = True,
    exclude_suspended: bool = True,
    max_ec: float | None = None,
) -> FetchResult:
    """Drop ST / suspended symbols from a universe via kline.snapshot (P1-1).

    Hygiene pre-pass: fetches the is_st/is_suspend flags BEFORE any history
    fetch. Shares the snapshot-batch guards with the P0-3 cross-section:
    >= 3 symbols, universe intersected with stocks.list, --max-ec post-hoc
    ceiling.
    """
    if max_ec is None:
        raise normalize.NormalizeError(
            "universe hygiene requires an explicit max_ec (budget ceiling; "
            "the snapshot batch works per ticket)"
        )
    symbols = list(symbols)
    if len(symbols) < 3:
        raise normalize.NormalizeError(f"snapshot batch needs >= 3 symbols (got {len(symbols)})")
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
    # body client-side, the server scan is unchanged.
    if _supports_fields(client):
        snap = client.kline.snapshot(universe, _fields=["is_st", "is_suspend"])
    else:
        snap = client.kline.snapshot(universe)
    _snapshot_chunk_warnings(snap, list(universe), warnings, label="hygiene batch")
    ec = getattr(client, "last_ec_cost", None)
    meter = Metering(
        mode="live",
        requests=2,  # stocks.list + batch
        ec_cost=float(ec) if ec is not None else None,
        note="live snapshot hygiene pass (is_st/is_suspend)",
        auth_mode=_auth_mode(client),
    )
    if (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"hygiene budget ceiling {max_ec} exceeded; raise --max-ec or "
            "narrow the universe"
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
    return FetchResult(frame=pd.DataFrame({"instrument": keep}), metering=meter, warnings=warnings)


def fetch_cross_section(
    *,
    as_of: str | None = None,
    client: Any | None = None,
    anonymous: bool = False,
    metric_codes: tuple[Any, ...] = config.CROSS_SECTION_METRICS,
    universe: Iterable[str] | None = None,
    max_ec: float | None = None,
    where: str | None = None,
    idx: str | None = None,
) -> FetchResult:
    """Fetch today's cross-sectional scores.

    Path: screener.query(fields=[codes], ...) -> wide frame -> composite score.

    The server materializes the requested columns over the SCANNED universe
    in one request - a full-market scan is heavy and NOT viable as a daily
    cadence. Use ``idx`` (e.g. "index.hs300", ~300 names) for daily
    accumulation. ``max_ec`` is REQUIRED: materialization cannot be
    pre-stopped, so this is a post-hoc ceiling + pre-flight budget ack.
    Stock-domain pre-materialized codes only (see config.CROSS_SECTION_METRICS);
    unknown codes drop with a warning.
    """
    # Budget gate: screener.query scans per row per column and a small
    # limit=10 probe does NOT predict a full-universe scan. Budget required
    # fail-loud; the ceiling is checked post-hoc (the scan is paid once the
    # response returns) via client.last_ec_cost.
    if max_ec is None:
        raise normalize.NormalizeError(
            "cross-section requires an explicit max_ec (budget ceiling): "
            "screener.query materializes the columns in one request and the "
            "cost is only knowable once paid, so set a budget before firing"
        )
    c = (
        client
        if client is not None
        else _client(anonymous=anonymous, max_limit=10000, timeout=120)
    )
    day = as_of or dt.date.today().isoformat()
    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(c),
        note=(
            "live: screener.query materializes the requested columns over "
            "the scanned universe in one request"
        ),
    )
    warnings: list[str] = []

    if universe is not None:
        # The path scopes via screener.query(idx=...) and cannot apply a
        # per-call code list; silently returning the whole market would hand
        # back a frame the caller did not ask for.
        warnings.append(
            "universe= is ignored; scope a cross-section "
            "with idx=<index key> (e.g. idx='index.hs300') instead"
        )
    codes = [m.code for m in metric_codes]
    where_cond = where or config.CROSS_SECTION_WHERE
    # ONE request at the 10000-row hard cap - the universe (~5.7k) fits,
    # and offset pagination would re-materialize every column per page.
    # Never paginate this path. 2.x: unified envelope {ok, as_of, data,
    # meta} with the row DataFrame under data.rows.
    env = c.screener.query(
        where=where_cond,
        fields=codes,
        limit=10000,
        offset=0,
        idx=idx,
    )
    meter.requests += 1
    sdf = (env.get("data") or {}).get("rows") if isinstance(env, dict) else env
    if sdf is None:
        raise normalize.NormalizeError(
            "screener.query envelope missing data.rows (unexpected 2.x payload)"
        )
    # Spend accounting: the gateway surfaces cost via client.last_ec_cost
    # (response attrs do NOT carry it). This is the post-hoc ceiling's
    # EYES - without it max_ec is blind (on 2026-11-17 a heavy scan slipped
    # a small ceiling exactly because of this read miss).
    ec = getattr(sdf, "attrs", {}).get("ec_cost")
    if ec is None:
        ec = getattr(c, "last_ec_cost", None)
    if ec is not None:
        meter.ec_cost = float(ec)
    if max_ec is not None and (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"cross-section budget ceiling {max_ec} exceeded: the single "
            f"request materialized over the whole scanned universe "
            f"(rows returned: {len(sdf)}); scope with --idx or raise --max-ec"
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
            f"{len(unmapped)} symbol(s) not in stocks.list (dropped, e.g. {unmapped[:5]})"
        )
    sdf = sdf[sdf["symbol"].astype(str).isin(instrument_map)].copy()
    sdf["instrument"] = sdf["symbol"].astype(str).map(instrument_map)
    frame = _score_from_wide(sdf, metric_codes, day, warnings=warnings)

    return FetchResult(frame=frame, metering=meter, warnings=warnings)


def _score_from_wide(
    wide: pd.DataFrame,
    metrics: tuple[Any, ...],
    day: str,
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


def fetch_margin_snapshot(
    symbols: Iterable[str],
    *,
    client: Any | None = None,
    max_ec: float | None = None,
    anonymous: bool = False,
) -> FetchResult:
    """One forward-only Layer-3 margin snapshot (``margin.latest`` per symbol).

    PIT discipline: captures TODAY's real margin row per symbol and appends it
    to the accumulation store - never backfilled. Symbols outside the margin
    universe (a subset of all equities, ~4k names) return an empty row and are
    reported as warnings, not errors. Missing days are healed by
    ``fetch_margin_history`` (explicit, budgeted) - never by fabricating rows.
    """
    symbols = [str(s) for s in symbols]
    if not symbols:
        raise normalize.NormalizeError("margin snapshot needs at least one symbol")
    c = client if client is not None else _client(anonymous=anonymous)
    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(c),
        note="live margin.latest per symbol; Layer-3 forward-only PIT",
    )
    warnings: list[str] = []
    frames = []
    ec_total = 0.0
    has_meter = False
    for sym in symbols:
        raw = c.margin.latest(sym)
        v = getattr(c, "last_ec_cost", None)
        if v is not None:
            ec_total += float(v)  # per-call: last_ec_cost is the LAST bill only
            has_meter = True
        if isinstance(raw, pd.DataFrame) and raw.empty:
            warnings.append(
                f"{sym}: no margin row (likely outside the margin universe); skipped"
            )
            continue
        frame = normalize.margin_rows_to_frame(raw, symbol=sym)
        if frame.empty:
            warnings.append(f"{sym}: margin row had no {len(config.MARGIN_FIELDS)}-field values")
            continue
        frames.append(frame)
    meter.requests = len(symbols)
    meter.ec_cost = ec_total if has_meter else None
    if max_ec is not None and (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"margin snapshot budget ceiling {max_ec} exceeded for "
            f"{len(symbols)} ticket(s); raise --max-ec or narrow --symbols"
        )
    if not frames:
        raise normalize.NormalizeError(
            "margin snapshot fetched no rows (all symbols outside the margin universe?)"
        )
    frame = pd.concat(frames, ignore_index=True)
    return FetchResult(frame=frame, metering=meter, warnings=warnings)


def fetch_margin_history(
    symbol: str,
    days: int,
    *,
    client: Any | None = None,
    max_ec: float | None = None,
    anonymous: bool = False,
) -> FetchResult:
    """Explicit, budgeted catch-up pull (``margin.query(limit=days+2)``).

    The ONLY sanctioned backfill path for the margin accumulation store - a
    real upstream history read, declared before firing (1-2 missed days are
    better served by daily ``fetch_margin_snapshot`` runs). The +2 row buffer
    gives the overlap reconciliation something to agree on.
    """
    if days <= 0:
        raise normalize.NormalizeError("margin history needs days >= 1")
    c = client if client is not None else _client(anonymous=anonymous)
    limit = int(days) + 2
    meter = Metering(
        mode="live",
        auth_mode=_auth_mode(c),
        note=f"live margin.query(limit={limit}); explicit catch-up",
    )
    raw = c.margin.query(symbol, limit=limit)
    frame = normalize.margin_rows_to_frame(raw, symbol=str(symbol))
    meter.requests = 1
    ec = getattr(c, "last_ec_cost", None)
    if ec is not None:
        meter.ec_cost = float(ec)
    if max_ec is not None and (meter.ec_cost or 0.0) > max_ec:
        raise normalize.NormalizeError(
            f"margin history budget ceiling {max_ec} exceeded "
            f"(query(limit={limit})); raise --max-ec or narrow the catch-up"
        )
    if frame.empty:
        raise normalize.NormalizeError(
            f"margin history for {symbol} returned no rows (outside the margin universe?)"
        )
    return FetchResult(frame=frame, metering=meter, warnings=[])
