"""M5 tests: wmshare panel -> vnpy.alpha injection (zero EC, demo snapshot).

Tiered like the rest of the suite:
  * Pure mapping/shape tests always run (polars only, no vnpy import).
  * The AlphaDataset roundtrip test is skipped when vnpy is absent, so the
    default CI path stays green without the optional dependency.
"""

from __future__ import annotations

import pytest

import wmshare_qlib.vnpy_alpha as va

SYMBOLS = ["600519", "000858"]


@pytest.fixture(scope="module")
def demo_panel():
    from wmshare_qlib import fetch

    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    return r.frame


# --- vt_symbol mapping (pure) -------------------------------------------------
def test_to_vt_symbol_mapping():
    assert va.to_vt_symbol("SH600519") == "600519.SSE"
    assert va.to_vt_symbol("SZ000858") == "000858.SZSE"
    assert va.to_vt_symbol("BJ832000") == "832000.BSE"


def test_to_vt_symbol_rejects_unknown():
    for bad in ("XX600519", "600519", "SH", "SHabcd"):
        with pytest.raises(va.VnpyAdapterError):
            va.to_vt_symbol(bad)


# --- result frame contract (polars only) --------------------------------------
def test_result_frame_shape_contract(demo_panel):
    pl = va._require_polars()
    f = va.panel_to_alpha_result(demo_panel, "close", pl)
    assert f.columns == ["datetime", "vt_symbol", "data"]
    # one row per (datetime, vt_symbol)
    assert f.height == f.unique(subset=["datetime", "vt_symbol"]).height


def test_result_frame_rejects_missing_field(demo_panel):
    pl = va._require_polars()
    with pytest.raises(va.VnpyAdapterError, match="absent from panel"):
        va.panel_to_alpha_result(demo_panel, "not_a_field", pl)


def test_result_frame_rejects_duplicates(demo_panel):
    pl = va._require_polars()
    import pandas as pd

    # Duplicate the whole panel: every (datetime, vt_symbol) of 'close' now
    # appears twice - the adapter must refuse instead of fanning out the join.
    dup_panel = pd.concat([demo_panel, demo_panel], ignore_index=True)
    with pytest.raises(va.VnpyAdapterError, match="duplicate"):
        va.panel_to_alpha_result(dup_panel, "close", pl)


def test_base_frame_contract(demo_panel):
    pl = va._require_polars()
    base = va.panel_to_alpha_base(demo_panel, pl)
    for col in ("datetime", "vt_symbol", "open", "high", "low", "close", "volume", "amount"):
        assert col in base.columns, col
    assert base.schema["datetime"] == pl.Datetime
    assert {s.split(".")[1] for s in base["vt_symbol"].unique().to_list()} <= {"SSE", "SZSE"}


def test_base_frame_missing_close_fails_loud(demo_panel):
    pl = va._require_polars()
    pruned = demo_panel[demo_panel["field"] != "close"]
    with pytest.raises(va.VnpyAdapterError, match="missing from panel"):
        va.panel_to_alpha_base(pruned, pl)


# --- full roundtrip with real vnpy (optional tier) -----------------------------
def test_alpha_dataset_roundtrip(demo_panel):
    pytest.importorskip("vnpy")
    ds, Segment = va.build_alpha_dataset(
        demo_panel,
        feature_fields=("pe", "net_mf"),
        label_field=None,
        train_period=("2000-01-01", "2026-12-31"),
        valid_period=("2027-01-01", "2027-12-31"),
        test_period=("2028-01-01", "2028-12-31"),
    )
    report = va.assert_dataset_roundtrip(ds, Segment, demo_panel, ("pe", "net_mf"))
    assert report["ok"], report
    for f, stats in report["fields"].items():
        assert stats["mismatches"] == 0 and stats["phantom"] == 0, (f, stats)


def test_feature_name_clash_with_base_frame_rejected(demo_panel):
    """Injecting 'close'/'volume' (already base columns) corrupts prepare_data's
    column selection (duplicate-name join) - must be rejected up front."""
    pytest.importorskip("vnpy")
    with pytest.raises(va.VnpyAdapterError, match="already exist in the vnpy base"):
        va.build_alpha_dataset(demo_panel, feature_fields=("close", "pe"))


def test_label_and_feature_collision_rejected(demo_panel):
    with pytest.raises(va.VnpyAdapterError, match="must differ"):
        va.build_alpha_dataset(
            demo_panel,
            feature_fields=("close", "pe"),
            label_field="pe",
        )
