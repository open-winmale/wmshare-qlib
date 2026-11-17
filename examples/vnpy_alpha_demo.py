"""M5: inject a wmshare history panel into vnpy.alpha (zero EC, demo data).

This example demonstrates the vnpy.alpha injection channel: a wmshare history
panel is converted to the exact contract vnpy's AlphaDataset.add_feature()
expects (pl.DataFrame[datetime, vt_symbol, data]) and read back with a two-way
reconciliation (values match, zero phantom rows).

Run:
    PYTHONPATH=src python examples/vnpy_alpha_demo.py

EC honesty: demo snapshot only (zero EC); live mode bills per symbol and the
metering surface is identical to the Qlib path.
"""

from __future__ import annotations

from wmshare_qlib import fetch
from wmshare_qlib import vnpy_alpha as va

SYMBOLS = ["600519", "000858"]


def main() -> None:
    print("[1/4] fetching demo panel (zero EC)...")
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    print(f"      rows={len(r.frame)} warnings={len(r.warnings)}")

    print("[2/4] building AlphaDataset (features: pe, net_mf)...")
    ds, Segment = va.build_alpha_dataset(
        r.frame,
        feature_fields=("pe", "net_mf"),
        train_period=("2000-01-01", "2026-12-31"),
        valid_period=("2027-01-01", "2027-12-31"),
        test_period=("2028-01-01", "2028-12-31"),
    )

    print("[3/4] prepare_data + fetch_learn + two-way reconciliation...")
    report = va.assert_dataset_roundtrip(ds, Segment, r.frame, ("pe", "net_mf"))
    for f, stats in report["fields"].items():
        print(f"      {f}: src={stats['src_rows']} covered={stats['covered']} "
              f"mismatches={stats['mismatches']} phantom={stats['phantom']}")
    if not report["ok"]:
        raise SystemExit(f"roundtrip FAILED: {report['issues']}")
    learn = ds.fetch_learn(Segment.TRAIN)
    print(f"      learn shape={learn.shape} columns={learn.columns}")

    print("[4/4] OK -", va.snapshot_note())


if __name__ == "__main__":
    main()
