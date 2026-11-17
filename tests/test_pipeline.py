"""End-to-end pipeline tests using demo snapshot (offline, no credentials).

Verification is split into two tiers (follow-up roadmap M0):
  * Tier 1 (MUST run, zero Qlib): fetch -> normalize -> dump .bin -> pure-numpy
    self-check via format.read_bin_selfcheck.
  * Tier 2 (optional, importorskip pyqlib): read the same .bin back with Qlib's
    own reader IN A CHILD PROCESS and assert the parent's qlib.config.C is
    unchanged (no global side effects).

Score CSV roundtrip also uses the qlib_io boundary with an equivalent fallback.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from wmshare_qlib import config, cross_section, dump, fetch, format, panel, qlib_io

SYMBOLS = ["600519", "000858"]
DAY = "2026-10-30"


def _anchors(df):
    """Build (instrument, field, date, value) anchors from a fetched panel."""
    checks = []
    for inst in ("SH600519", "SZ000858"):
        sub = df[(df.instrument == inst) & (df.field == "close")].sort_values("date")
        row = sub.iloc[-1]
        checks.append((inst, "close", row["date"], float(row["value"])))
    return checks


def test_panel_integrity_and_direction(tmp_path):
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    assert len(r.frame) > 0
    assert r.metering.mode == "demo"
    integ = panel.assert_panel_integrity(r.frame)
    assert integ["ok"], integ
    assert integ["instruments"] == 2
    assert "pe" in integ["fields"]
    assert "close" in integ["fields"]
    # degradation warnings are explicit, never silent zeros
    assert any("skipped" in w for w in r.warnings) or any("capped" in w for w in r.warnings)


# --- P0-2 regression: cross-field date alignment -----------------------------
def test_cross_field_date_alignment_holds_on_real_panel():
    """$pe must trade on the SAME dates as $close (per instrument)."""
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    rep = panel.assert_dates_aligned_across_fields(r.frame)
    assert rep["ok"], rep
    # coverage must be recorded for every present valuation field
    assert rep["coverage"], "alignment coverage not computed"


def test_cross_field_alignment_catches_one_day_offset():
    """A systematic +1 trading-day offset (the P0-2 bug shape) must FAIL loud."""
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    shifted = r.frame.copy()
    is_pe = shifted["field"] == "pe"
    assert is_pe.any(), "demo panel has no pe rows; alignment test vacuous"
    shifted.loc[is_pe, "date"] = (
        pd.to_datetime(shifted.loc[is_pe, "date"]) + pd.Timedelta(days=1)
    ).dt.strftime("%Y-%m-%d")
    rep = panel.assert_dates_aligned_across_fields(shifted)
    assert not rep["ok"]
    assert any("/pe" in i for i in rep["issues"])


# --- P1-3 regression: unit magnitude guard is wired into integrity ------------
def test_panel_integrity_catches_absurd_magnitude():
    """Scale drift (e.g. pe emitted as 1e6) must fail integrity, never reach Qlib."""
    r = fetch.fetch_history_panel(SYMBOLS, n=30)
    bad = r.frame.copy()
    is_pe = bad["field"] == "pe"
    assert is_pe.any()
    bad.loc[is_pe, "value"] = 1e6
    integ = panel.assert_panel_integrity(bad)
    assert not integ["ok"]
    assert any("outside" in i and "pe" in i for i in integ["issues"]), integ["issues"]


# --- P0-2' regressions: factor emission and the factor monotonic gate ---------
def test_demo_factor_is_explicit_constant_one():
    """Demo (qfq_const, forced) must still emit factor=1.0 explicitly - the
    convention is DECLARED, never silently omitted."""
    r = fetch.fetch_history_panel(SYMBOLS, n=30)
    f = r.frame[r.frame["field"] == "factor"]
    assert len(f) > 0, "demo panel missing the declared factor field"
    assert (f["value"] == 1.0).all()


def test_panel_integrity_catches_decreasing_factor():
    """The dump-time backstop: factor decreasing along dates (mixed-convention
    fragments, upstream inconsistency) must fail integrity. The manifest param
    guard refuses the mix at resume time; this catches anything that slips past.
    """
    r = fetch.fetch_history_panel(SYMBOLS, n=30)
    bad = r.frame.copy()
    fac = bad[bad["field"] == "factor"]
    inst = fac["instrument"].iloc[0]
    sub = fac[fac["instrument"] == inst].sort_values("date")
    oldest_idx = sub.index[0]
    bad.loc[oldest_idx, "value"] = 5.0  # oldest 5.0 -> later 1.0: DECREASE
    integ = panel.assert_panel_integrity(bad)
    assert not integ["ok"]
    assert any(inst in i and "factor DECREASES" in i for i in integ["issues"]), integ["issues"]


# --- EC budget gate: live cross-section requires an explicit budget -----------
def test_live_cross_section_requires_explicit_max_ec():
    """screener.query bills column MATERIALIZATION in one request (light
    columns 70~500 EC, heavy ~48k EC). The cost is only knowable once paid,
    so without an explicit max_ec it must fail loud BEFORE any request.
    (The earlier 'per-symbol billing' reading was wrong - source docs
    llms-full.txt §10.1: 219 EC for a 2-code query, NOT scaling with rows.)
    """
    from wmshare_qlib import normalize

    with pytest.raises(normalize.NormalizeError, match="explicit max_ec"):
        fetch.fetch_cross_section(live=True)


def test_live_cross_section_single_request_and_ec_accounting(monkeypatch):
    """Live path mechanics with a stub (zero real EC):

    1. exactly ONE screener.request fires (no offset pagination - every page
       would re-pay column materialization, ~114k EC x pages);
    2. limit is the 10000 hard cap (whole universe fits one request);
    3. metering surfaces ec_cost from the response attrs;
    4. post-hoc max_ec ceiling raises loud when the paid cost exceeds it.
    """
    from wmshare_qlib import normalize

    calls: list[dict] = []

    def fake_query(where, order=None, desc=True, limit=50, offset=0,
                   fields=None, idx=None, _df=False):
        calls.append({"where": where, "limit": limit, "offset": offset})
        df = pd.DataFrame({
            "symbol": ["600519", "000858"],
            "$PCT_PE_TTM_10Y_LAST": [0.12, 0.45],
            "$PCT_PB_TTM_10Y_LAST": [0.30, 0.80],
            "$ROE_TTM_LAST": [0.22, 0.09],
            "$BONUS_RATE_3Y_AVG_LAST": [0.05, 0.01],
        })
        df.attrs["ec_cost"] = 800.0
        return df

    class _StubScreener:
        query = staticmethod(fake_query)

    class _StubStocks:
        @staticmethod
        def list(_df=True):
            return pd.DataFrame({
                "symbol": ["600519", "000858"],
                "exchange": ["sh", "sz"],
            })

    class _StubClient:
        screener = _StubScreener()
        stocks = _StubStocks()
        last_ec_cost = 800

    r = fetch.fetch_cross_section(live=True, client=_StubClient(), max_ec=1000)
    assert len(calls) == 1, f"expected ONE request, got {len(calls)}"
    assert calls[0]["limit"] == 10000 and calls[0]["offset"] == 0
    assert r.metering.ec_cost == pytest.approx(800.0)
    assert r.metering.requests == 2  # screener.query + stocks.list
    assert len(r.frame) == 2
    # instruments must be Qlib-style <SUFFIX><code> (bare codes poison scoring)
    assert set(r.frame["instrument"]) == {"SH600519", "SZ000858"}
    # a second request (e.g. pagination) would be the EC bomb amplifier
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section(live=True, client=_StubClient(), max_ec=500)


def test_ec_cost_falls_back_to_client_last_ec_cost(monkeypatch):
    """REGRESSION (2026-11-17, real 621,399 EC breach): the gateway surfaces
    EC via ``client.last_ec_cost``, NOT df.attrs - reading only attrs left the
    max_ec ceiling BLIND and a 3,000 ceiling was silently passed by a 621,399
    EC request. The fallback must be wired, and the ceiling must raise.
    """
    from wmshare_qlib import normalize

    class _StubScreener:
        @staticmethod
        def query(where, order=None, desc=True, limit=50, offset=0,
                  fields=None, idx=None, _df=False):
            return pd.DataFrame({
                "symbol": ["600519"],
                "$PCT_PE_TTM_10Y_LAST": [0.12],
                "$PCT_PB_TTM_10Y_LAST": [0.30],
                "$ROE_TTM_LAST": [0.22],
                "$BONUS_RATE_3Y_AVG_LAST": [0.05],
            })  # attrs deliberately WITHOUT ec_cost - the gateway contract

    class _StubStocks:
        @staticmethod
        def list(_df=True):
            return pd.DataFrame({"symbol": ["600519"], "exchange": ["sh"]})

    class _StubClient:
        screener = _StubScreener()
        stocks = _StubStocks()
        last_ec_cost = 621_399  # what the SDK actually reports

    r = fetch.fetch_cross_section(live=True, client=_StubClient(), max_ec=10**9)
    assert r.metering.ec_cost == pytest.approx(621_399.0), (
        "metering must fall back to client.last_ec_cost"
    )
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section(live=True, client=_StubClient(), max_ec=3000)


def test_field_declaration_two_way_coverage(tmp_path):
    """M2 DoD: zero phantom fields; missing ones are explicitly reported.

    Demo snapshot skips some valuation fields (cache/window), so a strict
    emitted==declared assertion would be wrong here. The hard guarantee is
    ZERO PHANTOM (every emitted field has a unit declaration). Missing fields
    must be enumerated in the report, never silently ignored.
    """
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    emitted = set(r.frame["field"].unique())
    check = panel.assert_field_declaration_roundtrip(emitted, config.FIELD_UNITS)
    assert check["phantom"] == [], f"undeclared fields emitted: {check['phantom']}"
    assert isinstance(check["missing"], list)  # reported, may be non-empty in demo
    # Demo snapshot does not store pb/ps/pr (only pe in the fixed snapshot).
    # `factor` is now emitted explicitly (qfq_const convention, factor=1).
    # Missing fields are EXPLICIT here — never silently ignored.
    allowed_missing = set(config.VALUATION_FIELDS) - {"pe"}
    assert set(check["missing"]).issubset(allowed_missing), check["missing"]
    assert "factor" not in check["missing"], "factor must be emitted under qfq_const"


def test_dump_selfcheck_without_qlib(tmp_path):
    """TIER 1 (mandatory): .bin is valid WITHOUT importing Qlib at all."""
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    out = tmp_path / "qlib_data"
    dump.dump_panel(r.frame, out)

    calendar = format.read_calendar(out)
    assert len(calendar) > 0
    for inst, field, date, expected in _anchors(r.frame):
        assert date in calendar, f"anchor date {date} not in calendar"
        actual = format.read_bin_selfcheck(out, inst, field, date, calendar)
        assert actual is not None, f"{inst}/{field}@{date} missing"
        assert abs(actual - expected) <= max(1e-3, abs(expected) * 1e-5), (
            f"{inst}/{field}@{date}: expected={expected} actual={actual}"
        )

    # .bin must be little-endian float32 with start_index header.
    import numpy as np

    arr = np.fromfile(format.bin_path(out, "SH600519", "close"), dtype="<f4")
    assert arr.dtype == np.dtype("<f4")
    assert int(arr[0]) >= 0  # start index header


@pytest.mark.qlib
def test_dump_readback_with_qlib_isolated(tmp_path):
    """TIER 2 (optional): Qlib reads back the .bin, with NO parent-process side effect."""
    pytest.importorskip("qlib")
    r = fetch.fetch_history_panel(SYMBOLS, n=60)
    out = tmp_path / "qlib_data"
    dump.dump_panel(r.frame, out)
    checks = _anchors(r.frame)

    # If a host app initialized Qlib already, verify_bin must not touch it.
    import qlib

    try:
        qlib.init(provider_uri=str(tmp_path / "host_provider"), region="cn")
    except Exception:
        pass  # already initialized; the point is to compare before/after
    # Config is a custom mapping; snapshot its backing dict, not iteration.
    def _snapshot():
        cfg = getattr(qlib.config, "C", None)
        if cfg is None:
            return None
        backing = getattr(cfg, "_config", None)
        return dict(backing) if isinstance(backing, dict) else repr(cfg)

    before = _snapshot()

    result = qlib_io.verify_bin(out, checks)

    after = _snapshot()
    assert before == after, "verify_bin polluted the parent process qlib.config.C"
    assert result["ok"] is True, result["details"]
    assert result["status"] == "passed", result
    assert len(result["details"]) >= len(checks)


def test_cross_section_score_csv_roundtrip(tmp_path):
    r = fetch.fetch_cross_section(as_of=DAY)
    assert r.metering.mode == "demo"
    assert len(r.frame) > 0
    out = cross_section.write_score_csv(r, tmp_path / "score", as_of=DAY)
    assert out.exists()

    meta = json.loads((tmp_path / "score" / f"{DAY}.meta.json").read_text())
    assert meta["history"] is False
    assert meta["metering"]["mode"] == "demo"

    read = qlib_io.read_score(out, DAY)
    assert read.series is not None
    assert read.backend in {"ScoreFileModel", "local-equivalent"}
    assert len(read.series) == len(r.frame)
    # Degradation is explicit, never silent: a local fallback must say why.
    if read.backend == "local-equivalent":
        assert read.note


def test_accumulation_cold_start_guard(tmp_path):
    r = fetch.fetch_cross_section(as_of=DAY)
    cross_section.accumulate(r.frame, tmp_path / "acc", as_of=DAY)
    frame, status = cross_section.promote_to_panel(
        tmp_path / "acc", min_days=60, out_parquet=tmp_path / "panel.parquet"
    )
    assert frame is None  # only 1 day, must not promote
    assert "need >= 60" in status


def test_history_meta_available_from_semantics():
    """P2-2 rewording: PIT available-from semantics + wired guard default.

    The threshold is a promotion guard (configurable, single source of truth in
    config.history_days_threshold, consumed by the CLI default) - NOT a data
    limit: the series is honest from its first captured day.
    """
    meta = cross_section.history_meta(1, config.history_days_threshold(),
                                      available_from=DAY)
    assert meta["available_from"] == DAY
    assert meta["threshold"] == config.history_days_threshold()
    assert meta["backtestable"] is False
    assert "PIT self-built series" in meta["note"]
    assert f"available from {DAY}" in meta["note"]
    assert "honest, just short" in meta["note"]

    passed = cross_section.history_meta(config.history_days_threshold(),
                                        config.history_days_threshold(),
                                        available_from=DAY)
    assert passed["backtestable"] is True
    assert "guard passed" in passed["note"]

    # default threshold comes from config, not a second hard-coded 60
    assert config.history_days_threshold() == 60


def test_accumulate_promotes_once_guard_passes(tmp_path):
    """The P2-2 auto-promotion path: guard passes -> promote_to_panel succeeds."""
    r = fetch.fetch_cross_section(as_of=DAY)
    for i in range(config.history_days_threshold()):
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        cross_section.accumulate(r.frame, tmp_path / "acc", as_of=d)
    frame, status = cross_section.promote_to_panel(
        tmp_path / "acc", min_days=config.history_days_threshold(),
        out_parquet=tmp_path / "panel.parquet",
    )
    assert frame is not None
    assert "promoted 60 days" in status
    assert (tmp_path / "panel.parquet").exists()
