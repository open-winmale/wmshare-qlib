"""vnpy.alpha injection adapter (M5) — the ONLY module that imports vnpy.

Mirrors the qlib_io.py single-point import discipline: the rest of the adapter
stays vnpy-free; this module converts a wmshare history panel into the exact
shapes vnpy.alpha consumes, with EC metering passed through and demo data
validated end-to-end at zero EC.

Contract (verified against vnpy master source, vnpy/alpha/dataset/template.py,
2026-11-17):
  * ``AlphaDataset.add_feature(name, expression=None, result=None)`` — exactly
    one of expression/result. A RESULT feature DataFrame must carry the columns
    ``[datetime, vt_symbol, data]``; the ``data`` column is renamed to ``name``
    and left-joined on ``["datetime", "vt_symbol"]`` during ``prepare_data()``.
  * The base frame (``AlphaDataset(df=...)``) needs at least ``datetime``,
    ``vt_symbol`` and ``close`` (close is used by factor-performance views).
  * ``vt_symbol`` = ``f"{code}.{exchange}"`` using vnpy ``Exchange`` values:
    SH -> SSE, SZ -> SZSE, BJ -> BSE.
  * ``prepare_data()`` runs a spawn-multiprocessing pool for expression
    features; result features bypass the pool entirely (pure join).

M5 gate: a wmshare demo panel -> AlphaDataset -> prepare_data() -> fetch_learn()
roundtrip with values matching the panel, at zero EC.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

import pandas as pd

from . import panel as panel_mod

# vnpy Exchange values per wmshare exchange suffix (config.EXCHANGE_SUFFIX keys
# are lowercase 'sh'/'sz'/'bj'; qlib instruments carry the uppercase prefix).
VNPY_EXCHANGE: dict[str, str] = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}

# Fields emitted into the vnpy base frame. 'close' is required by vnpy's
# factor-performance views; the rest are the standard OHLCV set we already dump.
BASE_FRAME_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount")


class VnpyAdapterError(ValueError):
    """Raised when a conversion violates the vnpy.alpha contract."""


def to_vt_symbol(qlib_instrument: str) -> str:
    """Qlib instrument (SH600519) -> vnpy vt_symbol (600519.SSE).

    Raises loud on unknown exchange prefixes instead of guessing.
    """
    s = str(qlib_instrument).strip().upper()
    prefix, code = s[:2], s[2:]
    exchange = VNPY_EXCHANGE.get(prefix)
    if exchange is None or not code.isdigit():
        raise VnpyAdapterError(
            f"cannot map instrument {qlib_instrument!r} to a vnpy vt_symbol "
            f"(expected SH/SZ/BJ prefix + numeric code)"
        )
    return f"{code}.{exchange}"


def _require_polars() -> Any:
    """Import polars lazily with a clear message (vnpy optional dependency)."""
    try:
        import polars as pl

        return pl
    except Exception as exc:  # pragma: no cover - dependency optional
        raise VnpyAdapterError(
            f"polars is required for the vnpy.alpha adapter ({exc}); "
            f"install with: pip install 'wmshare-qlib[vnpy]' or pip install polars"
        ) from exc


def _subset(panel: pd.DataFrame, field: str) -> pd.DataFrame:
    sub = panel[panel["field"] == field]
    if sub.empty:
        raise VnpyAdapterError(
            f"field {field!r} absent from panel; cannot build vnpy frame"
        )
    return sub


def panel_to_alpha_result(
    panel: pd.DataFrame, field: str, pl: Any
) -> Any:
    """One field of the long panel -> pl.DataFrame[datetime, vt_symbol, data].

    This is the exact shape ``AlphaDataset.add_feature(name, result=...)``
    expects (verified: 'data' is renamed to the feature name on join).
    """
    sub = _subset(panel, field)
    pdf = pd.DataFrame(
        {
            "datetime": pd.to_datetime(sub["date"]),
            "vt_symbol": sub["instrument"].map(to_vt_symbol),
            "data": sub["value"].astype(float),
        }
    )
    # Same (datetime, vt_symbol) must appear at most once - a duplicate would
    # fan out the left join and silently inflate the dataset.
    dup = pdf.duplicated(["datetime", "vt_symbol"]).sum()
    if dup:
        raise VnpyAdapterError(
            f"field {field!r}: {int(dup)} duplicate (datetime, vt_symbol) rows; "
            f"panel is not a valid wide table"
        )
    return pl.from_pandas(pdf).sort(["datetime", "vt_symbol"])


def panel_to_alpha_base(
    panel: pd.DataFrame, pl: Any, fields: Iterable[str] = BASE_FRAME_FIELDS
) -> Any:
    """Long panel -> vnpy BASE frame pl.DataFrame[datetime, vt_symbol, ...fields].

    Missing fields are reported, never silently dropped (anti-pitfall: vnpy's
    performance views need 'close'; a base frame without it fails late and far
    from the cause).
    """
    fields = tuple(fields)
    wide = panel_mod.to_wide(panel)  # [date, instrument, field1, field2, ...]
    missing = [f for f in fields if f not in wide.columns]
    if missing:
        raise VnpyAdapterError(
            f"base frame fields missing from panel: {missing} "
            f"(present: {sorted(c for c in wide.columns if c not in ('date', 'instrument'))})"
        )
    out = wide[["date", "instrument", *fields]].rename(
        columns={"date": "datetime", "instrument": "vt_symbol"}
    )
    out["vt_symbol"] = out["vt_symbol"].map(to_vt_symbol)
    out["datetime"] = pd.to_datetime(out["datetime"])
    # Base frame = rows with actual PRICE data (kline dates). Panel rows that
    # only carry valuation/moneyflow history extend far beyond the price
    # calendar and would bloat the base frame with all-NaN OHLCV rows.
    out = out[out["close"].notna()]
    frame = pl.from_pandas(out).sort(["datetime", "vt_symbol"])
    # Datetime column must be a real Datetime type: vnpy compares
    # pl.col('datetime') >= python datetime during time queries.
    if frame.schema["datetime"] != pl.Datetime:
        frame = frame.with_columns(pl.col("datetime").cast(pl.Datetime("us")))
    return frame


def build_alpha_dataset(
    panel: pd.DataFrame,
    *,
    feature_fields: Iterable[str],
    label_field: str | None = None,
    train_period: tuple[str, str] = ("2020-01-01", "2024-12-31"),
    valid_period: tuple[str, str] = ("2025-01-01", "2025-06-30"),
    test_period: tuple[str, str] = ("2025-07-01", "2026-12-31"),
) -> Any:
    """Build a vnpy AlphaDataset from a wmshare history panel.

    Features AND the optional label are injected as RESULT features (not
    expressions) - result features bypass vnpy's expression engine entirely,
    so the gate is about DATA correctness, not expression dialects. Note that
    a result-based 'label' keeps AlphaDataset.label_expression empty; vnpy's
    show_feature_performance() needs an expression label and is out of scope
    for the M5 data gate (documented, not hidden).
    """
    pl = _require_polars()
    try:
        from vnpy.alpha.dataset import AlphaDataset  # vnpy single-point import
        from vnpy.alpha.dataset.template import Segment
    except Exception as exc:  # pragma: no cover - dependency optional
        raise VnpyAdapterError(
            f"vnpy is required for build_alpha_dataset ({exc}); "
            f"install with: pip install vnpy"
        ) from exc

    if label_field is not None and label_field in set(feature_fields):
        raise VnpyAdapterError(
            f"label field {label_field!r} must differ from the feature fields"
        )
    clash = [f for f in feature_fields if f in BASE_FRAME_FIELDS]
    if clash:
        raise VnpyAdapterError(
            f"feature fields {clash} already exist in the vnpy base frame; "
            f"inject only NEW factor columns - a duplicate-name result join "
            f"corrupts prepare_data's column selection (verified 2026-11-17)"
        )
    base = panel_to_alpha_base(panel, pl)
    ds = AlphaDataset(base, train_period, valid_period, test_period)
    for f in feature_fields:
        ds.add_feature(f, result=panel_to_alpha_result(panel, f, pl))
    if label_field is not None:
        ds.add_feature("label", result=panel_to_alpha_result(panel, label_field, pl))
    return ds, Segment


def assert_dataset_roundtrip(
    ds: Any, Segment: Any, panel: pd.DataFrame, fields: Iterable[str]
) -> dict:
    """M5 gate: prepare + fetch_learn must return injected values EXACTLY.

    Two-way reconciliation (request-response discipline):
      * on the INTERSECTION of source keys and learn keys, values must match;
      * every learn row carrying the feature must exist in the source panel
        (zero phantom rows - vnpy must never fabricate a value we didn't emit);
      * coverage (src keys present in learn) is reported, not enforced, because
        feature history legitimately extends beyond the base price calendar
        (e.g. pe 3y vs n-day kline) and TRAIN-period filtering applies.
    """
    ds.prepare_data()
    learn = ds.fetch_learn(Segment.TRAIN)
    report: dict[str, Any] = {"ok": True, "issues": [], "fields": {}}
    learn_dates = [d.date().isoformat() for d in learn["datetime"].to_list()]
    learn_keys = list(zip(learn["vt_symbol"].to_list(), learn_dates))
    for f in fields:
        if f not in learn.columns:
            report["ok"] = False
            report["issues"].append(f"feature {f!r} absent from fetch_learn output")
            continue
        sub = _subset(panel, f)
        src = {
            (to_vt_symbol(str(inst)), d): float(v)
            for inst, d, v in zip(sub["instrument"], sub["date"], sub["value"])
        }
        values = learn[f].to_list()
        mismatches: list[str] = []
        phantom: list[str] = []
        covered = 0
        for key, actual in zip(learn_keys, values):
            if actual is None or pd.isna(actual):
                continue  # left-join miss on a date outside the feature series
            expected = src.get(key)
            if expected is None:
                phantom.append(f"{key}: learn has {f}={actual} but panel has no such row")
                continue
            covered += 1
            if abs(float(actual) - expected) > max(1e-6, abs(expected) * 1e-9):
                mismatches.append(f"{key}: {actual} != {expected}")
        if mismatches or phantom:
            report["ok"] = False
            report["issues"].extend((mismatches + phantom)[:5])
        report["fields"][f] = {
            "src_rows": len(src),
            "covered": covered,
            "mismatches": len(mismatches),
            "phantom": len(phantom),
        }
    return report


def snapshot_note() -> str:
    """Honest provenance note for demo-mode datasets (never claim live)."""
    return (
        f"wmshare demo snapshot panel (zero EC, mode=demo); generated "
        f"{dt.date.today().isoformat()}. NOT live precomputed-metric output."
    )
