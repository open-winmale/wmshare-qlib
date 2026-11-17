"""Cross-section layer: score CSV for Qlib ScoreFileModel + incremental accumulation.

Honesty contract: a single-day cross section has NO history, cannot be trained or
backtested. We emit it as score/<date>.csv with meta history=false. Accumulated
snapshots may later be promoted to a backtestable panel once N days are available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .fetch import FetchResult


def write_score_csv(result: FetchResult, out_dir: str | Path, *, as_of: str) -> Path:
    """Write ScoreFileModel input: index=[instrument, datetime], column 'score'.

    ScoreFileModel does pd.read_csv(score_path, index_col=[0,1], parse_dates=True).
    We write with instrument,datetime as the first two columns.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{as_of}.csv"
    df = result.frame[["instrument", "datetime", "score"]].copy()
    df["instrument"] = df["instrument"].astype(str)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d")
    df.to_csv(out, index=False)

    meta = {
        "as_of": as_of,
        "history": False,
        "rows": int(len(df)),
        "mode": result.metering.mode,
        "metering": result.metering.as_meta(),
        "warnings": result.warnings,
        "note": "single-day snapshot: NOT backtestable; score sign applied per metric direction",
    }
    (out_dir / f"{as_of}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


def accumulate(snapshot: pd.DataFrame, out_dir: str | Path, *, as_of: str) -> Path:
    """Append one day's cross-section to the accumulation store (parquet)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{as_of}.parquet"
    snapshot.to_parquet(out, index=False, engine="pyarrow")
    return out


def promote_to_panel(
    accum_dir: str | Path, *, min_days: int, out_parquet: str | Path
) -> tuple[pd.DataFrame | None, str]:
    """Promote accumulated snapshots to a long panel if enough days exist.

    Returns (frame_or_None, status). Cold-start guard: below min_days we refuse to
    promote and say so explicitly (anti-pitfall #5, no fabricated history).
    """
    accum_dir = Path(accum_dir)
    files = sorted(accum_dir.glob("*.parquet"))
    if len(files) < min_days:
        return None, f"only {len(files)} days accumulated; need >= {min_days}"
    frames = [pd.read_parquet(f, engine="pyarrow") for f in files]
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(out_parquet, index=False, engine="pyarrow")
    return df, f"promoted {len(files)} days -> {out_parquet}"


def history_meta(n_days: int, threshold: int) -> dict:
    return {
        "history_days": n_days,
        "threshold": threshold,
        "backtestable": n_days >= threshold,
        "note": "below threshold: snapshot only, not backtestable" if n_days < threshold else "ok",
    }
