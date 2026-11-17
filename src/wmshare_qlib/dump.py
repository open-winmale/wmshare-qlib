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
