"""Example: wmshare cross section -> score CSV -> ScoreFileModel (line A, closed loop).

Run:
  pip install 'wmshare-qlib[qlib]'   # pyqlib is only needed by the qlib_io examples
  python examples/online_score.py

The snapshot fetch is LIVE: without credentials it runs on the public-pool
free trial, with credentials on your own account. Three symbols keep it tiny.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from wmshare_qlib import cross_section, fetch, qlib_io

DAY = dt.date.today().isoformat()
OUT = Path("out/score")


def main() -> None:
    print("[1/3] fetch today's cross section from wmshare (snapshot batch)")
    r = fetch.fetch_cross_section_snapshot(
        ["600519", "000858", "601318"], as_of=DAY, max_ec=1000
    )
    print("       rows:", len(r.frame), "| mode:", r.metering.mode,
          "| auth:", r.metering.auth_mode)
    for w in r.warnings:
        print("       warn:", w)

    print("[2/3] write ScoreFileModel input CSV (history=false)")
    out = cross_section.write_score_csv(r, OUT, as_of=DAY)
    print("       wrote:", out)

    print("[3/3] read back via ScoreFileModel (official or equivalent reader)")
    read = qlib_io.read_score(out, DAY)   # -> ScoreRead(series, backend, note)
    print("       backend:", read.backend, "| rows:",
          0 if read.series is None else len(read.series))
    if read.note:
        print("       note:", read.note)
    if read.series is not None and len(read.series):
        top = read.series.sort_values(ascending=False).head(5)
        print("       top-5 by score:")
        print(top.to_string())
    print("\nDONE: line A closed loop (wmshare -> score CSV -> Qlib online scoring) OK")
    print("NOTE: single-day snapshot has no history -> cannot backtest (by design).")


if __name__ == "__main__":
    main()
