"""P0 (2026-11-24): the .bin payload must START at ``start_index``.

Qlib's reader (``.qlibvendor/qlib/data/storage/file_storage.py`` 0.9.7) is::

    seek(4 * (i - start_index) + 4)      # value of calendar[i]
    len(self) = file_size // 4 - 1       # payload length
    end_index = start_index + len - 1

so ``payload[0]`` is ``calendar[start_index]`` -- the header is the ORIGIN of
the value array, not a label prefix. Qlib's own writer agrees
(``scripts/dump_bin.py``: ``data_merge_calendar`` slices the calendar to the
instrument's [min, max], then ``np.hstack([date_index, df[field]])``).

``dump_panel`` used to reindex every series onto the FULL global calendar and
still write ``start_index`` -- correct only when an instrument's first date is
calendar[0]. Any heterogeneous dump was shifted by ``start_index`` slots.
These tests encode the layout contract directly from the raw float32 array (no
same-origin oracle) and are RED on that implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wmshare_qlib import dump, format, qlib_io

# D0..D4 global calendar. A spans all of it, B starts late (D2), C stops early
# (D2) -- one instrument per misalignment shape.
CALENDAR = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
CLOSE = {
    "SH600519": dict(zip(CALENDAR, [10.0, 11.0, 12.0, 13.0, 14.0])),
    "SZ000858": dict(zip(CALENDAR[2:], [20.0, 21.0, 22.0])),
    "SH601318": dict(zip(CALENDAR[:3], [30.0, 31.0, 32.0])),
}


def _panel() -> pd.DataFrame:
    """Long panel [date, instrument, field, value] over the three shapes."""
    rows = []
    for inst, series in CLOSE.items():
        for date, value in series.items():
            rows.append({"date": date, "instrument": inst, "field": "close", "value": value})
            # amount/volume so _append_vwap fires too (it must not shift anything)
            rows.append(
                {"date": date, "instrument": inst, "field": "volume", "value": 100.0}
            )
            rows.append(
                {"date": date, "instrument": inst, "field": "amount", "value": value * 100.0}
            )
    return pd.DataFrame(rows)


def _raw(out, inst: str, field: str = "close") -> np.ndarray:
    return np.fromfile(format.bin_path(out, inst, field), dtype="<f4")


# --- the layout contract, straight from the float32 array ---------------------
def test_payload_starts_at_start_index(tmp_path):
    out = tmp_path / "qlib_data"
    dump.dump_panel(_panel(), out)

    a = _raw(out, "SH600519")
    assert int(a[0]) == 0
    assert a[1:].tolist() == pytest.approx([10.0, 11.0, 12.0, 13.0, 14.0])

    # late starter: header = calendar index 2, payload length = 3 (NOT 5 NaNs)
    b = _raw(out, "SZ000858")
    assert int(b[0]) == 2, "payload must start at the first covered date"
    assert b.size - 1 == 3, f"payload padded to the full calendar: {b.size - 1} values"
    assert b[1:].tolist() == pytest.approx([20.0, 21.0, 22.0])

    # early stopper: payload ends at its own last date, no trailing NaN tail
    c = _raw(out, "SH601318")
    assert int(c[0]) == 0
    assert c.size - 1 == 3, f"payload padded past the last covered date: {c.size - 1} values"
    assert c[1:].tolist() == pytest.approx([30.0, 31.0, 32.0])


def test_selfcheck_and_decoder_agree_for_every_start(tmp_path):
    """The Qlib-free readers implement the same formula; writer must match."""
    out = tmp_path / "qlib_data"
    dump.dump_panel(_panel(), out)
    calendar = format.read_calendar(out)

    for inst, series in CLOSE.items():
        for date, expected in series.items():
            actual = format.read_bin_selfcheck(out, inst, "close", date, calendar)
            assert actual is not None, f"{inst}/{date} reads as missing"
            assert actual == pytest.approx(expected)

        decoded = format.decode_series(out, inst, "close", calendar)
        assert decoded.loc[list(series)].tolist() == pytest.approx(list(series.values()))
        # dates outside the instrument's own span stay NaN, never fabricated
        outside = [d for d in calendar if d not in series]
        assert decoded.loc[outside].isna().all()


def test_instrument_date_ranges_report_real_coverage(tmp_path):
    """Trailing padding must not claim coverage past the last real bar."""
    out = tmp_path / "qlib_data"
    dump.dump_panel(_panel(), out)
    ranges = dump.instrument_date_ranges(out)
    assert ranges["SH600519"] == (CALENDAR[0], CALENDAR[-1])
    assert ranges["SZ000858"] == (CALENDAR[2], CALENDAR[-1])
    assert ranges["SH601318"] == (CALENDAR[0], CALENDAR[2]), (
        f"early stopper reported as {ranges['SH601318']} (trailing NaN counted as data)"
    )


def test_format_contract_declares_payload_origin():
    assert format.FORMAT_CONTRACT["version"] == "2.0"
    assert "payload" in format.FORMAT_CONTRACT["header"]


# --- re-dump hygiene: the output dir is exactly the panel --------------------
def test_redump_prunes_stale_instrument_files(tmp_path):
    """Stale files from an earlier dump are served against the NEW calendar.

    Qlib reads whatever .bin exists, so a leftover instrument from a previous
    (different-calendar) dump would silently deliver wrong dates. A re-dump must
    leave exactly the files of the panel it wrote.
    """
    out = tmp_path / "qlib_data"
    dump.dump_panel(_panel(), out)
    stale_close = format.bin_path(out, "SH601318", "close")
    assert stale_close.exists()

    kept = _panel()
    kept = kept[kept["instrument"] == "SH600519"]
    manifest = dump.dump_panel(kept, out)

    assert not stale_close.exists(), "stale .bin from the previous dump survived"
    assert not (out / "features" / "sh601318").exists(), "empty instrument dir left behind"
    # SH601318 + SZ000858 x (close, volume, amount, vwap)
    assert manifest["removed"] == 8, manifest["removed"]
    calendar = format.read_calendar(out)
    assert format.read_bin_selfcheck(out, "SH600519", "close", CALENDAR[0], calendar) == 10.0


# --- Tier 2: the real Qlib reader is the oracle -------------------------------
@pytest.mark.qlib
def test_qlib_reads_staggered_instruments_at_the_right_dates(tmp_path):
    """Read back BOTH edges of every instrument with Qlib's own reader.

    Before the fix this returns MISMATCH for the late starter (its first date
    reads calendar[0]'s NaN) -- the exact shape the old suite could not see,
    because every stub gave all instruments the same dates.
    """
    pytest.importorskip("qlib")
    out = tmp_path / "qlib_data"
    dump.dump_panel(_panel(), out)

    checks = []
    for inst, series in CLOSE.items():
        dates = sorted(series)
        checks.append((inst, "close", dates[0], series[dates[0]]))
        checks.append((inst, "close", dates[-1], series[dates[-1]]))

    result = qlib_io.verify_bin(out, checks)
    assert result["ok"] is True, result["details"]
    assert result["status"] == "passed", result
