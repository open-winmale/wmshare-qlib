"""Cross-section layer: score CSV for Qlib ScoreFileModel + incremental accumulation.

Honesty contract: a single-day cross section has NO history, cannot be trained or
backtested. We emit it as score/<date>.csv with meta history=false. Accumulated
snapshots form a self-built PIT series: the series is available FROM its first
captured day and grows one real day at a time (never backfilled). A configurable
cold-start guard (min_days, default config.history_days_threshold()) gates
promotion to a backtestable panel — it is a sanity guard, not a data limit: the
data itself is honest from day one, just short.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from .fetch import FetchResult

# A snapshot file is a PIT DAY only when its stem is an ISO date. Anything else
# (notably the literal ``None`` stem <= 0.7.0 produced when ``accumulate`` ran
# without --as-of) is not history: it must never count toward the promotion
# guard nor be concatenated into a backtest panel.
_DATE_STEM = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    """Append one day's cross-section to the accumulation store (parquet).

    ``as_of`` MUST be an ISO date: a date-less snapshot lands on the same file
    every day, so the store would hold one rolling day (and the promotion guard
    could never pass). The CLI defaults it to today; the library refuses None
    so the defect cannot be reintroduced silently.
    """
    if not isinstance(as_of, str) or not _DATE_STEM.fullmatch(as_of):
        raise ValueError(
            f"accumulate requires an ISO date as_of (got {as_of!r}): a date-less "
            "snapshot would overwrite one file every day and never become PIT history"
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{as_of}.parquet"
    snapshot.to_parquet(out, index=False, engine="pyarrow")
    return out


def scan_accumulation(out_dir: str | Path) -> tuple[list[Path], list[Path]]:
    """Split the accumulation store into (ISO-date snapshots, ignored files).

    Both lists are sorted by name; ISO stems sort chronologically. Non-date
    files are reported, never used -- a stray ``None.parquet`` from an older
    version must not inflate ``history_days`` or poison the promoted panel.
    """
    files = sorted(Path(out_dir).glob("*.parquet"))
    kept = [f for f in files if _DATE_STEM.fullmatch(f.stem)]
    ignored = [f for f in files if not _DATE_STEM.fullmatch(f.stem)]
    return kept, ignored


def promote_to_panel(
    accum_dir: str | Path, *, min_days: int, out_parquet: str | Path
) -> tuple[pd.DataFrame | None, str]:
    """Promote accumulated snapshots to a long panel if enough days exist.

    Returns (frame_or_None, status). Cold-start guard: below min_days we refuse to
    promote and say so explicitly (anti-pitfall #5, no fabricated history).
    Only ISO-date files are days; ignored files are named in the status.
    """
    accum_dir = Path(accum_dir)
    files, ignored = scan_accumulation(accum_dir)
    suffix = f" (ignored {len(ignored)} non-date file(s))" if ignored else ""
    if len(files) < min_days:
        return None, f"only {len(files)} days accumulated; need >= {min_days}{suffix}"
    frames = [pd.read_parquet(f, engine="pyarrow") for f in files]
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(out_parquet, index=False, engine="pyarrow")
    return df, f"promoted {len(files)} days -> {out_parquet}{suffix}"


def history_meta(n_days: int, threshold: int, *, available_from: str | None = None) -> dict:
    """PIT self-built series status (available-from semantics).

    ``available_from`` is the earliest captured snapshot date (see
    ``scan_accumulation``: only ISO-date files are days). The guard
    ``threshold`` is configurable and only gates PROMOTION - the series itself
    is honest (real captured values) from day one, just short.
    """
    promoted = n_days >= threshold
    return {
        "history_days": n_days,
        "threshold": threshold,
        "available_from": available_from,
        "backtestable": promoted,
        "note": (
            f"PIT self-built series: available from {available_from or 'n/a'}; "
            + (
                f"promotion guard passed ({n_days} days >= {threshold})"
                if promoted
                else f"{n_days} real day(s) accumulated; promotion (backtest "
                     f"panel export) waits for the configurable guard of "
                     f"{threshold} days - data is honest, just short"
            )
        ),
    }
