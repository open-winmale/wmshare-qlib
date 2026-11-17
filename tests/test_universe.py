"""P1-1 universe-file tests. Offline except where a stub stands in.

Coverage: .bin-header range derivation against a REAL dumped demo panel,
the Qlib-convention writer, codes-file sources (qlib-style + bare via
stocks.list mapping), index-members stub path, the snapshot hygiene filter
(ST/suspended dropped with warnings), and the panel-coverage intersection
(members without dumped data are dropped, never fabricated ranges).
"""

from __future__ import annotations

import argparse

import pandas as pd
import pytest

from wmshare_qlib import cli, dump, fetch

SYMBOLS = ["600519", "000858", "601318"]


def _dumped(tmp_path):
    """Dump a real demo panel and return (qlib_dir, panel)."""
    from wmshare_qlib import fetch as f

    r = f.fetch_history_panel(SYMBOLS, n=30)
    qlib_dir = tmp_path / "qlib_data"
    dump.dump_panel(r.frame, qlib_dir)
    return qlib_dir, r.frame


# --- range derivation ---------------------------------------------------------
def test_instrument_date_ranges_match_dumped_panel(tmp_path):
    qlib_dir, frame = _dumped(tmp_path)
    ranges = dump.instrument_date_ranges(qlib_dir)
    assert set(ranges) == {"SH600519", "SZ000858", "SH601318"}
    for inst in ranges:
        sub = frame[frame["instrument"] == inst]
        assert ranges[inst][0] == sub["date"].min()
        assert ranges[inst][1] == sub["date"].max()


def test_write_universe_file_is_qlib_convention(tmp_path):
    qlib_dir, _ = _dumped(tmp_path)
    ranges = dump.instrument_date_ranges(qlib_dir)
    out = dump.write_universe_file(qlib_dir, "my_uni", ranges)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].split("\t") == ["SH600519", *ranges["SH600519"]]
    assert all(len(ln.split("\t")) == 3 for ln in lines)
    with pytest.raises(ValueError, match="name"):
        dump.write_universe_file(qlib_dir, "../evil", ranges)


def test_write_universe_requires_dumped_panel(tmp_path):
    with pytest.raises(SystemExit, match="dump-history"):
        cli.cmd_write_universe(argparse.Namespace(
            qlib_dir=str(tmp_path / "nope"), name="x", index=None,
            codes_file=None, live=False, exclude_st=False,
            exclude_suspended=False, max_ec=None, freq="day",
        ))


# --- codes-file path (offline: demo client maps bare codes) -------------------
def test_codes_file_universe_intersected_with_panel(tmp_path):
    qlib_dir, _ = _dumped(tmp_path)
    codes = tmp_path / "codes.txt"
    # bare code maps via demo stocks.list; SH-style passes through; junk drops
    codes.write_text("# comment\n600519\nSZ000858\n999999\n", encoding="utf-8")
    args = argparse.Namespace(qlib_dir=str(qlib_dir), name="uni", index=None,
                              codes_file=str(codes), live=False, exclude_st=False,
                              exclude_suspended=False, max_ec=None, freq="day")
    cli.cmd_write_universe(args)
    lines = (qlib_dir / "instruments" / "uni.txt").read_text().strip().splitlines()
    insts = {ln.split("\t")[0] for ln in lines}
    assert insts == {"SH600519", "SZ000858"}, "junk 999999 must be dropped"


# --- index-members path (stubbed; live-only in production) -------------------
class _StubIndices:
    def __init__(self, codes):
        self._codes = codes

    def members(self, key):
        return pd.DataFrame({"symbol": self._codes, "weight": [0.01] * len(self._codes)})


class _StubStocks:
    def list(self, _df=False):
        return pd.DataFrame({"symbol": SYMBOLS, "exchange": ["sh", "sz", "sh"]})


class _StubLiveClient:
    def __init__(self, member_codes):
        self.indices = _StubIndices(member_codes)
        self.stocks = _StubStocks()
        self.last_ec_cost = 5.0


def test_index_members_live_only(tmp_path, monkeypatch):
    """--index on a demo client must refuse (no indices namespace)."""
    import wmshare

    qlib_dir, _ = _dumped(tmp_path)
    args = argparse.Namespace(qlib_dir=str(qlib_dir), name="csi300",
                              index="index.hs300", codes_file=None, live=False,
                              exclude_st=False, exclude_suspended=False,
                              max_ec=None, freq="day")
    monkeypatch.setattr(cli.fetch, "_client", lambda live: wmshare.demo_client())
    with pytest.raises(SystemExit, match="--live"):
        cli.cmd_write_universe(args)


def test_index_members_stub_path_writes_universe(tmp_path, monkeypatch):
    qlib_dir, _ = _dumped(tmp_path)
    stub = _StubLiveClient(["600519", "000858", "300750"])  # 300750 not dumped
    monkeypatch.setattr(cli.fetch, "_client", lambda live: stub)
    args = argparse.Namespace(qlib_dir=str(qlib_dir), name="csi300",
                              index="index.hs300", codes_file=None, live=True,
                              exclude_st=False, exclude_suspended=False,
                              max_ec=None, freq="day")
    cli.cmd_write_universe(args)
    lines = (qlib_dir / "instruments" / "csi300.txt").read_text().strip().splitlines()
    insts = {ln.split("\t")[0] for ln in lines}
    assert insts == {"SH600519", "SZ000858"}, "member without dumped data must drop"


# --- hygiene filter (snapshot stub) ------------------------------------------
class _StubKlineSnap:
    def __init__(self, flags):
        self._flags = flags  # {code: {"is_st": bool, "is_suspend": bool}}

    def snapshot(self, symbols):
        class _R:
            results = {s: {"is_st": self._flags.get(s, {}).get("is_st", False),
                           "is_suspend": self._flags.get(s, {}).get("is_suspend", False)}
                       for s in symbols}
            errors = {}
            requests = 1
        return _R()


class _HygClient:
    def __init__(self, flags):
        self.kline = _StubKlineSnap(flags)
        self.stocks = _StubStocks()
        self.last_ec_cost = 400.0


def test_hygiene_filter_drops_st_and_suspended():
    client = _HygClient({"601318": {"is_st": True},
                         "000858": {"is_suspend": True}})
    r = fetch.filter_universe_hygiene(
        SYMBOLS, client=client, exclude_st=True, exclude_suspended=True,
        max_ec=10_000,
    )
    assert r.frame["instrument"].tolist() == ["SH600519"]
    assert any("ST" in w for w in r.warnings)
    assert any("suspended" in w for w in r.warnings)


def test_hygiene_requires_max_ec_and_three_symbols():
    from wmshare_qlib import normalize

    client = _HygClient({})
    with pytest.raises(normalize.NormalizeError, match="max_ec"):
        fetch.filter_universe_hygiene(SYMBOLS, client=client, max_ec=None)
    with pytest.raises(normalize.NormalizeError, match=">= 3"):
        fetch.filter_universe_hygiene(SYMBOLS[:2], client=client, max_ec=10_000)


def test_cli_hygiene_requires_live_and_max_ec(tmp_path):
    qlib_dir, _ = _dumped(tmp_path)
    codes = tmp_path / "codes.txt"
    codes.write_text("600519\n000858\n601318\n", encoding="utf-8")
    base = dict(qlib_dir=str(qlib_dir), name="uni", index=None,
                codes_file=str(codes), live=False, exclude_st=True,
                exclude_suspended=False, max_ec=None, freq="day")
    with pytest.raises(SystemExit, match="--live"):
        cli.cmd_write_universe(argparse.Namespace(**base))
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_write_universe(argparse.Namespace(**{**base, "live": True}))
