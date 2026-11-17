"""Offline unit tests for the normalization guards (no network, no Qlib)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from wmshare_qlib import normalize
import wmshare


@pytest.fixture(scope="module")
def client():
    return wmshare.demo_client()


def test_direction_assert_desc_ok():
    s = pd.Series(["2026-10-30", "2026-10-29", "2026-10-28"])
    normalize.assert_series_order(s, "desc", "test-desc")  # no raise


def test_direction_assert_desc_rejects_asc():
    s = pd.Series(["2026-10-28", "2026-10-29", "2026-10-30"])
    with pytest.raises(normalize.NormalizeError):
        normalize.assert_series_order(s, "desc", "test-desc")


def test_direction_assert_asc_rejects_desc():
    s = pd.Series(["2026-10-30", "2026-10-29"])
    with pytest.raises(normalize.NormalizeError):
        normalize.assert_series_order(s, "asc", "test-asc")


def test_instrument_map_uses_exchange_field(client):
    m = normalize.build_instrument_map(client)
    assert m["600519"] == "SH600519"
    assert m["000858"] == "SZ000858"
    assert all(v[:2] in {"SH", "SZ", "BJ"} for v in m.values())


def test_kline_to_frame_is_ascending(client):
    raw = client.kline.bars("600519", n=10, _df=True)
    df = normalize.kline_to_frame(raw, symbol="600519")
    dates = pd.to_datetime(df["date"])
    assert dates.iloc[0] < dates.iloc[-1]  # now oldest -> newest


def test_valuation_series_to_frame_oldest_first(client):
    payload = client.valuation.hist_stats("600519", "pe", "3y")
    df = normalize.valuation_series_to_frame(payload, symbol="600519")
    dates = pd.to_datetime(df["date"])
    assert dates.iloc[0] < dates.iloc[-1]
    assert "pe" in df.columns


def test_annotate_missing_ok_false():
    assert normalize.annotate_missing({"ok": False}) is True
    assert normalize.annotate_missing({"ok": True}) is False
    assert normalize.annotate_missing({}) is False


def test_guard_unit_ratio_range():
    assert normalize.guard_unit("pe", 21.0) == 21.0
    with pytest.raises(normalize.NormalizeError):
        normalize.guard_unit("pe", 1e6)
    with pytest.raises(normalize.NormalizeError):
        normalize.guard_unit("unknown_field", 1.0)


# --- P0-2 regression: epoch seconds are Beijing-midnight, NOT UTC ------------
def test_numeric_ts_uses_cst_not_utc():
    """Upstream valuation stamps are Beijing-midnight epochs (ts%86400==57600).

    UTC conversion shifts the date one day EARLY and silently misaligns $pe
    against $close. Observed on demo: ts=1793289600 -> UTC 2026-10-29 but the
    trading date is 2026-10-30 (matches kline). CST must win.
    """
    ts = 1793289600  # 2026-10-29 16:00 UTC == 2026-10-30 00:00 CST
    assert normalize._coerce_date(ts) == dt.date(2026, 10, 30)


def test_valuation_demo_dates_match_kline_dates(client):
    """End-to-end P0-2 guard: the newest valuation date must equal the newest
    kline trading date (same instrument, same snapshot day)."""
    k = client.kline.bars("600519", n=3, _df=True)
    newest_kline_date = str(k["date"].iloc[0])[:10]
    p = client.valuation.hist_stats("600519", "pe", "3y")
    df = normalize.valuation_series_to_frame(p, symbol="600519")
    newest_val_date = df["date"].iloc[-1]
    assert newest_val_date == newest_kline_date, (
        f"valuation date {newest_val_date} != kline date {newest_kline_date} "
        f"(timezone offset in epoch->date conversion)"
    )
