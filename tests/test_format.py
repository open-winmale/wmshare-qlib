"""Format round-trip and the ``verify`` CLI path (offline, zero EC).

``format.decode_series`` previously had no direct coverage (only
``read_bin_selfcheck`` was exercised via dump tests); ``cmd_verify``'s
argparse/branch behavior likewise. verify_bin is stubbed so these tests stay
in Tier-1 (no pyqlib requirement, no subprocess).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wmshare_qlib import cli, format, qlib_io

CALENDAR = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"]


def _write_series(out, instrument: str, field: str, start: int, values, freq="day") -> None:
    path = format.bin_path(out, instrument, field, freq)
    path.parent.mkdir(parents=True, exist_ok=True)
    format.encode_bin(start, values).tofile(path)


# --- decode_series round-trip -------------------------------------------------


def test_decode_series_roundtrip(tmp_path):
    # close starts at calendar index 1 (day 0 uncovered -> NaN); pe covers all
    # four days with a NaN hole that must never be fabricated as 0.
    _write_series(tmp_path, "SH600519", "close", 1, [100.0, 101.0, 102.0])
    _write_series(tmp_path, "SH600519", "pe", 0, [10.0, np.nan, 11.0, 12.0])

    got_close = format.decode_series(tmp_path, "SH600519", "close", CALENDAR)
    assert got_close.name == "close"
    assert pd.isna(got_close.iloc[0])
    assert got_close.iloc[1:].tolist() == pytest.approx([100.0, 101.0, 102.0])

    got_pe = format.decode_series(tmp_path, "SH600519", "pe", CALENDAR)
    assert pd.isna(got_pe.iloc[1])
    assert got_pe.iloc[[0, 2, 3]].tolist() == pytest.approx([10.0, 11.0, 12.0])


def test_decode_series_absent_file_is_all_nan(tmp_path):
    got = format.decode_series(tmp_path, "SZ000858", "close", CALENDAR)
    assert got.isna().all()


def test_decode_series_truncated_tail_stays_nan(tmp_path):
    # File shorter than the calendar window: uncovered trailing days stay NaN.
    _write_series(tmp_path, "SH600519", "close", 1, [100.0])
    got = format.decode_series(tmp_path, "SH600519", "close", CALENDAR)
    assert got.iloc[1] == pytest.approx(100.0)
    assert got.iloc[2:].isna().all()


def test_decode_series_respects_custom_freq(tmp_path):
    _write_series(tmp_path, "SH600519", "close", 0, [7.0], freq="1min")
    got = format.decode_series(
        tmp_path, "SH600519", "close", ["2026-11-20"], freq="1min"
    )
    assert got.iloc[0] == pytest.approx(7.0)
    # the day-freq file was not touched
    assert format.decode_series(tmp_path, "SH600519", "close", CALENDAR).isna().all()


# --- cmd_verify argparse path -------------------------------------------------


def _run_verify(tmp_path, monkeypatch, capsys, *, ok, status, checks=()):
    calls: dict = {}

    def fake_verify_bin(qlib_dir, check_list):
        calls["qlib_dir"] = qlib_dir
        calls["checks"] = list(check_list)
        return {"ok": ok, "status": status, "backend": "stub", "details": [], "notes": []}

    monkeypatch.setattr(qlib_io, "verify_bin", fake_verify_bin)
    argv = ["verify", "--qlib-dir", str(tmp_path)]
    if checks:
        argv += ["--check", *checks]  # nargs="*": values are sibling argv items
    ns = cli.build_parser().parse_args(argv)
    cli.cmd_verify(ns)
    return calls


def test_verify_cli_parses_checks(tmp_path, monkeypatch, capsys):
    # --check is nargs="*": values are sibling argv items after one flag.
    # Repeating the flag itself would overwrite (plain argparse semantics) -
    # pinned in the docstring note below so a change to append semantics is a
    # conscious one.
    calls = _run_verify(
        tmp_path,
        monkeypatch,
        capsys,
        ok=True,
        status="pass",
        checks=["SH600519:close:2026-11-20:102.0", "SZ000858:pe:2026-11-23:10.5"],
    )
    assert calls["checks"] == [
        ("SH600519", "close", "2026-11-20", 102.0),
        ("SZ000858", "pe", "2026-11-23", 10.5),
    ]
    assert "PASS" in capsys.readouterr().out


def test_verify_cli_no_checks_still_calls_verify(tmp_path, monkeypatch, capsys):
    calls = _run_verify(tmp_path, monkeypatch, capsys, ok=None, status="timeout")
    assert calls["checks"] == []
    # ok=None means "could not run" - UNAVAILABLE, never a silent PASS, never a failure
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_verify_cli_mismatch_exits_1(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_verify(
            tmp_path,
            monkeypatch,
            capsys,
            ok=False,
            status="mismatch",
            checks=["SH600519:close:2026-11-20:999.0"],
        )
    assert ei.value.code == 1
    assert "FAIL" in capsys.readouterr().out
