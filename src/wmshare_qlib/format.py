"""Qlib .bin on-disk contract (single source of truth) + Qlib-free self-check.

This module encodes/decodes the Qlib feature binary format WITHOUT importing
Qlib. It exists so that:

  * the binary layout lives in exactly one place (dump uses encode_bin);
  * correctness can be verified offline with pure numpy (read_bin_selfcheck),
    so the test suite does not require pyqlib to be installed;
  * a future Qlib format change is confined to this file.

The layout is the format read by Qlib 0.9.x
``qlib.data.storage.file_storage.FileFeatureStorage``::

    features/<instrument.lower()>/<field.lower()>.<freq>.bin
      = np.hstack([start_index, values_from_start_index_onward]).astype("<f")

where ``start_index`` is the position of this instrument's first date in the
global calendar, and the remaining float32 values BEGIN at that position
(``values[0]`` is ``calendar[start_index]``), ending at the instrument's last
covered date. The reader resolves calendar index ``i`` at file offset
``4 * (i - start_index) + 4``, so a payload padded with values from
``calendar[0]`` would shift the whole series. Qlib's own ``dump_bin`` writes the
same shape: the calendar is sliced to the instrument's [min, max] and paired
with ``calendar.index(min)``.

FORMAT_CONTRACT version 2.0 (2026-11-24): payload starts at ``start_index``.
Version 1.0 wrote the full calendar-aligned array (correct only when
``start_index == 0``); dumps made by <= 0.7.0 with heterogeneous instrument
start dates must be re-dumped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

FORMAT_CONTRACT: dict[str, str] = {
    "version": "2.0",
    "source": "qlib.data.storage.file_storage.FileFeatureStorage (pyqlib 0.9.x)",
    "dtype": "little-endian float32 (<f4)",
    "header": "first float32 = start_index; payload[0] is calendar[start_index]",
    "layout": "features/<instrument.lower()>/<field.lower()>.<freq>.bin",
    "calendar": "calendars/day.txt: one YYYY-MM-DD per line, ascending",
    "instruments": "instruments/all.txt: symbol\\tstart\\tend",
}


def bin_path(base_dir: str | Path, instrument: str, field: str, freq: str = "day") -> Path:
    """Resolve the Qlib feature file path for one (instrument, field)."""
    return (
        Path(base_dir)
        / "features"
        / instrument.lower()
        / f"{field.lower()}.{freq.lower()}.bin"
    )


def encode_bin(start_index: int, values: Sequence[float] | np.ndarray) -> np.ndarray:
    """Encode one instrument's series into the Qlib .bin buffer.

    ``values`` must begin at ``start_index`` -- ``values[0]`` is the value for
    ``calendar[start_index]``, and the array ends at the instrument's last
    covered date. This is exactly what Qlib's reader expects
    (``seek(4*(i-start_index)+4)``); do NOT pass a full-calendar-aligned array
    unless ``start_index == 0``. Missing days INSIDE the span must be NaN (not
    0).
    """
    arr = np.asarray(values, dtype=np.float32)
    return np.hstack([np.float32(start_index), arr]).astype("<f")


def read_bin_selfcheck(
    base_dir: str | Path,
    instrument: str,
    field: str,
    date: str,
    calendar: Sequence[str],
    *,
    freq: str = "day",
) -> float | None:
    """Read one value back with pure numpy (NO Qlib import).

    Returns None when the file is absent, the date is outside this instrument's
    covered window, or the value is NaN. A float otherwise. This is the
    Qlib-independent half of the read-back verification.
    """
    path = bin_path(base_dir, instrument, field, freq)
    if not path.exists():
        return None
    arr = np.fromfile(path, dtype="<f4")
    if arr.size < 1:
        return None
    start_index = int(arr[0])
    data = arr[1:]
    if date not in calendar:
        return None
    cal_index = list(calendar).index(date)
    if cal_index < start_index:
        return None
    pos = cal_index - start_index
    if pos >= data.size:
        return None
    value = float(data[pos])
    return value if value == value else None  # NaN -> None (never fabricate 0)


def read_calendar(base_dir: str | Path, *, freq: str = "day") -> list[str]:
    """Read the global calendar written by dump.write_calendar."""
    path = Path(base_dir) / "calendars" / f"{freq.lower()}.txt"
    return path.read_text(encoding="utf-8").split()


def decode_series(
    base_dir: str | Path,
    instrument: str,
    field: str,
    calendar: Sequence[str],
    *,
    freq: str = "day",
) -> pd.Series:
    """Decode the whole feature back into a calendar-indexed float Series.

    A missing file decodes as all-NaN (symmetric with ``read_bin_selfcheck``
    returning None) instead of raising - dumping an empty/partial directory
    stays a readable no-data state, not a crash.
    """
    path = bin_path(base_dir, instrument, field, freq)
    if not path.exists():
        return pd.Series(
            np.full(len(calendar), np.nan, dtype=np.float32),
            index=list(calendar),
            name=field,
        )
    arr = np.fromfile(path, dtype="<f4")
    start_index = int(arr[0]) if arr.size else 0
    data = arr[1:]
    values = np.full(len(calendar), np.nan, dtype=np.float32)
    end = min(len(data), len(calendar) - start_index)
    if end > 0:
        values[start_index : start_index + end] = data[:end]
    return pd.Series(values, index=list(calendar), name=field)
