"""Example: wmshare cross section -> score CSV -> ScoreFileModel (line A, closed loop).

Run:
  PYTHONPATH=src:.qlibvendor python examples/online_score.py
"""

from __future__ import annotations

from pathlib import Path

from wmshare_qlib import cross_section, fetch

DAY = "2026-11-16"
OUT = Path("out/score")


def main() -> None:
    print("[1/3] fetch today's cross section from wmshare")
    r = fetch.fetch_cross_section(as_of=DAY)
    print("       rows:", len(r.frame), "| mode:", r.metering.mode)
    for w in r.warnings:
        print("       warn:", w)

    print("[2/3] write ScoreFileModel input CSV (history=false)")
    out = cross_section.write_score_csv(r, OUT, as_of=DAY)
    print("       wrote:", out)

    print("[3/3] read back via ScoreFileModel (official or equivalent reader)")
    score = cross_section.read_score_with_qlib_scorefilemodel(out, DAY)
    print("       score series rows:", 0 if score is None else len(score))
    if score is not None:
        top = score.sort_values(ascending=False).head(5)
        print("       top-5 by score:")
        print(top.to_string())
    print("\nDONE: line A closed loop (wmshare -> score CSV -> Qlib online scoring) OK")
    print("NOTE: single-day snapshot has no history -> cannot backtest (by design).")


if __name__ == "__main__":
    main()
