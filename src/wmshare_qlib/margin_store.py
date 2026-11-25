"""Layer-3 margin accumulation store (forward-only PIT, one parquet per day).

Same shape as ``cross_section.accumulate``: each captured day lands as
``<out>/<date>.parquet`` (long table ``date/symbol/instrument/field/value``).
A same-day re-capture overwrites that day's file (frontier semantics: the
latest capture of a day wins - mirrors the production frontier rewrite);
other days are never touched. Promotion to a Qlib .bin panel reuses
``dump.dump_panel`` directly on the concatenated days - margin fields are
declared in ``config.FIELD_UNITS``, so no new panel code exists.

PIT honesty contract: every row is a real upstream capture from its own day.
Missing days are healed ONLY via an explicit, budgeted ``fetch_margin_history``
call - never by fabricating or interpolating rows.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd


def save_margin_days(frame: pd.DataFrame, out_dir: str | Path) -> list[Path]:
    """Write one parquet per distinct date in the long frame; return paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for day, sub in frame.groupby("date", sort=True):
        path = out_dir / f"{day}.parquet"
        sub.to_parquet(path, index=False, engine="pyarrow")
        paths.append(path)
    return paths


def load_margin_panel(out_dir: str | Path) -> tuple[pd.DataFrame, dict]:
    """Concatenate all captured days into the long panel + status meta.

    The panel is honest from its first captured day (every row is a real
    upstream value); ``dump.dump_panel`` consumes it directly when a .bin
    export is wanted - there is no separate promotion gate for raw margin
    data (the 60-day guard is a score-panel concept, not a raw-data one).
    """
    out_dir = Path(out_dir)
    files = sorted(out_dir.glob("*.parquet"))
    if not files:
        empty = pd.DataFrame(columns=["date", "symbol", "instrument", "field", "value"])
        return empty, {
            "days": 0, "rows": 0, "available_from": None, "frontier": None,
            "note": "no margin days captured yet",
        }
    frames = [pd.read_parquet(f, engine="pyarrow") for f in files]
    panel = pd.concat(frames, ignore_index=True)
    meta = {
        "days": len(files),
        "rows": int(len(panel)),
        "available_from": str(files[0].stem),
        "frontier": str(files[-1].stem),
        "captured_at": dt.date.today().isoformat(),
        "note": (
            f"Layer-3 forward-only margin series: available from "
            f"{files[0].stem}; {len(files)} day(s) captured"
        ),
    }
    return panel, meta


def write_meta(out_dir: str | Path, meta: dict) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
