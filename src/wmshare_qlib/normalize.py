"""Normalization layer: bare code -> Qlib instrument, date/unit guards, direction asserts.

This module is where the five AGENTS.md anti-pitfalls are enforced. Every guard
raises or annotates explicitly - we never silently fill 0 or guess direction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from . import config


class NormalizeError(ValueError):
    """Raised when an upstream payload violates an explicit guard."""


# Upstream valuation timestamps are Beijing-midnight epochs (the second of
# 00:00 CST, i.e. 16:00 UTC of the previous UTC day: ts % 86400 == 57600).
# Converting with UTC shifts the date ONE DAY EARLY and silently misaligns
# $pe against $close (P0-2, observed on demo: ts=1793289600 -> UTC 2026-10-29
# but the trading date is 2026-10-30). All epoch-second conversion MUST use CST.
CST = dt.timezone(dt.timedelta(hours=8))


def build_instrument_map(client: Any) -> dict[str, str]:
    """Build bare-code -> Qlib instrument map from wmshare stocks.list().

    Uses the exchange field ('sh'/'sz'/'bj'), never infers suffix from the number
    prefix (which is wrong for e.g. BJ stocks and some SZ codes).
    """
    df = client.stocks.list(_df=True)
    required = {"symbol", "exchange"}
    missing = required - set(df.columns)
    if missing:
        raise NormalizeError(f"stocks.list missing columns: {missing}")
    mapping: dict[str, str] = {}
    for symbol, exchange in zip(df["symbol"], df["exchange"]):
        suffix = config.EXCHANGE_SUFFIX.get(str(exchange).lower())
        if suffix is None:
            raise NormalizeError(f"unknown exchange {exchange!r} for {symbol}")
        mapping[str(symbol)] = f"{suffix}{symbol}"
    return mapping


def to_qlib_instrument(symbol: str, instrument_map: dict[str, str]) -> str:
    if symbol in instrument_map:
        return instrument_map[symbol]
    # Fallback for codes absent from the demo snapshot universe: infer by range,
    # but mark this path explicitly via a distinct return convention is not used;
    # instead callers should restrict to the universe. Raise to stay honest.
    raise NormalizeError(
        f"symbol {symbol!r} not in instrument map - restrict universe to known codes"
    )


def _coerce_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        # unix seconds (valuation.hist_stats series uses this).
        # Beijing-midnight epoch: UTC conversion shifts the date one day early
        # (P0-2). Always convert with CST (UTC+8).
        return dt.datetime.fromtimestamp(value, tz=CST).date()
    s = str(value)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise NormalizeError(f"unparseable date value: {value!r}")


def assert_series_order(series: pd.Series, expected: str, label: str) -> None:
    """Assert monotonic date direction (anti-pitfall #3: points[0]).

    'desc' = newest first (kline/moneyflow), 'asc' = oldest first (valuation).
    We assert BEFORE reversing so a silent upstream change fails loud.
    """
    s = pd.to_datetime(pd.Series(list(series)))
    if expected == "desc" and not (s.iloc[0] >= s.iloc[-1]):
        raise NormalizeError(f"{label}: expected desc series but got {s.iloc[0]} -> {s.iloc[-1]}")
    if expected == "asc" and not (s.iloc[0] <= s.iloc[-1]):
        raise NormalizeError(f"{label}: expected asc series but got {s.iloc[0]} -> {s.iloc[-1]}")


def _dedup_dates(df: pd.DataFrame, date_col: str, label: str) -> pd.DataFrame:
    """Deduplicate rows on the date column with a hard conflict rule.

    Observed 2026-11-17 (demo kline + valuation tails): upstream can return the
    same date twice with IDENTICAL values. Identical duplicates are dropped
    (the caller reports the drop); CONFLICTING values for the same date are
    upstream corruption and raise loud — never silently keep-last (the old
    pivot_table(aggfunc='last') behaviour silently swallowed this for months).
    """
    dup_mask = df[date_col].duplicated(keep=False)
    if not dup_mask.any():
        return df
    conflicts: list = []
    for d in df.loc[dup_mask, date_col].unique():
        if df.loc[df[date_col] == d].drop_duplicates().shape[0] > 1:
            conflicts.append(d)
    if conflicts:
        raise NormalizeError(
            f"{label}: conflicting duplicate rows for dates "
            f"{sorted(map(str, conflicts))[:3]} (upstream corruption; "
            f"never silently keep-last)"
        )
    return df.drop_duplicates(subset=[date_col], keep="last").reset_index(drop=True)


def ascending_frame(
    df: pd.DataFrame, date_col: str = "date", *, label: str = "panel"
) -> pd.DataFrame:
    """Return a copy sorted oldest -> newest with normalized date strings.

    Identical-value duplicate dates are dropped; conflicting duplicates raise
    (see _dedup_dates). Callers compare row counts before/after to warn.
    """
    out = df.copy()
    out[date_col] = [_coerce_date(v).isoformat() for v in out[date_col]]
    out = _dedup_dates(out, date_col, label)
    out = out.sort_values(date_col).reset_index(drop=True)
    return out


def kline_to_frame(payload: Any, *, symbol: str | None = None) -> pd.DataFrame:
    """Normalize kline.bars result to a clean ascending DataFrame.

    Handles both DataFrame (_df=True) and list[dict] raw payloads.
    """
    if isinstance(payload, pd.DataFrame):
        df = payload.copy()
    elif isinstance(payload, list):
        df = pd.DataFrame(payload)
    else:
        raise NormalizeError(f"unsupported kline payload type: {type(payload).__name__}")
    need = {"date", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        raise NormalizeError(f"kline payload missing {missing}")
    if symbol is not None:
        df["symbol"] = symbol
    # Guard the source direction before we normalize to ascending.
    assert_series_order(df["date"], "desc", f"kline.bars[{symbol}]")
    return ascending_frame(df, label=f"kline.bars[{symbol}]")


def valuation_series_to_frame(payload: dict[str, Any], *, symbol: str) -> pd.DataFrame:
    """Normalize valuation.hist_stats().series ([unix_ts, value]) to a frame.

    Anti-pitfall #1/#5: ok=False / empty series is returned as an EMPTY frame with
    an attribute-style marker in the caller, never as zeros.
    """
    series = payload.get("series") or []
    if not series:
        return pd.DataFrame(columns=["date", "symbol", "pe"])
    rows = [(_coerce_date(ts), float(val)) for ts, val in series if val is not None]
    df = pd.DataFrame(rows, columns=["date", "pe"])
    assert_series_order(df["date"], "asc", f"valuation.hist_stats[{symbol}]")
    df["symbol"] = symbol
    return ascending_frame(df, label=f"valuation.hist_stats[{symbol}]")


def moneyflow_to_frame(payload: Any, *, symbol: str) -> pd.DataFrame:
    if isinstance(payload, pd.DataFrame):
        df = payload.copy()
    elif isinstance(payload, list):
        df = pd.DataFrame(payload)
    else:
        raise NormalizeError(f"unsupported moneyflow payload type: {type(payload).__name__}")
    if "date" not in df.columns:
        raise NormalizeError("moneyflow payload missing 'date'")
    assert_series_order(df["date"], "desc", f"moneyflow.bars[{symbol}]")
    df["symbol"] = symbol
    return ascending_frame(df, label=f"moneyflow.bars[{symbol}]")


def merge_adjusted_klines(
    adj_df: pd.DataFrame, raw_df: pd.DataFrame, *, symbol: str
) -> pd.DataFrame:
    """Merge a back-adjusted (hfq) kline frame with its raw twin -> factor column.

    Qlib convention (verified against microsoft/qlib
    scripts/data_collector/yahoo/collector.py, YahooNormalize1d.adjusted_price,
    2026-11-17): ``factor = adjusted / raw``, the stored price fields ARE the
    adjusted values, ``raw = close / factor`` and ``volume_adj = raw / factor``.

    Probe 2026-11-17 (600519 n=10): raw and hfq bars share identical date sets;
    upstream volume is NOT adjusted (raw volume == hfq volume), so volume is
    divided by ``factor`` here - that keeps ``close * volume == amount``
    invariant across ex-dividend dates. ``amount`` stays raw turnover.

    Guards (fail loud, never fill):
    - both frames must be non-empty and cover the SAME date set (a partial
      mismatch means the two calls straddled an upstream update -> raise, the
      caller retries the symbol as a whole);
    - raw close must be finite and > 0 (a non-positive raw price cannot yield
      a factor);
    - factor must be non-decreasing along dates (hfq is IPO-anchored cumulative;
      a downward move is upstream inconsistency). The dump-time bulk backstop
      lives in panel.assert_panel_integrity.
    """
    if adj_df.empty or raw_df.empty:
        raise NormalizeError(f"kline[{symbol}]: adjusted/raw frame is empty")
    adj_dates = set(adj_df["date"])
    raw_dates = set(raw_df["date"])
    if adj_dates != raw_dates:
        only_adj = sorted(adj_dates - raw_dates)[:3]
        only_raw = sorted(raw_dates - adj_dates)[:3]
        raise NormalizeError(
            f"kline[{symbol}]: adjusted/raw date sets differ "
            f"(adj-only={only_adj}, raw-only={only_raw}); the two calls "
            f"straddled an upstream update - refetch the symbol whole"
        )
    merged = adj_df.merge(
        raw_df[["date", "close"]].rename(columns={"close": "_close_raw"}),
        on="date", how="inner", validate="one_to_one",
    )
    if len(merged) != len(adj_df):
        raise NormalizeError(
            f"kline[{symbol}]: adjusted/raw merge shrank {len(adj_df)} -> {len(merged)} rows"
        )
    raw_close = pd.to_numeric(merged["_close_raw"], errors="coerce")
    bad = (~raw_close.notna()) | (raw_close <= 0)
    if bad.any():
        d = merged.loc[bad, "date"].iloc[0]
        raise NormalizeError(
            f"kline[{symbol}]: non-positive/non-numeric raw close on {d} "
            f"(value={merged.loc[bad, '_close_raw'].iloc[0]}); factor undefined"
        )
    merged["factor"] = merged["close"].astype(float) / raw_close.astype(float)
    if "volume" in merged.columns:
        merged["volume"] = merged["volume"].astype(float) / merged["factor"]
    decreasing = merged["factor"].diff().dropna()
    bad_series = decreasing[decreasing < -1e-9]
    if not bad_series.empty:
        pos = merged.index.get_loc(bad_series.index[0])
        raise NormalizeError(
            f"kline[{symbol}]: factor decreases at {merged['date'].iloc[pos]} "
            f"({merged['factor'].iloc[pos - 1]:.6g} -> {merged['factor'].iloc[pos]:.6g}); "
            f"hfq factor is cumulative and can only jump up at ex-div dates"
        )
    return merged.drop(columns=["_close_raw"])


def guard_unit(field: str, value: float) -> float:
    """Sanity-check value magnitude against its declared unit (unit guard).

    Detects obvious scale drift e.g. amount reported in 万元 instead of 元, or a
    ratio mistakenly expressed as percent. Raises instead of emitting wrong data.
    Thresholds live in config.unit_value_range (single source of truth); the
    vectorized bulk form of the same guard runs inside
    panel.assert_panel_integrity on every dump.
    """
    unit = config.FIELD_UNITS.get(field)
    if unit is None:
        raise NormalizeError(f"field {field!r} has no unit declaration")
    lo, hi = config.unit_value_range(unit)
    if not (lo <= value <= hi):
        raise NormalizeError(
            f"field {field}={value} outside [{lo}, {hi}] for unit {unit}"
        )
    return float(value)


def annotate_missing(payload: dict[str, Any], key: str = "ok") -> bool:
    """Return True if upstream marks a payload as unavailable (cache cold / window short).

    Anti-pitfall #5: ok=False must be skipped and recorded in meta, not filled with 0.
    """
    return payload.get(key) is False
