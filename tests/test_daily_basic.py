"""daily_basic integration tests (upstream-gated materialized wide table).

Zero real EC: local stubs, param-sensitive (stub discipline in
test_resume.py). Covers the three honest behaviours:
  * envelope normalize (long table, None skipped, loud on bad shapes);
  * unavailable upstream -> explicit skip warning, panel unchanged;
  * requested-and-live -> only the panel-EXTENSION fields enter the panel
    (pb/ps/pr stay single-sourced from hist_stats; Gate B owns the overlap).
Plus the Gate B reconciliation unit tests (reconcile.py).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from wmshare_qlib import config, fetch, normalize, reconcile

SYMS = ["600519"]
TODAY = "2026-11-17"
DB_FIELDS = config.PANEL_DAILY_BASIC_FIELDS


def _dates_asc(today: str, n: int) -> list[str]:
    d = dt.date.fromisoformat(today)
    return [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _db_envelope(symbol: str, days: int = 10, *, ok: bool = True,
                 values: dict[str, float | None] | None = None) -> dict:
    """Long-table envelope; values are pure functions of (date, field) and
    magnitudes match each field's declared unit (the unit guard is real)."""
    if not ok:
        return {"ok": False, "as_of": "", "data": [], "meta": {}}
    scales = {"pe_ttm": 20.0, "pb": 6.0, "ps": 12.0, "pr": 30.0,
              "total_mv": 2.69e12, "circ_mv": 2.69e12,
              "turnover_rate": 0.012, "dv_ttm": 0.031}
    rows = []
    for d in _dates_asc(TODAY, days):
        for f in DB_FIELDS:
            if values is not None and f in values:
                v = values[f]  # explicit None stays None (honest absence case)
                if v is None:
                    continue  # the row is ABSENT upstream, never zero-filled
            else:
                v = scales[f] * (1.0 + float(d[8:]) * 0.001)
            rows.append({"date": d, "symbol": symbol, "field": f, "value": float(v)})
    return {"ok": True, "as_of": _dates_asc(TODAY, days)[-1], "data": rows,
            "meta": {"fields": list(DB_FIELDS), "count": len(rows),
                     "requested_unknown": [], "db": "alt.valuation_daily"}}


class _StubDB:
    """Param-sensitive daily_basic stub: records kwargs, echoes fields back."""

    def __init__(self, *, error: Exception | None = None):
        self.calls: list[dict] = []
        self._error = error

    def daily_basic(self, symbol, begin="", end="", fields=None):
        self.calls.append({"symbol": symbol, "begin": begin, "fields": list(fields or [])})
        if self._error is not None:
            raise self._error
        return _db_envelope(symbol)


class _StubValuation:
    def hist_stats(self, symbol, metric="pe", window="3y"):
        dates = _dates_asc(TODAY, 80)
        return {"ok": True, "as_of": TODAY,
                "data": {"series": pd.DataFrame({
                    "date": dates,
                    "value": [20.0 + float(d[8:]) * 0.01 for d in dates],
                })},
                "meta": {}}


class _StubKline:
    today = TODAY

    def bars(self, symbol, n=60, adj=None):
        rows = max(1, int(n or 1))
        dates = _dates_asc(self.today, rows)
        return pd.DataFrame({
            "date": dates, "symbol": symbol,
            "close": [10.0 + float(d[8:]) * 0.1 for d in dates],
            "open": [9.5 + float(d[8:]) * 0.1 for d in dates],
            "high": [10.5 + float(d[8:]) * 0.1 for d in dates],
            "low": [9.0 + float(d[8:]) * 0.1 for d in dates],
            "volume": [100.0 + float(d[8:]) for d in dates],
            "amount": [1050.0 + float(d[8:]) for d in dates],
        })


class _StubStocks:
    def list(self):
        return pd.DataFrame({"symbol": SYMS, "exchange": ["sh"]})


class _StubClient:
    def __init__(self, db: _StubDB | None = None):
        self.kline = _StubKline()
        self.valuation = _StubValuation()
        self.stocks = _StubStocks()
        self._db = db or _StubDB()
        self.valuation.daily_basic = self._db.daily_basic  # wire the same recorder
        self.last_ec_cost = 1.0


# ---- normalize.envelope ----

def test_envelope_long_table_roundtrip():
    df = normalize.daily_basic_to_frame(_db_envelope("600519"), symbol="600519")
    assert list(df.columns) == ["date", "symbol", "field", "value"]
    assert set(df["field"]) == set(DB_FIELDS)
    assert df["date"].is_monotonic_increasing  # oldest-first rows kept in order


def test_envelope_ok_false_is_empty_with_columns():
    df = normalize.daily_basic_to_frame(_db_envelope("600519", ok=False), symbol="600519")
    assert df.empty and list(df.columns) == ["date", "symbol", "field", "value"]


def test_envelope_missing_data_loud():
    bad = {"ok": True, "as_of": TODAY}  # no data key
    with pytest.raises(normalize.NormalizeError, match="envelope"):
        normalize.daily_basic_to_frame(bad, symbol="600519")


def test_envelope_bad_row_loud_and_none_skipped():
    bad = {"ok": True, "as_of": TODAY, "data": [{"date": TODAY, "symbol": "600519",
                                                 "field": "pe_ttm"}]}  # no value key
    with pytest.raises(normalize.NormalizeError, match="missing"):
        normalize.daily_basic_to_frame(bad, symbol="600519")
    # None value: honest absence, row skipped (not zero-filled)
    env = _db_envelope("600519", values={"total_mv": None})
    df = normalize.daily_basic_to_frame(env, symbol="600519")
    assert (df["field"] != "total_mv").all()


def test_envelope_unit_guard_trips():
    # turnover_rate is pct01: 25.0 means percent-scale leak -> loud, never enters Qlib
    env = _db_envelope("600519", values={"turnover_rate": 25.0})
    with pytest.raises(normalize.NormalizeError, match="outside"):
        normalize.daily_basic_to_frame(env, symbol="600519")


# ---- fetch path ----

def _run_panel(stub, fields):
    return fetch.fetch_history_panel(
        SYMS, n=10, client=stub, fields=fields, adjust_mode="hfq_factor",
    )


def test_fetch_unavailable_skips_with_warning():
    from wmshare.errors import UsageError

    db = _StubDB(error=UsageError("valuation_daily not deployed yet",
                                  code="catalog"))
    result = _run_panel(_StubClient(db=db), config.HISTORY_FIELDS + DB_FIELDS)
    assert len(db.calls) == 1  # requested -> exactly one call attempt
    assert any("daily_basic: unavailable" in w for w in result.warnings)
    panel_fields = set(result.frame["field"])
    assert not panel_fields & set(DB_FIELDS)  # panel unchanged


def test_fetch_not_requested_never_calls():
    db = _StubDB()
    _run_panel(_StubClient(db=db), config.HISTORY_FIELDS)
    assert db.calls == []  # param-sensitive: no request without the flag's fields


def test_fetch_live_extends_panel_with_extension_fields_only():
    db = _StubDB()
    result = _run_panel(_StubClient(db=db), config.HISTORY_FIELDS + DB_FIELDS)
    assert len(db.calls) == 1
    assert db.calls[0]["fields"] == list(DB_FIELDS)  # asks ONLY extension fields
    panel_fields = set(result.frame["field"])
    assert set(DB_FIELDS) & panel_fields == set(DB_FIELDS)
    # pb/ps/pr come from the hist_stats path only (single-sourced)
    pe_sources = result.frame[result.frame["field"].isin(("pe", "pe_ttm"))]
    assert set(pe_sources["field"]) == {"pe", "pe_ttm"}


# ---- Gate B reconciliation ----

def _long_frame(rows):
    return pd.DataFrame(rows, columns=["date", "instrument", "field", "value"])


def test_reconcile_pass_and_fail():
    acc = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0),
                       ("2026-11-14", "SH600519", "total_mv", 2.69e12)])
    ups = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0),
                       ("2026-11-14", "SH600519", "total_mv", 2.69e12)])
    report = reconcile.reconcile_daily_basic(acc, ups)
    assert report.ok and report.n_compared == 2
    ups2 = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0),
                        ("2026-11-14", "SH600519", "total_mv", 2.68e12)])
    bad = reconcile.reconcile_daily_basic(acc, ups2)
    assert not bad.ok and bad.mismatches[0]["field"] == "total_mv"


def test_reconcile_missing_sides_are_informational():
    acc = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0)])
    ups = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0),
                       ("2026-11-15", "SH600519", "pe_ttm", 21.5)])
    report = reconcile.reconcile_daily_basic(acc, ups)
    assert report.ok  # missing accumulated = lag, not disagreement
    assert report.n_missing_accumulated == 1 and report.n_compared == 1


def test_reconcile_duplicate_keys_rejected():
    acc = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0),
                       ("2026-11-14", "SH600519", "pe_ttm", 21.0)])
    ups = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0)])
    with pytest.raises(ValueError, match="duplicate"):
        reconcile.reconcile_daily_basic(acc, ups)


def test_reconcile_empty_overlap_is_not_a_pass():
    """Zero comparisons = absence of evidence, never 100% agreement.

    ``ok`` used to be ``n_mismatch == 0``, so a disjoint pair (or a
    budget-truncated pull that produced no rows) printed PASS.
    """
    acc = _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0)])
    ups = _long_frame([("2026-11-20", "SH600519", "pe_ttm", 21.0)])
    report = reconcile.reconcile_daily_basic(acc, ups)
    assert report.n_compared == 0
    assert not report.ok
    assert report.as_meta()["ok"] is False


def test_cli_gate_b_empty_overlap_fails_loud(tmp_path, capsys):
    from wmshare_qlib import cli

    acc = tmp_path / "acc.parquet"
    ups = tmp_path / "ups.parquet"
    _long_frame([("2026-11-14", "SH600519", "pe_ttm", 21.0)]).to_parquet(acc, index=False)
    _long_frame([("2026-11-20", "SH600519", "pe_ttm", 21.0)]).to_parquet(ups, index=False)
    ns = cli.build_parser().parse_args(
        ["reconcile-daily-basic", "--accumulated", str(acc), "--upstream", str(ups)]
    )
    with pytest.raises(SystemExit, match="empty comparison"):
        cli.cmd_reconcile_daily_basic(ns)
    assert "PASS" not in capsys.readouterr().out


def test_cli_gate_b_truncated_pull_is_incomplete_not_pass(tmp_path, monkeypatch, capsys):
    """Budget truncation must not print PASS: the gate only certifies the
    symbols actually compared."""
    import json

    from wmshare_qlib import cli, resume

    store = tmp_path / "store"
    manifest = {
        "version": 1,
        "symbols": {
            "600519": {"instrument": "SH600519"},
            "000858": {"instrument": "SZ000858"},
        },
        "ec_spent": 0.0,
        "params": {},
    }
    (store / "fragments").mkdir(parents=True)
    (store / resume.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    rows = [
        ("2026-11-20", "600519", "SH600519", "pe_ttm", 21.0),
        ("2026-11-20", "000858", "SZ000858", "pe_ttm", 15.0),
    ]
    panel = pd.DataFrame(rows, columns=["date", "symbol", "instrument", "field", "value"])
    for symbol in ("600519", "000858"):
        panel[panel["symbol"] == symbol].to_parquet(
            store / "fragments" / f"{symbol}.parquet", index=False
        )

    values = {"600519": 21.0, "000858": 15.0}

    def fake_db(client, symbol, instrument, fields, meter, begin=None):
        meter()
        return (
            [
                pd.DataFrame(
                    {
                        "date": ["2026-11-20"],
                        "symbol": [symbol],
                        "instrument": [instrument],
                        "field": ["pe_ttm"],
                        "value": [values[symbol]],
                    }
                )
            ],
            [],
            1,
        )

    class _Client:
        last_ec_cost = 5_000.0
        auth_mode = "credentials"

    monkeypatch.setattr(fetch, "_client", lambda **kw: _Client())
    monkeypatch.setattr(fetch, "_daily_basic_frames", fake_db)
    ns = cli.build_parser().parse_args(
        [
            "reconcile-daily-basic",
            "--from-upstream",
            "--store",
            str(store),
            "--max-ec",
            "4000",  # one symbol fits; the second is truncated
        ]
    )
    with pytest.raises(SystemExit, match="INCOMPLETE"):
        cli.cmd_reconcile_daily_basic(ns)
    assert "PASS" not in capsys.readouterr().out
