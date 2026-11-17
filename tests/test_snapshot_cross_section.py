"""P0-3 snapshot-batch cross-section tests. ZERO real EC: local stubs only.

Stub shapes mirror the SDK contract verified in client.py:
``kline.snapshot(list) -> BatchResult(results={code: 23-field MAP}, errors={})``
with field names from ``_SNAPSHOT_BATCH_FIELDS``. Score semantics are the
DECLARED technical composite (config.SNAPSHOT_METRICS): chg_20d / roi_1y /
ma60_up higher_better, mv lower_better - the tests pin the direction flips so
a silent sign error cannot pass.
"""

from __future__ import annotations

import argparse

import pandas as pd
import pytest

from wmshare_qlib import cli, config, fetch

SYMS = ["600519", "000858", "601318", "000001"]
DAY = "2026-11-17"


def _fields(sym: str, *, chg_20d: float, roi_1y: float, ma60_up: int,
            mv: float, is_st: bool = False, is_suspend: bool = False,
            drop_roi: bool = False) -> dict:
    """23-field-shaped MAP with only the scored fields populated (the rest
    are present-but-unused snapshot columns)."""
    return {
        "symbol": sym,
        "name": f"name-{sym}",
        "close": 10.0,
        "percent": 0.01,
        "volume": 1000.0,
        "amount": 1e6,
        "mv": mv,
        "mv_free": mv * 0.8,
        "kline_date": None,   # batch path: index not materialized (SDK contract)
        "kline_sas": None,
        "ma20": 9.9, "ma60": 9.8, "ma120": 9.7,
        "chg_5d": 0.01, "chg_10d": 0.02, "chg_20d": chg_20d, "chg_90d": 0.05,
        "roi_1y": None if drop_roi else roi_1y,
        "vol_ratio_5": 1.1, "ma60_up": ma60_up,
        "industry": "bank", "is_st": is_st, "is_suspend": is_suspend,
    }


class _StubBatchResult:
    """BatchResult-shaped: results/errors dicts + request count."""

    def __init__(self, results: dict[str, dict]):
        self.results = results
        self.errors: dict[str, str] = {}
        self.requests = 1


class _StubKline:
    def __init__(self, results: dict[str, dict]):
        self._results = results

    def snapshot(self, symbols):
        return _StubBatchResult({s: self._results[s] for s in symbols
                                 if s in self._results})


class _StubStocks:
    def list(self, _df=False):
        return pd.DataFrame({
            "symbol": SYMS,
            "exchange": ["sh", "sz", "sh", "sz"],
        })


class _StubClient:
    def __init__(self, results: dict[str, dict], ec: float = 400.0):
        self.kline = _StubKline(results)
        self.stocks = _StubStocks()
        self.last_ec_cost = ec


def _default_results() -> dict[str, dict]:
    # Deterministic ordering signal: 600519 strongest momentum + smallest mv;
    # 000858 weakest momentum + largest mv. 601318 middle. 000001 has a
    # missing roi_1y -> must be DROPPED (never filled with 0).
    return {
        "600519": _fields("600519", chg_20d=0.30, roi_1y=0.40, ma60_up=1, mv=2e11),
        "601318": _fields("601318", chg_20d=0.10, roi_1y=0.10, ma60_up=1, mv=9e11),
        "000858": _fields("000858", chg_20d=-0.20, roi_1y=-0.10, ma60_up=0, mv=5e11),
        "000001": _fields("000001", chg_20d=0.05, roi_1y=0.05, ma60_up=1, mv=2e11,
                          drop_roi=True),
    }


def test_snapshot_score_directions_and_drops():
    client = _StubClient(_default_results(), ec=400.0)
    r = fetch.fetch_cross_section_snapshot(
        SYMS, live=True, as_of=DAY, client=client, max_ec=10_000,
    )
    # 000001 dropped for the missing roi_1y (anti-pitfall #5: never filled)
    assert any("missing metric" in w for w in r.warnings), r.warnings
    scores = r.frame.set_index("instrument")["score"]
    assert set(scores.index) == {"SH600519", "SH601318", "SZ000858"}
    # direction flips: strongest momentum + smallest mv must score highest;
    # weakest momentum + (here) larger mv must score lowest
    assert scores["SH600519"] > scores["SH601318"] > scores["SZ000858"]
    # metering: stocks.list + one batch request
    assert r.metering.requests == 2
    assert r.metering.ec_cost == pytest.approx(400.0)
    # the score family is LABELED - never blurs with the screener fundamentals
    assert "TECHNICAL COMPOSITE" in r.metering.note


def test_snapshot_drops_st_and_suspended_rows():
    results = _default_results()
    results["601318"] = _fields("601318", chg_20d=0.10, roi_1y=0.10, ma60_up=1,
                                mv=9e11, is_st=True)
    results["000858"] = _fields("000858", chg_20d=-0.20, roi_1y=-0.10, ma60_up=0,
                                mv=5e11, is_suspend=True)
    client = _StubClient(results, ec=400.0)
    r = fetch.fetch_cross_section_snapshot(
        SYMS, live=True, as_of=DAY, client=client, max_ec=10_000,
    )
    assert any("ST row" in w for w in r.warnings)
    assert any("suspended row" in w for w in r.warnings)
    assert set(r.frame["instrument"]) == {"SH600519"}


def test_snapshot_posthoc_ec_ceiling_raises():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results(), ec=50_000.0)
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section_snapshot(
            SYMS, live=True, as_of=DAY, client=client, max_ec=10_000,
        )


def test_snapshot_requires_max_ec():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results())
    with pytest.raises(normalize.NormalizeError, match="max_ec"):
        fetch.fetch_cross_section_snapshot(
            SYMS, live=True, as_of=DAY, client=client,
        )


def test_snapshot_requires_three_symbols_minimum():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results())
    with pytest.raises(normalize.NormalizeError, match=">= 3"):
        fetch.fetch_cross_section_snapshot(
            SYMS[:2], live=True, as_of=DAY, client=client, max_ec=10_000,
        )


def test_snapshot_is_live_only():
    from wmshare_qlib import normalize

    with pytest.raises(normalize.NormalizeError, match="live-only"):
        fetch.fetch_cross_section_snapshot(SYMS, live=False, max_ec=10_000)


def test_snapshot_universe_intersected_with_stocks_list():

    results = _default_results()
    results["999999"] = _fields("999999", chg_20d=0.99, roi_1y=0.99, ma60_up=1,
                                mv=1e9)  # NOT in stocks.list -> must be dropped
    client = _StubClient(results, ec=400.0)
    r = fetch.fetch_cross_section_snapshot(
        [*SYMS, "999999"], live=True, as_of=DAY, client=client, max_ec=10_000,
    )
    assert any("not in stocks.list" in w for w in r.warnings)
    assert "999999" not in r.frame["instrument"].str.slice(2).tolist()


# --- CLI guard rails (fire BEFORE any network/EC) ----------------------------
def test_cli_snapshot_requires_symbols():
    with pytest.raises(SystemExit, match="--symbols"):
        cli.cmd_dump_cross_section(_ns(via="snapshot", live=True, max_ec=1.0))


def test_cli_snapshot_refuses_demo():
    with pytest.raises(SystemExit):
        # snapshot is live-only; the demo client would route there and refuse
        cli.cmd_dump_cross_section(_ns(via="snapshot", live=False, max_ec=1.0,
                                       symbols=SYMS))


def _ns(**kw):
    base = dict(live=False, as_of=None, via="screener", symbols=None,
                max_ec=None, idx=None, out="out/score", min_days=60)
    base.update(kw)
    return argparse.Namespace(**base)


def test_snapshot_chunks_large_universes():
    """P1-1 ops finding (2026-11-17): ~81-ticket single batches fail
    server-side; the cross-section must chunk (config.SNAPSHOT_BATCH_CHUNK)
    and SUM per-chunk EC."""
    seen_chunks: list[int] = []

    class _ChunkKline:
        def snapshot(self, symbols):
            seen_chunks.append(len(symbols))
            return _StubBatchResult({s: _fields(s, chg_20d=0.1, roi_1y=0.1,
                                                ma60_up=1, mv=1e10)
                                     for s in symbols})

    class _ChunkClient:
        def __init__(self):
            self.kline = _ChunkKline()
            self.stocks = _StubStocks()
            self.last_ec_cost = 2000.0  # per-chunk reading

    n = config.SNAPSHOT_BATCH_CHUNK * 2 + 5
    syms = [f"{600000 + i}" for i in range(n)]
    client = _ChunkClient()
    client.stocks = type("S", (), {"list": staticmethod(
        lambda _df=False: pd.DataFrame(
            {"symbol": syms, "exchange": ["sh"] * n}))})()
    r = fetch.fetch_cross_section_snapshot(
        syms, live=True, as_of=DAY, client=client, max_ec=10**9,
    )
    assert seen_chunks == [config.SNAPSHOT_BATCH_CHUNK,
                           config.SNAPSHOT_BATCH_CHUNK, 5]
    assert len(r.frame) == n
    # per-chunk readings SUMMED: 3 chunks x 2000
    assert r.metering.ec_cost == pytest.approx(6000.0)
