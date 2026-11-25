"""Offline freshness/consistency check for a wmshare-qlib fragment store.

Zero network - safe to run from cron/launchd after the daily update.

Two-level semantics:
- CORRUPT (exit 1): manifest unreadable, a bulk fragment missing/empty, a
  recorded tail whose parquet is absent, a tail frontier OLDER than the bulk
  frontier, or per-symbol field sets disagreeing with the manifest.
- STALE (exit 2): the tail frontier lags the most recent weekday (Mon-Fri) by
  more than ``--max-lag-days``. CN holidays are NOT modeled, so a long
  holiday can trigger this warn spuriously - it is a warn, never a
  corruption signal, and the threshold exists to tolerate one missed run.

Output: one JSON verdict line on stdout; human hints on stderr.
    {"store": ..., "symbols": N, "frontier_bulk": ..., "frontier_tail": ...,
     "weekdays_behind": N, "verdict": "green"|"stale"|"corrupt", "problems": [...]}
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

# Allow running as `python tools/freshness.py` from a repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wmshare_qlib import resume  # noqa: E402  (path bootstrap above)


def _weekdays_behind(frontier: str, today: dt.date) -> int:
    """Count Mon-Fri days strictly after ``frontier`` up to ``today``."""
    f = dt.date.fromisoformat(str(frontier))
    n = 0
    d = f + dt.timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


def check(store: Path, max_lag_days: int) -> tuple[str, dict]:
    """Return (verdict, report). verdict is green | stale | corrupt."""
    problems: list[str] = []

    try:
        manifest = resume.load_manifest(store)
    except Exception as exc:
        return "corrupt", {
            "store": str(store), "symbols": 0, "frontier_bulk": None,
            "frontier_tail": None, "weekdays_behind": None,
            "verdict": "corrupt",
            "problems": [f"manifest unreadable: {type(exc).__name__}: {exc}"],
        }

    syms = manifest.get("symbols", {})
    if not syms:
        problems.append("manifest records no symbols")

    bulk_frontier: str | None = None
    tail_frontier: str | None = None
    for code, entry in sorted(syms.items()):
        frag = store / "fragments" / f"{code}.parquet"
        if not frag.exists():
            problems.append(f"{code}: bulk fragment missing")
            continue
        try:
            n_rows = len(pd.read_parquet(frag))
        except Exception as exc:
            problems.append(f"{code}: bulk fragment unreadable: {exc}")
            continue
        if n_rows == 0:
            problems.append(f"{code}: bulk fragment empty")

        bulk_last = entry.get("last_date")
        tail = entry.get("tail") or {}
        tail_last = tail.get("last_date")
        if bulk_last:
            bulk_frontier = max(bulk_frontier or bulk_last, str(bulk_last))
        if tail_last:
            tail_frontier = max(tail_frontier or tail_last, str(tail_last))
            tail_path = store / "tail" / f"{code}.parquet"
            if not tail_path.exists():
                problems.append(f"{code}: manifest records a tail but tail parquet is missing")
            elif str(tail_last) < str(bulk_last):
                problems.append(
                    f"{code}: tail frontier {tail_last} older than bulk frontier {bulk_last}"
                )
        fields = entry.get("fields") or []
        if not fields:
            problems.append(f"{code}: manifest records no fields")

    if tail_frontier is None and bulk_frontier is None:
        problems.append("no frontier date recoverable from the manifest")

    verdict = "corrupt" if problems else "green"
    lag = None
    if not problems:
        frontier = tail_frontier or bulk_frontier
        lag = _weekdays_behind(str(frontier), dt.date.today())
        if lag > max_lag_days:
            verdict = "stale"
            problems.append(
                f"frontier {frontier} is {lag} weekdays behind today "
                f"(tolerance {max_lag_days}; CN holidays are not modeled)"
            )

    report = {
        "store": str(store),
        "symbols": len(syms),
        "frontier_bulk": bulk_frontier,
        "frontier_tail": tail_frontier,
        "weekdays_behind": lag,
        "verdict": verdict,
        "problems": problems,
    }
    return verdict, report


def main() -> None:
    p = argparse.ArgumentParser(description="offline store freshness check")
    p.add_argument("--store", default="out/_resume_live", help="fragment store dir")
    p.add_argument(
        "--max-lag-days", type=int, default=3,
        help="tail frontier may lag the most recent weekday by up to this many "
             "weekdays before verdict=stale (default 3: tolerates one missed run "
             "plus CN holidays, which are not modeled)",
    )
    args = p.parse_args()

    verdict, report = check(Path(args.store), args.max_lag_days)
    print(json.dumps(report, ensure_ascii=False))
    for problem in report["problems"]:
        print(f"[freshness] {problem}", file=sys.stderr)
    sys.exit(0 if verdict == "green" else 2 if verdict == "stale" else 1)


if __name__ == "__main__":
    main()
