"""$vwap derived-field tests (dump._append_vwap).

Qlib's bundled cn_data ships a $vwap field and Alpha158 consumes it; the
upstream kline surface does not expose one. These tests pin the derivation
contract: amount/volume on rows with positive volume, consistent with the
adjusted-price scale, NaN (not zero) on suspension days, declared in
FIELD_UNITS, and emitted by dump_panel.
"""

import pandas as pd

from wmshare_qlib import config, dump, fetch, format

SYMBOLS = ["600519"]


def _long_row(panel_long, field, instrument, date):
    sub = panel_long[
        (panel_long["field"] == field)
        & (panel_long["instrument"] == instrument)
        & (panel_long["date"] == date)
    ]
    return float(sub["value"].iloc[0]) if len(sub) else None


def test_derived_rows_equal_amount_over_volume():
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    enriched = dump._append_vwap(r.frame)
    base_rows = len(r.frame)
    derived = enriched[enriched["field"] == "vwap"]
    assert not derived.empty, "vwap rows must be derived when amount+volume exist"
    assert len(enriched) == base_rows + len(derived)
    assert not enriched.duplicated(["date", "instrument", "field"]).any()
    for _, row in derived.iterrows():
        amount = _long_row(r.frame, "amount", row["instrument"], row["date"])
        volume = _long_row(r.frame, "volume", row["instrument"], row["date"])
        assert volume and volume > 0, "vwap rows must only exist where volume > 0"
        assert abs(row["value"] - amount / volume) <= 1e-6 * max(1.0, amount / volume)


def test_vwap_within_low_high_and_adjustment_consistent(tmp_path):
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    out = tmp_path / "qlib_data"
    dump.dump_panel(r.frame, out)
    calendar = format.read_calendar(out)
    inst = "SH600519"
    dates = sorted(set(r.frame.loc[r.frame["instrument"] == inst, "date"]))
    checked = 0
    for date in dates:
        volume = _long_row(r.frame, "volume", inst, date)
        if not volume or volume <= 0:
            continue
        vwap = format.read_bin_selfcheck(out, inst, "vwap", date, calendar)
        assert vwap is not None, f"vwap missing at {date} despite positive volume"
        low = _long_row(r.frame, "low", inst, date)
        high = _long_row(r.frame, "high", inst, date)
        factor = _long_row(r.frame, "factor", inst, date)
        # low/high/vwap are all in the adjusted scale; tolerance is loose
        # because amount is trade-value (not close*volume exactly).
        assert low * factor * 0.9 <= vwap <= high * factor * 1.1, (date, vwap, low, high)
        checked += 1
    assert checked > 0
    # volume==0 rows would be NaN in the bin (honest missing), never zero-stamped


def test_vwap_nan_on_zero_volume_rows(tmp_path):
    dates = ["2026-01-05", "2026-01-06"]
    panel_long = pd.DataFrame(
        [
            {"date": d, "symbol": s, "instrument": i, "field": f, "value": v}
            for d in dates
            for s, i in (("600519", "SH600519"),)
            for f, v in (
                ("open", 10.0), ("high", 11.0), ("low", 9.5), ("close", 10.5),
                # day 1: normal volume; day 2: suspended (zero volume)
                ("volume", 1000.0 if d == dates[0] else 0.0),
                ("amount", 10500.0 if d == dates[0] else 0.0),
                ("factor", 1.0),
            )
        ]
    )
    out = tmp_path / "qlib_data"
    dump.dump_panel(panel_long, out)
    calendar = format.read_calendar(out)
    vwap_d1 = format.read_bin_selfcheck(out, "SH600519", "vwap", dates[0], calendar)
    vwap_d2 = format.read_bin_selfcheck(out, "SH600519", "vwap", dates[1], calendar)
    assert abs(vwap_d1 - 10500.0 / 1000.0) <= 1e-6
    assert vwap_d2 is None, "suspended day must stay NaN (absent), never 0"


def test_vwap_declared_and_no_phantom():
    assert "vwap" in config.FIELD_UNITS
    assert config.FIELD_UNITS["vwap"] == "CNY/share"
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    enriched = dump._append_vwap(r.frame)
    emitted = set(enriched["field"].unique())
    # Only the phantom side is asserted here: FIELD_UNITS also declares the
    # upstream-gated daily_basic family, which a plain fetch legitimately
    # does not emit. The dump pipeline must never emit an undeclared field.
    phantom = sorted(emitted - set(config.FIELD_UNITS))
    assert phantom == [], phantom
