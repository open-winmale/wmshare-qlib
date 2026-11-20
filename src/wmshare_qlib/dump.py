"""Qlib-compatible .bin dumper (Qlib-free; contract lives in format.py).

This module writes the on-disk layout that Qlib's
``qlib.data.storage.file_storage.FileFeatureStorage`` reads::

  calendars/day.txt          one YYYY-MM-DD per line (ascending)
  instruments/all.txt        <symbol>\\t<start>\\t<end>
  features/<inst>/<field>.<freq>.bin
      = np.hstack([start_index, calendar_aligned_values]).astype("<f")

The binary contract is centralized in format.py (single source of truth) so a
future Qlib format change is confined to that file. Read-back verification with
Qlib's own reader is delegated to qlib_io.verify_bin(), which runs in a child
process and never imports Qlib into this pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import format


def _append_vwap(panel: pd.DataFrame) -> pd.DataFrame:
    """Derive ``vwap = amount / volume`` rows (dump-time, single point).

    Qlib's bundled cn_data ships a ``$vwap`` field and Alpha158 consumes it; the
    upstream kline surface has no vwap (verified 2026-11-20: bar rows carry
    open/high/low/close/volume/amount/percent only). The stored ``volume`` is
    ALREADY divided by the adjustment factor (see config ADJUSTMENT_MODES notes),
    so ``amount / volume`` here is consistent with the adjusted (hfq) ``close``
    and satisfies the same ``close * volume == amount`` invariant.

    Rows with non-positive or missing volume (suspension days) are skipped -
    the calendar reindex in dump_panel turns that into NaN, i.e. honest missing
    data, never zeros. Called from dump_panel so bulk dumps, incremental
    re-dumps and old fragment stores (which lack a vwap column) all emit it.
    """
    fields = set(panel["field"].unique())
    if not {"amount", "volume"}.issubset(fields):
        return panel
    amount = panel.loc[panel["field"] == "amount", ["date", "instrument", "value"]]
    volume = panel.loc[panel["field"] == "volume", ["date", "instrument", "value"]]
    merged = amount.merge(volume, on=["date", "instrument"], suffixes=("_amount", "_volume"))
    merged = merged[merged["value_volume"] > 0]
    if merged.empty:
        return panel
    derived = pd.DataFrame(
        {
            "date": merged["date"],
            "instrument": merged["instrument"],
            "field": "vwap",
            "value": merged["value_amount"] / merged["value_volume"],
        }
    )
    return pd.concat([panel, derived], ignore_index=True)


def build_calendar(panel: pd.DataFrame) -> list[str]:
    return sorted(panel["date"].unique().tolist())


def write_calendar(qlib_dir: Path, calendar: list[str]) -> None:
    d = qlib_dir / "calendars"
    d.mkdir(parents=True, exist_ok=True)
    (d / "day.txt").write_text("\n".join(calendar) + "\n", encoding="utf-8")


def write_instruments(qlib_dir: Path, panel: pd.DataFrame) -> None:
    d = qlib_dir / "instruments"
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for instrument, grp in panel.groupby("instrument"):
        dates = sorted(grp["date"].unique())
        rows.append(f"{instrument}\t{dates[0]}\t{dates[-1]}")
    (d / "all.txt").write_text("\n".join(sorted(rows)) + "\n", encoding="utf-8")


def dump_panel(panel: pd.DataFrame, qlib_dir: str | Path, *, freq: str = "day") -> dict:
    """Dump a long panel [date, instrument, field, value] into Qlib .bin format.

    Returns a manifest with per-field stats for the verify step.
    """
    qlib_dir = Path(qlib_dir)
    qlib_dir.mkdir(parents=True, exist_ok=True)
    need = {"date", "instrument", "field", "value"}
    missing = need - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing columns: {missing}")

    panel = _append_vwap(panel)
    calendar = build_calendar(panel)
    cal_index = {d: i for i, d in enumerate(calendar)}
    write_calendar(qlib_dir, calendar)
    write_instruments(qlib_dir, panel)

    manifest: dict[str, object] = {
        "calendar": [calendar[0], calendar[-1], len(calendar)],
        "instruments": int(panel["instrument"].nunique()),
        "fields": {},
        "freq": freq,
        "format": format.FORMAT_CONTRACT["version"],
    }
    fields_stat: dict[str, dict] = {}

    for instrument, grp in panel.groupby("instrument"):
        inst_dates = sorted(grp["date"].unique())
        start_idx = cal_index[inst_dates[0]]
        for field, fgrp in grp.groupby("field"):
            # De-duplicate (a date may appear twice if two sources overlap),
            # then align to the FULL calendar so Qlib seek offsets are correct.
            dedup = fgrp.groupby("date", as_index=False)["value"].last()
            series = dedup.set_index("date")["value"].reindex(calendar)
            buf = format.encode_bin(start_idx, series.to_numpy(dtype="float32"))
            out = format.bin_path(qlib_dir, instrument, field, freq)
            out.parent.mkdir(parents=True, exist_ok=True)
            buf.tofile(out)
            fs = fields_stat.setdefault(field, {"files": 0, "non_null": 0, "null": 0})
            fs["files"] += 1
            fs["non_null"] += int(series.notna().sum())
            fs["null"] += int(series.isna().sum())

    manifest["fields"] = fields_stat
    return manifest


def instrument_date_ranges(
    qlib_dir: str | Path, *, freq: str = "day", field: str = "close"
) -> dict[str, tuple[str, str]]:
    """Per-instrument data coverage [start, end] read from the .bin headers.

    P1-1: universe files must carry Qlib-convention membership ranges. The
    honest range is what the dump actually CONTAINS (start_index + length vs
    the global calendar) - not the full calendar, which would pretend
    suspended/late-listed instruments traded on days they did not.
    Instruments lacking the anchor field's .bin are skipped (reported by the
    caller), never fabricated.
    """
    import numpy as np

    calendar = format.read_calendar(qlib_dir, freq=freq)
    features = Path(qlib_dir) / "features"
    if not calendar or not features.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    for inst_dir in sorted(p for p in features.iterdir() if p.is_dir()):
        bin_file = inst_dir / f"{field}.{freq}.bin"
        if not bin_file.exists():
            continue
        arr = np.fromfile(bin_file, dtype="<f4")
        if arr.size < 2:
            continue
        start_index = int(arr[0])
        length = arr.size - 1
        end_index = min(start_index + length - 1, len(calendar) - 1)
        if not (0 <= start_index < len(calendar)) or start_index > end_index:
            continue
        out[inst_dir.name.upper()] = (calendar[start_index], calendar[end_index])
    return out


def write_universe_file(
    qlib_dir: str | Path,
    name: str,
    ranges: dict[str, tuple[str, str]],
) -> Path:
    """Write ``instruments/<name>.txt`` (Qlib convention: INST\\tstart\\tend).

    ``D.instruments("<name>")`` then works directly. Rows are sorted for
    deterministic diffs; the name is validated (no path tricks).
    """
    import re

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError(f"universe name {name!r} must be [A-Za-z0-9_-]+")
    inst_dir = Path(qlib_dir) / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    out = inst_dir / f"{name}.txt"
    lines = [f"{inst}\t{start}\t{end}" for inst, (start, end) in sorted(ranges.items())]
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out
