"""Example: wmshare -> Qlib .bin -> D.features / DataHandler (line B, closed loop).

Run (pyqlib comes from the ``qlib`` extra):
  pip install 'wmshare-qlib[qlib]'
  python examples/dump_to_qlib.py

The fetch below is LIVE. Without credentials it runs on the
public-pool free trial (see README "Quick start A"); with credentials in
~/.wmshare/credentials.env it uses your own account. This example
deliberately keeps n small and declares a generous --max-ec ceiling.
"""

from __future__ import annotations

from pathlib import Path

import qlib
from qlib.data import D

from wmshare_qlib import dump, fetch, panel, qlib_io

OUT = Path("out/qlib_data")


def main() -> None:
    print("[1/4] fetch history from wmshare (live; free trial without credentials)")
    r = fetch.fetch_history_panel(["600519", "000858"], n=60, max_ec=20000)
    print("       metering:", r.metering.as_meta())
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

    last_date = manifest["calendar"][1]
    print("[4/4] D.features with custom factor $pe (Qlib native)")
    qlib.init(provider_uri=str(OUT.resolve()), region="cn")
    df = D.features(
        ["SH600519", "SZ000858"],
        ["$close", "$pe", "$volume"],
        start_time=manifest["calendar"][0],
        end_time=last_date,
    )
    print("       D.features shape:", df.shape)
    print(df.tail(3).to_string())
    print("\nDONE: line B closed loop (wmshare -> .bin -> Qlib native read) OK")


if __name__ == "__main__":
    main()
