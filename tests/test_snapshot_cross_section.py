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
        SYMS, as_of=DAY, client=client, max_ec=10_000,
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
        SYMS, as_of=DAY, client=client, max_ec=10_000,
    )
    assert any("ST row" in w for w in r.warnings)
    assert any("suspended row" in w for w in r.warnings)
    assert set(r.frame["instrument"]) == {"SH600519"}


def test_snapshot_posthoc_ec_ceiling_raises():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results(), ec=50_000.0)
    with pytest.raises(normalize.NormalizeError, match="ceiling"):
        fetch.fetch_cross_section_snapshot(
            SYMS, as_of=DAY, client=client, max_ec=10_000,
        )


def test_snapshot_requires_max_ec():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results())
    with pytest.raises(normalize.NormalizeError, match="max_ec"):
        fetch.fetch_cross_section_snapshot(
            SYMS, as_of=DAY, client=client,
        )


def test_snapshot_requires_three_symbols_minimum():
    from wmshare_qlib import normalize

    client = _StubClient(_default_results())
    with pytest.raises(normalize.NormalizeError, match=">= 3"):
        fetch.fetch_cross_section_snapshot(
            SYMS[:2], as_of=DAY, client=client, max_ec=10_000,
        )


def test_snapshot_universe_intersected_with_stocks_list():

    results = _default_results()
    results["999999"] = _fields("999999", chg_20d=0.99, roi_1y=0.99, ma60_up=1,
                                mv=1e9)  # NOT in stocks.list -> must be dropped
    client = _StubClient(results, ec=400.0)
    r = fetch.fetch_cross_section_snapshot(
        [*SYMS, "999999"], as_of=DAY, client=client, max_ec=10_000,
    )
    assert any("not in stocks.list" in w for w in r.warnings)
    assert "999999" not in r.frame["instrument"].str.slice(2).tolist()


# --- contract check against the REAL installed SDK (zero EC) -----------------
def test_real_sdk_exposes_fields_and_truncated():
    """Pin the actual upstream contract we rely on, NOT a stub: a future SDK
    that drops ``_fields``/``truncated`` must turn this red instead of
    silently degrading to the fallback branch forever. Introspection only -
    the real facade probe itself is a live-suite concern (no EC here)."""
    import inspect

    import wmshare
    from wmshare.client import BatchResult

    assert "truncated" in BatchResult.__dataclass_fields__
    # 3.x: zero network at construction (credentials parse or public-pool mark)
    client = wmshare.Client.from_env()
    params = inspect.signature(client.kline.snapshot).parameters
    assert "_fields" in params, "kline.snapshot lost the _fields projection kwarg"
    assert fetch._supports_fields(client) is True


# --- CLI guard rails (fire BEFORE any network/EC) ----------------------------
def test_cli_snapshot_requires_symbols(monkeypatch):
    monkeypatch.setattr(
        cli.fetch, "_client",
        lambda **kw: (_ for _ in ()).throw(AssertionError("guard must fire pre-client")),
    )
    with pytest.raises(SystemExit, match="--symbols"):
        cli.cmd_dump_cross_section(_ns(via="snapshot", max_ec=1.0))


def test_cli_snapshot_max_ec_guard_fires_before_client(monkeypatch):
    """max_ec is REQUIRED on the snapshot path; the refusal must happen in the
    fetch layer BEFORE any client is built (0.7.0: a built client silently
    selects the public-pool free trial, so guard order is what keeps
    argument mistakes off the network)."""
    monkeypatch.setattr(
        cli.fetch, "_client",
        lambda **kw: (_ for _ in ()).throw(AssertionError("guard must fire pre-client")),
    )
    with pytest.raises(SystemExit, match="max_ec"):
        cli.cmd_dump_cross_section(_ns(via="snapshot", symbols=SYMS))


def _ns(**kw):
    base = dict(as_of=None, via="screener", symbols=None,
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
        syms, as_of=DAY, client=client, max_ec=10**9,
    )
    assert seen_chunks == [config.SNAPSHOT_BATCH_CHUNK,
                           config.SNAPSHOT_BATCH_CHUNK, 5]
    assert len(r.frame) == n
    # per-chunk readings SUMMED: 3 chunks x 2000
    assert r.metering.ec_cost == pytest.approx(6000.0)


# --- wmshare latest-spec adoption: _fields projection + truncated guard ------
class _ProjectingBatchResult:
    """BatchResult-shaped with the latest-spec attributes (truncated flag)."""

    def __init__(self, results: dict[str, dict], *, truncated: bool = False):
        self.results = results
        self.errors: dict[str, str] = {}
        self.requests = 1
        self.truncated = truncated


class _FieldsKline:
    """``snapshot(symbols, _fields=None)`` that CONSUMES ``_fields`` like the
    SDK's project_fields (param-sensitive stub discipline: a stub that ignores
    its arguments cannot detect a wrong projection request)."""

    def __init__(self, results: dict[str, dict]):
        self._results = results
        self.requested_fields: list[list[str] | None] = []

    def snapshot(self, symbols, _fields=None):
        self.requested_fields.append(list(_fields) if _fields else None)
        rows = {s: self._results[s] for s in symbols if s in self._results}
        if _fields:
            keep = set(_fields)
            rows = {s: {k: v for k, v in row.items() if k in keep}
                    for s, row in rows.items()}
        return _ProjectingBatchResult(rows)


class _FieldsClient:
    def __init__(self, results: dict[str, dict], ec: float = 400.0):
        self.kline = _FieldsKline(results)
        self.stocks = _StubStocks()
        self.last_ec_cost = ec


def test_supports_fields_detector():
    """Signature probe: latest-spec facades have ``_fields``, older ones do
    not - and a client without kline degrades to False (no projection)."""
    assert fetch._supports_fields(_FieldsClient(_default_results()))
    assert not fetch._supports_fields(_StubClient(_default_results()))

    class _NoKline:
        pass

    assert not fetch._supports_fields(_NoKline())


def test_snapshot_projects_fields_when_supported():
    """Latest-spec SDKs: the cross-section requests a SUPERSET of every field
    downstream touches (is_st/is_suspend + all metric codes) and the projected
    (narrow) results still produce identical scores. EC is unchanged either
    way - projection only narrows the response body."""
    client = _FieldsClient(_default_results(), ec=400.0)
    r = fetch.fetch_cross_section_snapshot(
        SYMS, as_of=DAY, client=client, max_ec=10_000,
    )
    assert client.kline.requested_fields, "projection kwarg must be sent"
    want = {"is_st", "is_suspend", "chg_20d", "roi_1y", "ma60_up", "mv"}
    assert set(client.kline.requested_fields[0]) == want
    scores = r.frame.set_index("instrument")["score"]
    assert set(scores.index) == {"SH600519", "SH601318", "SZ000858"}
    assert scores["SH600519"] > scores["SH601318"] > scores["SZ000858"]
    assert r.metering.ec_cost == pytest.approx(400.0)  # EC identical


def test_snapshot_truncated_reports_silent_omissions():
    """BatchResult.truncated (latest spec): server-side silent omissions must
    surface as an explicit warning - before the flag existed a ticket missing
    from results was simply never scored, invisibly."""
    class _TruncKline(_FieldsKline):
        def snapshot(self, symbols, _fields=None):
            r = super().snapshot(symbols, _fields)
            r.results.pop("000858", None)  # requested 4, served 3
            r.truncated = True
            return r

    client = _FieldsClient(_default_results(), ec=400.0)
    client.kline = _TruncKline(_default_results())
    r = fetch.fetch_cross_section_snapshot(
        SYMS, as_of=DAY, client=client, max_ec=10_000,
    )
    assert any("silently omitted" in w and "SUBSET" in w for w in r.warnings), r.warnings
    assert "SZ000858" not in set(r.frame["instrument"])


def test_hygiene_projects_fields_and_warns_truncated():
    """Hygiene pass: projects to exactly is_st/is_suspend and surfaces the
    truncation warning with the omitted ticket named."""
    flags = {"600519": {"is_st": False, "is_suspend": False},
             "000858": {"is_st": False, "is_suspend": True},
             "601318": {"is_st": True, "is_suspend": False}}

    class _HygKline:
        def __init__(self):
            self.requested: list[list[str] | None] = []

        def snapshot(self, symbols, _fields=None):
            self.requested.append(list(_fields) if _fields else None)
            rows = {s: dict(flags.get(s, {})) for s in symbols if s != "601318"}
            if _fields:
                keep = set(_fields)
                rows = {s: {k: v for k, v in row.items() if k in keep}
                        for s, row in rows.items()}
            return _ProjectingBatchResult(rows, truncated=True)

    kline = _HygKline()
    client = type("C", (), {"kline": kline, "stocks": _StubStocks(),
                            "last_ec_cost": 400.0})()
    r = fetch.filter_universe_hygiene(
        ["600519", "000858", "601318"], client=client,
        exclude_st=True, exclude_suspended=True, max_ec=10_000,
    )
    assert kline.requested and kline.requested[0] == ["is_st", "is_suspend"]
    assert any("silently omitted" in w and "601318" in w for w in r.warnings)
    # kept only the clean-and-served ticket; 000858 suspended, 601318 omitted
    assert r.frame["instrument"].tolist() == ["SH600519"]
