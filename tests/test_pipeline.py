"""End-to-end pipeline tests over a synthetic offline history client.

0.7.0: the offline demo snapshot was retired with the SDK's demo_client
(wmshare 3.0). These tests run the SAME pipeline (fetch -> normalize ->
dump .bin -> self-check) against ``_StubHistoryClient`` - synthetic frames
served through the real SDK-shaped API surface (kline.bars adj hfq/raw,
valuation.hist_stats envelope, moneyflow.bars). No network, no credentials,
zero EC.

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
DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
CLOSES = [1400.0, 1410.0, 1425.0, 1440.0, 1450.0]
PE = [21.5, 21.7, 21.9, 22.1, 22.3]


class _StubHistoryClient:
    """SDK-shaped offline client: the only history source these tests need.

    kline.bars serves synthetic ascending frames (adj="hfq" scales prices by
    ``adj_scale`` so the hfq+raw merge produces a real factor); valuation and
    moneyflow serve small envelopes shaped like the 2.x facade payloads.
    """

    def __init__(self, adj_scale: float = 2.0):
        self._adj_scale = adj_scale
        self.last_ec_cost = None  # no meter: result ec_cost stays None
        self.kline = _StubKline(self)
        self.valuation = _StubValuation()
        self.moneyflow = _StubMoneyflow()
        self.stocks = _StubStocks()


class _StubKline:
    def __init__(self, owner: "_StubHistoryClient"):
        self._owner = owner

    def bars(self, symbol, n=120, adj=None):
        df = pd.DataFrame({
            "date": list(DATES),
            "symbol": symbol,
            "open": [c - 5.0 for c in CLOSES],
            "high": [c + 5.0 for c in CLOSES],
            "low": [c - 10.0 for c in CLOSES],
            "close": list(CLOSES),
            "volume": [100_000.0] * len(DATES),
            "amount": [c * 100_000.0 for c in CLOSES],
            "percent": [0.001] * len(DATES),
        })
        if adj == "hfq":
            df[["open", "high", "low", "close"]] = df[
                ["open", "high", "low", "close"]
            ] * self._owner._adj_scale
        elif adj == "qfq":
            df[["open", "high", "low", "close"]] = df[
                ["open", "high", "low", "close"]
            ] * 0.5
        return df.tail(min(n, len(DATES))).reset_index(drop=True)


class _StubValuation:
    def hist_stats(self, symbol, metric="pe", window="3y"):
        return {
            "ok": True,
            "as_of": DATES[-1],
            "data": {"series": pd.DataFrame({"date": list(DATES), "value": list(PE)})},
            "meta": {"window": window},
        }


class _StubMoneyflow:
    def bars(self, symbol, n=120):
        return pd.DataFrame({
            "date": list(DATES),
            "symbol": symbol,
            "net_mf": [1_000.0] * len(DATES),
            "elg_net": [400.0] * len(DATES),
            "lg_net": [300.0] * len(DATES),
            "md_net": [200.0] * len(DATES),
            "sm_net": [100.0] * len(DATES),
        }).tail(min(n, len(DATES))).reset_index(drop=True)


class _StubStocks:
    def list(self):
        return pd.DataFrame({"symbol": SYMBOLS, "exchange": ["sh", "sz"]})


_STUB = _StubHistoryClient()


class _StubScreener:
    """screener.query + stocks.list for the cross-section tests (zero real EC)."""

    def __init__(self, ec_cost: int = 800):
        self.last_ec_cost = ec_cost
        self.screener = self
        self.stocks = self

    def query(self, where, order=None, desc=True, limit=50, offset=0,
              fields=None, idx=None):
        df = pd.DataFrame({
            "symbol": ["600519", "000858"],
            "$PCT_PE_TTM_10Y_LAST": [0.12, 0.45],
            "$PCT_PB_TTM_10Y_LAST": [0.30, 0.80],
            "$ROE_TTM_LAST": [0.22, 0.09],
            "$BONUS_RATE_3Y_AVG_LAST": [0.05, 0.01],
        })
        df.attrs["ec_cost"] = self.last_ec_cost
        return df

    def list(self):
        return pd.DataFrame({"symbol": SYMBOLS, "exchange": ["sh", "sz"]})


def _stub_panel(n: int = 60, **kwargs) -> object:
    """fetch_history_panel over the shared stub (offline, zero EC)."""
    return fetch.fetch_history_panel(SYMBOLS, client=_STUB, n=n, **kwargs)


def _anchors(df):
    """Build (instrument, field, date, value) anchors from a fetched panel."""
    checks = []
    for inst in ("SH600519", "SZ000858"):
        sub = df[(df.instrument == inst) & (df.field == "close")].sort_values("date")
        row = sub.iloc[-1]
        checks.append((inst, "close", row["date"], float(row["value"])))
    return checks


def test_panel_integrity_and_direction(tmp_path):
    r = _stub_panel()
    assert len(r.frame) > 0
    assert r.metering.mode == "live"
    assert r.metering.auth_mode == "credentials"  # stub carries no auth_mode attr
    integ = panel.assert_panel_integrity(r.frame)
    assert integ["ok"], integ
    assert integ["instruments"] == 2
    assert "pe" in integ["fields"]
    assert "close" in integ["fields"]
    assert "factor" in integ["fields"]  # hfq_factor: factor emitted explicitly


# --- P0-2 regression: cross-field date alignment -----------------------------
def test_cross_field_date_alignment_holds_on_real_panel():
    """$pe must trade on the SAME dates as $close (per instrument)."""
    r = _stub_panel()
    rep = panel.assert_dates_aligned_across_fields(r.frame)
    assert rep["ok"], rep
    # coverage must be recorded for every present valuation field
    assert rep["coverage"], "alignment coverage not computed"


def test_cross_field_alignment_catches_one_day_offset():
    """A systematic +1 trading-day offset (the P0-2 bug shape) must FAIL loud."""
    r = _stub_panel()
    shifted = r.frame.copy()
    is_pe = shifted["field"] == "pe"
    assert is_pe.any(), "panel has no pe rows; alignment test vacuous"
    shifted.loc[is_pe, "date"] = (
        pd.to_datetime(shifted.loc[is_pe, "date"]) + pd.Timedelta(days=1)
    ).dt.strftime("%Y-%m-%d")
    rep = panel.assert_dates_aligned_across_fields(shifted)
    assert not rep["ok"]
    assert any("/pe" in i for i in rep["issues"])


def test_cross_field_alignment_stale_field_is_not_offset():
    """A field that stopped publishing BEFORE the anchor window (北交所 pe 断供
    shape: 920719 last pe 2024-08-28 while kline anchors are recent) is a data
    availability fact, not a systematic offset - record in ``stale``, keep ok.
    """
    r = _stub_panel()
    stale = r.frame.copy()
    is_pe = stale["field"] == "pe"
    assert is_pe.any(), "panel has no pe rows; stale test vacuous"
    stale.loc[is_pe, "date"] = "2000-01-04"  # whole field predates anchor window
    rep = panel.assert_dates_aligned_across_fields(stale)
    assert rep["ok"], rep
    assert any("/pe" in s for s in rep["stale"])


# --- P1-3 regression: unit magnitude guard is wired into integrity ------------
def test_panel_integrity_catches_absurd_magnitude():
    """Scale drift (e.g. pe emitted as 1e6) must fail integrity, never reach Qlib."""
    r = _stub_panel(n=5)  # narrow window: fewer rows
    bad = r.frame.copy()
    is_pe = bad["field"] == "pe"
    assert is_pe.any()
    bad.loc[is_pe, "value"] = 1e6
    integ = panel.assert_panel_integrity(bad)
    assert not integ["ok"]
    assert any("outside" in i and "pe" in i for i in integ["issues"]), integ["issues"]


# --- P0-2' regressions: factor emission and the factor monotonic gate ---------
def test_qfq_const_factor_is_explicit_constant_one():
    """qfq_const must still emit factor=1.0 explicitly - the convention is
    DECLARED, never silently omitted."""
    r = fetch.fetch_history_panel(
        SYMBOLS, client=_StubHistoryClient(), n=30, adjust_mode="qfq_const"
    )
    f = r.frame[r.frame["field"] == "factor"]
    assert len(f) > 0, "qfq_const panel missing the declared factor field"
    assert (f["value"] == 1.0).all()


def test_hfq_factor_emitted_and_constant():
    """Default hfq_factor: factor = adj/raw is emitted (2.0 on the stub) and
    constant on this synthetic window (no corporate action in it)."""
    r = _stub_panel()
    f = r.frame[r.frame["field"] == "factor"]
    assert len(f) > 0, "hfq panel missing the declared factor field"
    assert (f["value"] == 2.0).all()


def test_panel_integrity_catches_decreasing_factor():
    """The dump-time backstop: factor decreasing along dates (mixed-convention
    fragments, upstream inconsistency) must fail integrity. The manifest param
    guard refuses the mix at resume time; this catches anything that slips past.
    """
    r = _stub_panel(n=30)
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
        fetch.fetch_cross_section()


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

    r = fetch.fetch_cross_section(client=_StubClient(), max_ec=1000)
    assert len(calls) == 1, f"expected ONE request, got {len(calls)}"
    assert calls[0]["limit"] == 10000 and calls[0]["offset"] == 0
    assert r.metering.ec_cost == pytest.approx(800.0)
    assert r.metering.requests == 2  # screener.query + stocks.list
    assert len(r.frame) == 2
    # instruments must be Qlib-style <SUFFIX><code> (bare codes poison scoring)
    assert set(r.frame["instrument"]) == {"SH600519", "SZ000858"}
    # a second request (e.g. pagination) would be the EC bomb amplifier
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section(client=_StubClient(), max_ec=500)


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

    r = fetch.fetch_cross_section(client=_StubClient(), max_ec=10**9)
    assert r.metering.ec_cost == pytest.approx(621_399.0), (
        "metering must fall back to client.last_ec_cost"
    )
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section(client=_StubClient(), max_ec=3000)


def test_field_declaration_two_way_coverage(tmp_path):
    """M2 DoD: zero phantom fields; missing ones are explicitly reported.

    The stub serves a narrow field set (pe only among the valuation family),
    so a strict emitted==declared assertion would be wrong here. The hard
    guarantee is ZERO PHANTOM (every emitted field has a unit declaration).
    Missing fields must be enumerated in the report, never silently ignored.
    """
    r = _stub_panel()
    emitted = set(r.frame["field"].unique())
    check = panel.assert_field_declaration_roundtrip(emitted, config.FIELD_UNITS)
    assert check["phantom"] == [], f"undeclared fields emitted: {check['phantom']}"
    assert isinstance(check["missing"], list)  # reported, never silently ignored
    # The stub valuation serves pe only; `factor` is emitted explicitly
    # (hfq_factor convention). Missing fields are EXPLICIT here - never
    # silently ignored.
    allowed_missing = (
        (set(config.VALUATION_FIELDS) - {"pe"})
        | set(config.DAILY_BASIC_FIELDS)  # daily_basic: upstream-gated, only
        #   entered via --daily-basic which this stub panel does not use
        | set(config.MARGIN_FIELDS)  # margin: Layer-3 accumulation store, a
        #   separate pipeline from the history panel this test inspects
        | {"vwap"}
    )
    # ^ vwap: derived at dump time (dump._append_vwap), absent from the
    #   fetched long panel this test inspects.
    assert set(check["missing"]).issubset(allowed_missing), check["missing"]
    assert "factor" not in check["missing"], "factor must be emitted under hfq_factor"


def test_dump_selfcheck_without_qlib(tmp_path):
    """TIER 1 (mandatory): .bin is valid WITHOUT importing Qlib at all."""
    r = _stub_panel()
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
    r = _stub_panel()
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
    r = fetch.fetch_cross_section(client=_StubScreener(), as_of=DAY, max_ec=10**9)
    assert r.metering.mode == "live"
    assert len(r.frame) > 0
    out = cross_section.write_score_csv(r, tmp_path / "score", as_of=DAY)
    assert out.exists()

    meta = json.loads((tmp_path / "score" / f"{DAY}.meta.json").read_text())
    assert meta["history"] is False
    assert meta["metering"]["mode"] == "live"

    read = qlib_io.read_score(out, DAY)
    assert read.series is not None
    assert read.backend in {"ScoreFileModel", "local-equivalent"}
    assert len(read.series) == len(r.frame)
    # Degradation is explicit, never silent: a local fallback must say why.
    if read.backend == "local-equivalent":
        assert read.note


def test_accumulation_cold_start_guard(tmp_path):
    r = fetch.fetch_cross_section(client=_StubScreener(), as_of=DAY, max_ec=10**9)
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
    r = fetch.fetch_cross_section(client=_StubScreener(), as_of=DAY, max_ec=10**9)
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


# --- P0-3 (2026-11-24): a date-less accumulate stored ONE rolling file --------
def test_accumulate_rejects_missing_or_non_iso_as_of(tmp_path):
    r = fetch.fetch_cross_section(client=_StubScreener(), as_of=DAY, max_ec=10**9)
    for bad in (None, "", "snapshot", "20261124"):
        with pytest.raises(ValueError, match="ISO date"):
            cross_section.accumulate(r.frame, tmp_path / "acc", as_of=bad)
    assert not list((tmp_path / "acc").glob("*.parquet"))


def test_scan_accumulation_ignores_non_date_stems(tmp_path):
    """A legacy None.parquet must not count as a day nor enter the panel."""
    r = fetch.fetch_cross_section(client=_StubScreener(), as_of=DAY, max_ec=10**9)
    acc = tmp_path / "acc"
    cross_section.accumulate(r.frame, acc, as_of="2026-11-23")
    cross_section.accumulate(r.frame, acc, as_of="2026-11-24")
    r.frame.to_parquet(acc / "None.parquet", index=False)  # <= 0.7.0 artifact

    days, ignored = cross_section.scan_accumulation(acc)
    assert [d.stem for d in days] == ["2026-11-23", "2026-11-24"]
    assert [f.name for f in ignored] == ["None.parquet"]

    frame, status = cross_section.promote_to_panel(
        acc, min_days=2, out_parquet=tmp_path / "panel.parquet"
    )
    assert frame is not None and "promoted 2 days" in status
    assert "ignored 1 non-date" in status
    assert len(pd.read_parquet(tmp_path / "panel.parquet")) == 2 * len(r.frame)

    frame3, status3 = cross_section.promote_to_panel(
        acc, min_days=3, out_parquet=tmp_path / "p3.parquet"
    )
    assert frame3 is None and "only 2 days" in status3


def test_accumulate_cli_defaults_as_of_to_today(tmp_path, monkeypatch, capsys):
    """Two CLI runs on two days -> two PIT files, correct available_from.

    Before the fix both runs wrote the same ``None.parquet``: history_days
    stayed 1 and the promotion guard could never pass.
    """
    from wmshare_qlib import cli

    def fake_snapshot(symbols, **kw):
        day = kw.get("as_of") or "1970-01-01"
        return fetch.FetchResult(
            frame=pd.DataFrame(
                {"instrument": ["SH600519"], "datetime": [day], "score": [1.0]}
            ),
            metering=fetch.Metering(mode="live", requests=3, ec_cost=309.0),
            warnings=[],
        )

    monkeypatch.setattr(fetch, "fetch_cross_section_snapshot", fake_snapshot)
    out = tmp_path / "acc"
    ns = cli.build_parser().parse_args(
        [
            "accumulate",
            "--via",
            "snapshot",
            "--symbols",
            "600519",
            "000858",
            "601318",
            "--max-ec",
            "1000",
            "--out",
            str(out),
        ]
    )
    for day in ("2026-11-23", "2026-11-24"):
        monkeypatch.setattr(cli, "_today_iso", lambda d=day: d)
        cli.cmd_accumulate(ns)

    days, ignored = cross_section.scan_accumulation(out)
    assert [d.stem for d in days] == ["2026-11-23", "2026-11-24"]
    assert ignored == []
    text = capsys.readouterr().out
    assert '"history_days": 2' in text
    assert '"available_from": "2026-11-23"' in text
