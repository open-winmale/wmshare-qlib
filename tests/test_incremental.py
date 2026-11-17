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

import pandas as pd
import pytest

from wmshare_qlib import cli, config, dump, fetch, format, normalize, panel, resume

SYMS = ["600519", "000858"]
TODAY_BULK = "2026-11-10"   # bulk fetch day
TODAY_TAIL = "2026-11-17"   # one week later: gap 7d -> k = 7 + overlap
EXDIV_DAY = "2026-11-13"    # hfq factor steps UP here (event applies to >= day)
KEY = ["date", "instrument", "field", "value"]


def _dates_asc(today: str, n: int) -> list[str]:
    """`n` calendar dates ENDING at `today`, OLDEST first (2.x asc)."""
    d = dt.date.fromisoformat(today)
    return [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _doy(iso: str) -> int:
    return dt.date.fromisoformat(iso).timetuple().tm_yday


def _raw_close(d: str) -> float:
    return 10.0 + _doy(d) * 0.1


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
        return pd.DataFrame({
            "date": dates,
            "net_mf": [60.0 + _doy(d) * 0.5 for d in dates],
            "elg_net": [30.0 + _doy(d) * 0.2 for d in dates],
            "lg_net": [20.0 + _doy(d) * 0.1 for d in dates],
            "md_net": [5.0 + _doy(d) * 0.1 for d in dates],
            "sm_net": [-5.0 + _doy(d) * 0.05 for d in dates],
        })


class _StubValuation:
    """Date-deterministic series ENDING at the stub fetch day (valuation
    history can never extend past it). The window is LONGER than the kline
    anchor window (real: 3y vs n-day), otherwise the cross-field alignment
    gate would fail on stub coverage. Bulk and tail refresh agree on the
    overlap (immutable history); the tail merely extends the frontier."""

    today = TODAY_BULK

    def hist_stats(self, symbol, metric="pe", window="3y"):
        dates = _dates_asc(self.today, 80)
        # 2.x envelope: {ok, as_of, data:{series: DF[date, value]}}, asc.
        return {
            "ok": True,
            "as_of": self.today,
            "data": {"series": pd.DataFrame({
                "date": dates,
                "value": [20.0 + _doy(d) * 0.01 for d in dates],
            })},
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
        params={"n": n, "fields": list(config.HISTORY_FIELDS), "live": True,
                "freq": "day", "adjust": "hfq_factor"},
        resume=False,
    )
    stub = _StubClient()
    state = {"manifest": manifest}

    def _save(symbol, instrument, frames, ec):
        # record_fragment returns the UPDATED manifest (copy-on-write style) -
        # keep using it, exactly like the CLI's nonlocal holder. today is the
        # STUB clock (the bulk fetch happened at TODAY_BULK in stub time).
        state["manifest"] = resume.record_fragment(
            store, state["manifest"], symbol, instrument, frames, ec,
            today=TODAY_BULK,
        )

    result = fetch.fetch_history_panel(
        symbols, live=True, n=n, client=stub, max_workers=1, on_symbol=_save,
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
    merged_manifest = manifest

    def _save(symbol, instrument, frames, ec):
        nonlocal merged_manifest
        refreshed = any(
            set(f.get("field", pd.Series(dtype=object)).unique()) & set(config.VALUATION_FIELDS)
            for f in frames
            if hasattr(f, "columns") and "field" in f.columns
        )
        merged_manifest = resume.record_tail_fragment(
            store, merged_manifest, symbol, instrument, frames, ec,
            valuation_refreshed=refreshed, today=today,
        )

    result = fetch.fetch_history_tail(
        symbols,
        last_dates=last_dates,
        valuation_last_dates=v_last,
        live=True,
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
    assert any("hard ceiling" in w for w in result.warnings)
    assert set(result.frame["instrument"].unique()) == {"SH600519"}


def test_tail_is_live_only():
    with pytest.raises(normalize.NormalizeError, match="live-only"):
        fetch.fetch_history_tail(SYMS, last_dates={s: TODAY_BULK for s in SYMS},
                                 live=False)


# --- CLI guard rails (all fire BEFORE any network/EC) ------------------------
def _ns(**kw):
    base = dict(symbols=None, live=False, out="out/qlib_data", store=None,
                workers=1, max_ec=None, adjust=None, tail_overlap=5,
                tail_max_n=30, valuation_days=7, freq="day")
    base.update(kw)
    return argparse.Namespace(**base)


def test_update_history_refuses_demo():
    with pytest.raises(SystemExit, match="live-only"):
        cli.cmd_update_history(_ns(live=False))


def test_update_history_refuses_qfq_const():
    with pytest.raises(SystemExit, match="immutable basis"):
        cli.cmd_update_history(_ns(live=True, adjust="qfq_const"))


def test_update_history_requires_max_ec():
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_update_history(_ns(live=True))
