"""P0-1 incremental tail tests (update-history). ZERO real EC: local stubs.

Stub discipline (see test_resume.py module docstring): every stub is
param-sensitive. HERE the stricter property under test is that stub VALUES are
a pure function of DATE - the bulk fetch (n=60, earlier ``today``) and the tail
fetch (n=k, later ``today``) must return IDENTICAL values on overlapping
dates, because that is precisely the reconciliation contract
``resume.load_panel_with_tail`` enforces. A position-based stub would conflict
on every overlap and prove nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

import pandas as pd
import pytest

from wmshare_qlib import cli, config, dump, fetch, format, panel, resume

SYMS = ["600519", "000858"]
TODAY_BULK = "2026-11-10"  # bulk fetch day
TODAY_TAIL = "2026-11-17"  # one week later: gap 7d -> k = 7 + overlap
EXDIV_DAY = "2026-11-13"  # hfq factor steps UP here (event applies to >= day)
KEY = ["date", "instrument", "field", "value"]


def _dates_asc(today: str, n: int) -> list[str]:
    """`n` calendar dates ENDING at `today`, OLDEST first (2.x asc)."""
    d = dt.date.fromisoformat(today)
    return [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _doy(iso: str) -> int:
    return dt.date.fromisoformat(iso).timetuple().tm_yday


def _raw_close(d: str) -> float:
    return 10.0 + _doy(d) * 0.1


# daily_basic stub values: all inside config guard_unit ranges (bad scale
# must NOT enter the panel - the stub must be a valid upstream, not a bomb)
_DB_VALUES = {
    "pe_ttm": 20.0,
    "total_mv": 1.8e12,
    "circ_mv": 1.8e12,
    "turnover_rate": 0.021,
    "dv_ttm": 0.036,
}


def _hfq_factor(d: str) -> float:
    """factor_t = product of corporate actions up to t: steps UP on ex-div day."""
    return 2.5 if d >= EXDIV_DAY else 2.0


class _StubKline:
    """Date-deterministic kline: hfq = raw x factor(date); volume unadjusted."""

    today = TODAY_BULK

    def bars(self, symbol, n=60, adj=None):
        rows = max(1, int(n or 1))
        dates = _dates_asc(self.today, rows)
        out = {"date": dates, "symbol": symbol}
        for col, base in (
            ("close", _raw_close),
            ("open", lambda d: _raw_close(d) - 0.5),
            ("high", lambda d: _raw_close(d) + 0.5),
            ("low", lambda d: _raw_close(d) - 1.0),
        ):
            vals = [base(d) * (_hfq_factor(d) if adj == "hfq" else 1.0) for d in dates]
            out[col] = vals
        # upstream volume is NOT adjusted (probe 2026-11-17) - identical both pulls
        out["volume"] = [100.0 + _doy(d) for d in dates]
        out["amount"] = [1050.0 + _doy(d) for d in dates]
        return pd.DataFrame(out)


class _StubMoneyflow:
    today = TODAY_BULK

    def bars(self, symbol, n=60):
        rows = max(1, int(n or 1))
        dates = _dates_asc(self.today, rows)
        return pd.DataFrame(
            {
                "date": dates,
                "net_mf": [60.0 + _doy(d) * 0.5 for d in dates],
                "elg_net": [30.0 + _doy(d) * 0.2 for d in dates],
                "lg_net": [20.0 + _doy(d) * 0.1 for d in dates],
                "md_net": [5.0 + _doy(d) * 0.1 for d in dates],
                "sm_net": [-5.0 + _doy(d) * 0.05 for d in dates],
            }
        )


class _StubValuation:
    """Date-deterministic series ENDING at the stub fetch day (valuation
    history can never extend past it). The window is LONGER than the kline
    anchor window (real: 3y vs n-day), otherwise the cross-field alignment
    gate would fail on stub coverage. Bulk and tail refresh agree on the
    overlap (immutable history); the tail merely extends the frontier."""

    today = TODAY_BULK

    def __init__(self) -> None:
        self.db_calls: list[dict] = []

    def daily_basic(self, symbol, begin=None, end=None, fields=None):
        """Param-sensitive daily_basic stub: rows are a pure function of
        (date, field); the request window is RECORDED so tests can assert the
        begin-windowing contract (P3-1)."""
        self.db_calls.append(
            {
                "symbol": symbol,
                "begin": begin or "",
                "fields": list(fields or []),
            }
        )
        want = list(fields or config.PANEL_DAILY_BASIC_FIELDS)
        lo = begin or "2026-09-01"
        dates = [d for d in _dates_asc(self.today, 400) if d >= lo]
        rows = [
            {
                "date": d,
                "symbol": symbol,
                "field": f,
                "value": _DB_VALUES[f],
            }
            for d in dates
            for f in want
        ]
        return {"ok": True, "as_of": self.today, "data": rows, "meta": {"fields": want}}

    def hist_stats(self, symbol, metric="pe", window="3y"):
        dates = _dates_asc(self.today, 80)
        # 2.x envelope: {ok, as_of, data:{series: DF[date, value]}}, asc.
        return {
            "ok": True,
            "as_of": self.today,
            "data": {
                "series": pd.DataFrame(
                    {
                        "date": dates,
                        "value": [20.0 + _doy(d) * 0.01 for d in dates],
                    }
                )
            },
            "meta": {},
        }


class _StubStocks:
    def list(self):
        return pd.DataFrame({"symbol": SYMS, "exchange": ["sh", "sz"]})


class _StubClient:
    def __init__(self, today: str = TODAY_BULK):
        self.kline = _StubKline()
        self.moneyflow = _StubMoneyflow()
        self.valuation = _StubValuation()
        self.stocks = _StubStocks()
        self.last_ec_cost = 10.0
        self.set_today(today)

    def set_today(self, day: str) -> None:
        """Advance the stub clock across ALL groups in one call - the fetch
        day must move together or valuation/kline frontiers diverge."""
        self.kline.today = day
        self.moneyflow.today = day
        self.valuation.today = day


def _build_bulk(tmp_path, symbols=SYMS, n=60):
    """Full bulk fetch at TODAY_BULK through the REAL pipeline -> resume store."""
    store = tmp_path / "resume"
    manifest = resume.init_manifest(
        store,
        params={
            "n": n,
            "fields": list(config.HISTORY_FIELDS),
            "live": True,
            "freq": "day",
            "adjust": "hfq_factor",
        },
        resume=False,
    )
    stub = _StubClient()
    state = {"manifest": manifest}

    def _save(symbol, instrument, frames, ec):
        # record_fragment returns the UPDATED manifest (copy-on-write style) -
        # keep using it, exactly like the CLI's nonlocal holder. today is the
        # STUB clock (the bulk fetch happened at TODAY_BULK in stub time).
        state["manifest"] = resume.record_fragment(
            store,
            state["manifest"],
            symbol,
            instrument,
            frames,
            today=TODAY_BULK,
        )

    result = fetch.fetch_history_panel(
        symbols,
        n=n,
        client=stub,
        max_workers=1,
        on_symbol=_save,
    )
    assert result.metering.ec_cost is not None
    return store, state["manifest"], stub


def _run_tail(store, manifest, stub, symbols=SYMS, **kw):
    """Tail fetch at stub's current 'today' + record, then reconcile."""
    today = kw.pop("today", stub.kline.today)
    entry = manifest["symbols"]
    last_dates = {
        s: (entry.get(s, {}).get("tail") or {}).get("last_date")
        or entry.get(s, {}).get("last_date")
        for s in symbols
    }
    v_last = {
        s: (entry.get(s, {}).get("tail") or {}).get("valuation_last_date")
        or entry.get(s, {}).get("bulk_date")
        for s in symbols
    }
    db_last = {
        s: (entry.get(s, {}).get("tail") or {}).get("daily_basic_last_date") for s in symbols
    }
    merged_manifest = manifest

    def _save(symbol, instrument, frames, ec):
        nonlocal merged_manifest
        refreshed = any(
            set(f.get("field", pd.Series(dtype=object)).unique()) & set(config.VALUATION_FIELDS)
            for f in frames
            if hasattr(f, "columns") and "field" in f.columns
        )
        # mirror the CLI's _save_tail: the daily_basic pointer advances from
        # the RESULT only (P3-1)
        db_dates: list[str] = []
        for f in frames:
            if (
                hasattr(f, "columns")
                and "field" in f.columns
                and set(f["field"].unique()) & set(config.PANEL_DAILY_BASIC_FIELDS)
            ):
                d = pd.to_datetime(f["date"], errors="coerce").dropna()
                db_dates.extend(d.dt.date.astype(str).tolist())
        merged_manifest = resume.record_tail_fragment(
            store,
            merged_manifest,
            symbol,
            instrument,
            frames,
            valuation_refreshed=refreshed,
            today=today,
            daily_basic_last_date=max(db_dates) if db_dates else None,
        )

    result = fetch.fetch_history_tail(
        symbols,
        last_dates=last_dates,
        valuation_last_dates=v_last,
        daily_basic_last_dates=db_last,
        today=today,
        client=stub,
        max_workers=1,
        on_symbol=_save,
        **kw,
    )
    return result, merged_manifest


# --- the core end-to-end: append, reconcile, factor step, dump ---------------
def test_tail_appends_reconciles_and_dumps(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    assert manifest["symbols"]["600519"]["last_date"] == TODAY_BULK

    stub.set_today(TODAY_TAIL)
    result, manifest = _run_tail(store, manifest, stub)

    # kline 2 + moneyflow 1 + valuation 4 = 7 calls/symbol (valuation due: 7d)
    assert result.metering.requests == len(SYMS) * 7
    assert len(manifest["symbols"]["600519"]["tail"]["last_date"] or "") > 0
    assert manifest["symbols"]["600519"]["tail"]["valuation_last_date"] == TODAY_TAIL

    full = resume.load_panel_with_tail(store, manifest)
    assert full["date"].max() == TODAY_TAIL, "tail bars must extend the panel"
    integ = panel.assert_panel_integrity(full)
    assert integ["ok"], integ

    # the ex-div factor step must be present, and only UPWARD
    fac = full[(full["field"] == "factor") & (full["instrument"] == "SH600519")]
    steps = fac.sort_values("date")
    assert (steps.loc[steps["date"] == EXDIV_DAY, "value"] > 2.4).all()
    assert (steps.loc[steps["date"] < EXDIV_DAY, "value"] < 2.1).all()

    # tail-only anchor round-trips through the .bin dump
    out = tmp_path / "qlib_data"
    dump_manifest = dump.dump_panel(full, out)
    assert dump_manifest is not None
    calendar = format.read_calendar(out)
    expected = _raw_close(TODAY_TAIL) * _hfq_factor(TODAY_TAIL)
    actual = format.read_bin_selfcheck(out, "SH600519", "close", TODAY_TAIL, calendar)
    assert actual is not None and abs(actual - expected) < 1e-3


# --- reconciliation gate: history must NEVER be silently rewritten ----------
def test_bulk_tail_conflict_raises(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    _, manifest = _run_tail(store, manifest, stub)

    # tamper ONE overlapping bulk row (upstream revision simulation)
    frag_path = resume._frag_path(store, "600519")
    frag = pd.read_parquet(frag_path)
    row_mask = (frag["date"] == TODAY_BULK) & (frag["field"] == "close")
    assert row_mask.any()
    frag.loc[row_mask, "value"] = frag.loc[row_mask, "value"] + 1.0
    frag.to_parquet(frag_path, index=False)

    with pytest.raises(ValueError, match="reconciliation FAILED"):
        resume.load_panel_with_tail(store, resume.load_manifest(store))


# --- idempotency: running twice the same day spends ZERO EC ------------------
def test_tail_up_to_date_spends_zero_ec(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    # stub 'today' still TODAY_BULK: gap = 0
    result, _ = _run_tail(store, manifest, stub)
    assert result.metering.requests == 0
    assert (result.metering.ec_cost or 0.0) == 0.0
    assert result.frame.empty
    assert any("up to date" in w for w in result.warnings)


# --- planning guards ---------------------------------------------------------
def test_tail_skips_symbols_without_bulk(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path, symbols=[SYMS[0]])
    stub.set_today(TODAY_TAIL)
    result, _ = _run_tail(store, manifest, stub, symbols=list(SYMS))
    assert any("no bulk fragment" in w and SYMS[1] in w for w in result.warnings)
    assert set(result.frame["instrument"].unique()) == {"SH600519"}


def test_tail_too_stale_is_skipped_not_fetched(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path, symbols=[SYMS[0]])
    # pretend the bulk is a month old: k = 30 + overlap > tail_max_n
    entry = manifest["symbols"][SYMS[0]]
    entry["last_date"] = "2026-10-01"
    stub.set_today(TODAY_TAIL)
    result, _ = _run_tail(store, manifest, stub, symbols=[SYMS[0]], tail_max_n=30)
    assert any("tail_max_n" in w for w in result.warnings)
    assert result.frame.empty


def test_tail_valuation_cadence_gates_the_refresh(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    # valuation refreshed at the stub's TAIL day: must NOT re-pull the 3y series
    for s in SYMS:
        manifest["symbols"][s]["tail"] = {"valuation_last_date": TODAY_TAIL}
    result, manifest2 = _run_tail(store, manifest, stub)
    assert result.metering.requests == len(SYMS) * 3  # kline 2 + moneyflow 1
    tail_fields = set(result.frame["field"].unique())
    assert not (tail_fields & set(config.VALUATION_FIELDS))


def test_tail_ec_ceiling_stops_before_second_symbol(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    # probe symbol: 7 calls x 10 EC = 70 -> ceiling 50 stops after the probe
    result, _ = _run_tail(store, manifest, stub, max_ec=50.0)
    assert result.metering.ec_cost is not None and result.metering.ec_cost >= 50.0
    assert any("budget ceiling" in w for w in result.warnings)
    assert set(result.frame["instrument"].unique()) == {"SH600519"}


# 0.7.0: the live-only guard is gone (live is the only mode); the update
# contract now rests on the adjust guard + the max-ec guard below.
# --- CLI guard rails (all fire BEFORE any network/EC) ------------------------
def _ns(**kw):
    base = dict(
        symbols=None,
        out="out/qlib_data",
        store=None,
        workers=1,
        max_ec=None,
        adjust=None,
        tail_overlap=5,
        tail_max_n=30,
        valuation_days=7,
        freq="day",
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_update_history_refuses_qfq_const():
    with pytest.raises(SystemExit, match="immutable basis"):
        cli.cmd_update_history(_ns(adjust="qfq_const"))


def test_update_history_requires_max_ec():
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_update_history(_ns())


# --- P3-1: tail daily_basic reads are windowed; the resume pointer advances ---
def test_tail_daily_basic_is_windowed_and_pointer_advances(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    fields = config.HISTORY_FIELDS + config.PANEL_DAILY_BASIC_FIELDS

    # first-ever tail: no resume pointer -> begin = today - tail_max_n
    result, manifest = _run_tail(store, manifest, stub, fields=fields, tail_max_n=30)
    calls = stub.valuation.db_calls
    assert calls, "daily_basic must be called on the tail path"
    expected_first = (dt.date.fromisoformat(TODAY_TAIL) - dt.timedelta(days=30)).isoformat()
    assert all(c["begin"] == expected_first for c in calls), calls
    assert all(sorted(c["fields"]) == sorted(config.PANEL_DAILY_BASIC_FIELDS) for c in calls), calls
    for s in SYMS:
        assert manifest["symbols"][s]["tail"]["daily_basic_last_date"] == TODAY_TAIL, (
            "pointer must advance when the read returned rows"
        )

    # second tail next day: begin = the recorded pointer (NOT a full re-read)
    stub.valuation.db_calls.clear()
    stub.set_today("2026-11-18")
    _result2, manifest2 = _run_tail(store, manifest, stub, fields=fields, tail_max_n=30)
    calls2 = stub.valuation.db_calls
    assert calls2, "second tail must still read daily_basic"
    assert all(c["begin"] == TODAY_TAIL for c in calls2), calls2
    for s in SYMS:
        assert manifest2["symbols"][s]["tail"]["daily_basic_last_date"] == "2026-11-18"


def test_tail_without_daily_basic_fields_never_calls_endpoint(tmp_path):
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    result, _manifest = _run_tail(store, manifest, stub)  # default HISTORY_FIELDS
    assert stub.valuation.db_calls == []


# --- P3-3: Gate B automation - pull the official side over the acc window ----
def test_reconcile_daily_basic_from_upstream(tmp_path, monkeypatch, capsys):
    import argparse as _argparse

    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    fields = config.HISTORY_FIELDS + config.PANEL_DAILY_BASIC_FIELDS
    _result, manifest = _run_tail(store, manifest, stub, fields=fields, tail_max_n=30)

    monkeypatch.setattr(fetch, "_client", lambda **kw: stub)
    ns = _argparse.Namespace(
        accumulated=None,
        upstream=None,
        from_upstream=True,
        store=str(store),
        symbols=None,
        tol=1e-6,
        max_ec=10**9,  # required since 0.6.0: --from-upstream is a billed read
    )
    cli.cmd_reconcile_daily_basic(ns)  # must not raise: accumulated==upstream
    out = capsys.readouterr().out
    assert "[gate-b] PASS" in out


def test_reconcile_from_upstream_requires_max_ec(tmp_path):
    """The 0.5.0 changelog claimed this gate existed; it actually shipped in
    0.6.0. --from-upstream bills rows-scanned daily_basic reads, so a missing
    budget must refuse BEFORE any client/pull (no credentials needed here)."""
    import argparse as _argparse

    store, _manifest, _stub = _build_bulk(tmp_path)
    ns = _argparse.Namespace(
        accumulated=None,
        upstream=None,
        from_upstream=True,
        store=str(store),
        symbols=None,
        tol=1e-6,
        max_ec=None,
    )
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_reconcile_daily_basic(ns)


# --- 2026-11-24 P0: the tail file must be MERGED, never overwritten ----------
def test_second_tail_after_gap_over_overlap_keeps_every_date(tmp_path):
    """A tail window reaches back only ``gap + overlap`` bars.

    Bulk ends 11-10; the first tail (11-20) re-covers it and adds 11-11..11-20;
    the next day's tail (11-21, k=6) only reaches 11-16. Overwriting the tail
    file destroyed 11-11..11-15 -- a hole after the bulk end that the following
    full re-dump wrote into the .bin. Every date must survive the merge.
    """
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today("2026-11-20")
    _, manifest = _run_tail(store, manifest, stub)
    stub.set_today("2026-11-21")
    _, manifest = _run_tail(store, manifest, stub)

    full = resume.load_panel_with_tail(store, manifest)
    dates = sorted(full[(full["symbol"] == SYMS[0]) & (full["field"] == "close")]["date"])
    expected = _dates_asc("2026-11-21", 60 + 11)  # bulk(60) + 11 days after 11-10
    missing = sorted(set(expected) - set(dates))
    assert not missing, f"tail overwrite lost dates: {missing}"
    assert dates == expected


def test_tail_merge_conflict_raises(tmp_path):
    """Old tail vs new tail disagreement is an upstream revision: fail loud."""
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today("2026-11-20")
    _, manifest = _run_tail(store, manifest, stub)

    tail_path = resume._tail_path(store, SYMS[0])
    tail = pd.read_parquet(tail_path)
    mask = (tail["date"] == "2026-11-20") & (tail["field"] == "close")
    assert mask.any()
    tail.loc[mask, "value"] = tail.loc[mask, "value"] + 1.0
    tail.to_parquet(tail_path, index=False)

    stub.set_today("2026-11-21")  # k=6 re-covers 11-20 -> conflict must surface
    with pytest.raises(ValueError, match="reconciliation FAILED"):
        _run_tail(store, manifest, stub)


def test_fresh_bulk_supersedes_stale_tail(tmp_path):
    """A rebuilt bulk fragment owns the symbol: its old tail must not survive.

    Otherwise a non-resume rebuild would keep mixing pre-rebuild rows (possibly
    under another convention) into the merged panel.
    """
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    _, manifest = _run_tail(store, manifest, stub)
    tail_path = resume._tail_path(store, SYMS[0])
    assert tail_path.exists()

    fresh = resume.init_manifest(store, params=manifest["params"], resume=False)
    frames = [
        pd.DataFrame(
            {
                "date": ["2026-11-18"],
                "symbol": [SYMS[0]],
                "instrument": ["SH600519"],
                "field": ["close"],
                "value": [10.0],
            }
        )
    ]
    fresh = resume.record_fragment(
        store, fresh, SYMS[0], "SH600519", frames, today=TODAY_TAIL
    )
    assert not tail_path.exists(), "stale tail file survived a fresh bulk"
    assert "tail" not in fresh["symbols"][SYMS[0]]

    # an ORPHAN tail file (no manifest state) must be ignored too
    pd.concat(frames, ignore_index=True).to_parquet(tail_path, index=False)
    merged = resume.load_panel_with_tail(store, fresh)
    assert set(merged["date"]) == {"2026-11-18"}


def test_dump_only_redumps_merged_store_without_fetching(tmp_path, monkeypatch, capsys):
    """--dump-only: the zero-EC migration path (layout change / tail refresh)."""
    store, manifest, stub = _build_bulk(tmp_path)
    stub.set_today(TODAY_TAIL)
    _, manifest = _run_tail(store, manifest, stub)

    out = tmp_path / "qlib_data"
    ns = argparse.Namespace(
        symbols=None,
        out=str(out),
        store=str(store),
        workers=1,
        max_ec=None,
        adjust=None,
        tail_overlap=5,
        tail_max_n=30,
        valuation_days=7,
        daily_basic=False,
        dry_run=False,
        dump_only=True,
        freq="day",
    )
    monkeypatch.setattr(
        fetch, "_client", lambda *a, **k: pytest.fail("--dump-only fired a fetch")
    )
    cli.cmd_update_history(ns)

    calendar = format.read_calendar(out)
    assert calendar[-1] == TODAY_TAIL, "tail rows did not reach the .bin"
    meta = json.loads((out / "fetch_meta.json").read_text())
    assert meta["dump_only"] is True
    assert meta["format"] == "2.0"  # the layout this re-dump certifies
    assert "ec_cost" not in meta["metering"] and meta["metering"]["requests"] == 0


def test_update_history_daily_basic_flag_reaches_the_fetcher(tmp_path, monkeypatch):
    """P1: the flag used to be reset to HISTORY_FIELDS before the fetch, so the
    dry-run quoted a daily_basic read that never happened."""
    store, _manifest, _stub = _build_bulk(tmp_path)
    captured: dict = {}

    def fake_tail(symbols, **kw):
        captured.update(kw)
        return fetch.FetchResult(
            frame=pd.DataFrame(columns=resume.COLS),
            metering=fetch.Metering(mode="live", requests=0, ec_cost=0.0, auth_mode="cache_hit"),
            warnings=[],
        )

    monkeypatch.setattr(fetch, "fetch_history_tail", fake_tail)
    argv = [
        "update-history",
        "--max-ec",
        "10",
        "--store",
        str(store),
        "--out",
        str(tmp_path / "qlib_data"),
    ]
    cli.cmd_update_history(cli.build_parser().parse_args(argv + ["--daily-basic"]))
    assert set(config.PANEL_DAILY_BASIC_FIELDS) <= set(captured["fields"])

    captured.clear()
    cli.cmd_update_history(cli.build_parser().parse_args(argv))
    assert not set(config.PANEL_DAILY_BASIC_FIELDS) & set(captured["fields"])
