"""Margin Layer-3 accumulation: normalize, fetch, store, CLI (Tier-1, zero EC).

Contract under test (probed 2026-11-24, roadmap P2-2): margin.latest/query
rows -> long table -> one parquet per day, forward-only PIT. Backfill exists
ONLY as the explicit budgeted --catchup path.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from conftest import DATES, OfflineHistoryClient  # repo test constants/stub

from wmshare_qlib import config, fetch, margin_store, normalize

# --- normalize.margin_rows_to_frame -------------------------------------------


def test_margin_rows_to_frame_long_and_units():
    rows = pd.DataFrame({
        "trade_date": ["2026-11-20 00:00:00"],
        "security_code": ["600519"],
        "fin_buy_amt": [1_000_000.0],
        "fin_repay_amt": [900_000.0],
        "fin_balance": [50_000_000.0],
        "loan_sell_vol": [2_000.0],
        "loan_repay_vol": [1_500.0],
        "loan_balance": [300_000.0],
    })
    frame = normalize.margin_rows_to_frame(rows, symbol="600519")
    assert list(frame.columns) == ["date", "symbol", "field", "value"]
    assert set(frame["field"]) == set(config.MARGIN_FIELDS)
    assert (frame["date"] == "2026-11-20").all()
    assert frame.loc[frame.field == "fin_balance", "value"].iloc[0] == 50_000_000.0


def test_margin_rows_missing_values_absent_not_zero():
    rows = [{"trade_date": "2026-11-20", "fin_buy_amt": 100.0, "loan_balance": None}]
    frame = normalize.margin_rows_to_frame(rows, symbol="600519")
    assert list(frame["field"]) == ["fin_buy_amt"]  # honest absence, never 0


def test_margin_rows_duplicate_date_field_fails_loud():
    rows = [
        {"trade_date": "2026-11-20", "fin_buy_amt": 100.0},
        {"trade_date": "2026-11-20", "fin_buy_amt": 200.0},
    ]
    with pytest.raises(normalize.NormalizeError, match="conflicting duplicate"):
        normalize.margin_rows_to_frame(rows, symbol="600519")


def test_margin_rows_unit_guard_rejects_scale_drift():
    # shares bound is (-1e15, 1e15): an absurd magnitude means a unit/scale bug
    rows = [{"trade_date": "2026-11-20", "loan_sell_vol": 5e16}]
    with pytest.raises(normalize.NormalizeError):
        normalize.margin_rows_to_frame(rows, symbol="600519")


# --- fetch_margin_snapshot / fetch_margin_history ------------------------------


def test_fetch_margin_snapshot_long_frame_and_metering(offline_history_client):
    result = fetch.fetch_margin_snapshot(
        ["600519", "000858"], client=offline_history_client
    )
    assert set(result.frame["field"]) == set(config.MARGIN_FIELDS)
    assert set(result.frame["date"]) == {DATES[-1]}
    assert result.metering.requests == 2
    assert result.metering.auth_mode == "credentials"
    assert result.warnings == []


def test_fetch_margin_snapshot_out_of_universe_warns():
    client = OfflineHistoryClient(margin_empty=("000001",))
    result = fetch.fetch_margin_snapshot(["600519", "000001"], client=client)
    assert any("000001" in w for w in result.warnings)
    assert set(result.frame["symbol"]) == {"600519"}


def test_fetch_margin_snapshot_all_empty_fails_loud():
    client = OfflineHistoryClient(margin_empty=("600519",))
    with pytest.raises(normalize.NormalizeError, match="no rows"):
        fetch.fetch_margin_snapshot(["600519"], client=client)


def test_fetch_margin_snapshot_max_ec_ceiling(offline_history_client):
    client = offline_history_client
    client.last_ec_cost = 400.0
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_margin_snapshot(["600519"], client=client, max_ec=100.0)


def test_fetch_margin_history_requests_days_plus_two(offline_history_client):
    result = fetch.fetch_margin_history("600519", 3, client=offline_history_client)
    assert offline_history_client.margin.last_limit == 5  # days + 2 buffer
    assert set(result.frame["date"]) == set(DATES[-5:])


# --- margin_store + CLI accumulate-margin --------------------------------------


def test_margin_store_roundtrip(tmp_path, offline_history_client):
    result = fetch.fetch_margin_snapshot(
        ["600519", "000858"], client=offline_history_client
    )
    paths = margin_store.save_margin_days(result.frame, tmp_path)
    assert [p.name for p in paths] == [f"{DATES[-1]}.parquet"]
    panel, meta = margin_store.load_margin_panel(tmp_path)
    assert meta["days"] == 1
    assert meta["frontier"] == DATES[-1]
    assert len(panel) == len(result.frame)
    meta_path = margin_store.write_meta(tmp_path, meta)
    assert json.loads(meta_path.read_text())["frontier"] == DATES[-1]


def test_margin_store_empty_dir(tmp_path):
    panel, meta = margin_store.load_margin_panel(tmp_path)
    assert panel.empty and meta["days"] == 0


def test_cli_accumulate_margin_requires_max_ec(no_real_client, capsys):
    from wmshare_qlib import cli

    ns = cli.build_parser().parse_args(
        ["accumulate-margin", "--symbols", "600519"]
    )
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_accumulate_margin(ns)


def test_cli_accumulate_margin_dry_run_previews_scope(no_real_client, capsys):
    from wmshare_qlib import cli

    ns = cli.build_parser().parse_args(
        ["accumulate-margin", "--symbols", "600519", "000858", "601318",
         "--max-ec", "2000", "--dry-run"]
    )
    cli.cmd_accumulate_margin(ns)
    payload = json.loads(capsys.readouterr().out.split("[dry-run] ")[1])
    assert payload["dry_run"] is True
    assert payload["symbols"] == 3 and payload["catchup_days"] == 0
    assert "est_ec" not in payload


def test_cli_accumulate_margin_end_to_end(tmp_path, offline_history_client,
                                          monkeypatch, capsys):
    from wmshare_qlib import cli, fetch

    monkeypatch.setattr(fetch, "_client", lambda **kw: offline_history_client)
    out = tmp_path / "margin_accum"
    ns = cli.build_parser().parse_args(
        ["accumulate-margin", "--symbols", "600519", "000858", "601318",
         "--max-ec", "2000", "--out", str(out)]
    )
    cli.cmd_accumulate_margin(ns)
    texts = capsys.readouterr().out
    assert f"{DATES[-1]}.parquet" in texts
    panel, meta = margin_store.load_margin_panel(out)
    assert meta["days"] == 1
    assert set(panel["symbol"]) == {"600519", "000858", "601318"}
    # second run overwrites that day's file (frontier semantics), no dupes
    cli.cmd_accumulate_margin(ns)
    panel2, meta2 = margin_store.load_margin_panel(out)
    assert meta2["days"] == 1
    assert len(panel2) == len(panel)
