"""Example: wmshare -> Qlib .bin -> D.features / DataHandler (line B, closed loop).

Run (with the locally isolated Qlib on PYTHONPATH):
  PYTHONPATH=src:.qlibvendor python examples/dump_to_qlib.py
"""

from __future__ import annotations

from pathlib import Path

import qlib
from qlib.data import D

from wmshare_qlib import dump, fetch, panel, qlib_io

OUT = Path("out/qlib_data")


def main() -> None:
    print("[1/4] fetch history from wmshare (demo snapshot, no credentials)")
    r = fetch.fetch_history_panel(["600519", "000858"], n=120)
    integ = panel.assert_panel_integrity(r.frame)
    print("       integrity:", integ["ok"], "| rows:", integ["rows"],
          "| fields:", integ["fields"])
    assert integ["ok"]

    print("[2/4] dump to Qlib .bin")
    manifest = dump.dump_panel(r.frame, OUT)
    print("       calendar:", manifest["calendar"], "| instruments:", manifest["instruments"])

    print("[3/4] read back with Qlib's own reader (child process, isolated)")
    checks = []
    for inst in ("SH600519", "SZ000858"):
        sub = r.frame[(r.frame.instrument == inst) & (r.frame.field == "close")]
        row = sub.sort_values("date").iloc[-1]
        checks.append((inst, "close", row["date"], float(row["value"])))
    result = qlib_io.verify_bin(OUT, checks)
    print("       verify:", result["status"], "| backend:", result.get("backend"))
    for d in result["details"]:
        print("        ", d)

    print("[4/4] D.features with custom factor $pe (Qlib native)")
    qlib.init(provider_uri=str(OUT.resolve()), region="cn")
    df = D.features(
        ["SH600519", "SZ000858"],
        ["$close", "$pe", "$volume"],
        start_time="2026-10-01",
        end_time="2026-11-16",
    )
    print("       D.features shape:", df.shape)
    print(df.tail(3).to_string())
    print("\nDONE: line B closed loop (wmshare -> .bin -> Qlib native read) OK")


if __name__ == "__main__":
    main()
