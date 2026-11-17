"""P1-3 ops tests: transient retry/backoff, EC estimator, --dry-run quotes.

Retry tests fake ``wmshare.errors`` ON THE PACKAGE (``from wmshare import
errors`` resolves via the parent module's attribute, not sys.modules) and
inject the clock (fetch._sleep), so the policy table is pinned exactly:
TransportError / ServerError / rate-limit-429 retry; QuotaError (a LimitError
subclass!), UsageError, ApiError never do. retry_after wins over backoff.
"""

from __future__ import annotations

import argparse

import pytest
import wmshare

from wmshare_qlib import cli, config, estimate, fetch


# --- fake SDK error module ----------------------------------------------------
class _WErr(RuntimeError):
    def __init__(self, msg, code="", status=None, retry_after=None):
        super().__init__(msg)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _fake_errors(monkeypatch):
    mod = type("errors", (), {})()
    for name in ("TransportError", "ServerError", "LimitError", "QuotaError",
                 "AuthError", "UsageError", "ApiError"):
        setattr(mod, name, type(name, (_WErr,), {}))
    monkeypatch.setattr(wmshare, "errors", mod, raising=True)
    return mod


@pytest.fixture
def no_sleep(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(fetch, "_sleep", lambda s: seen.append(s))
    return seen


# --- retry policy table -------------------------------------------------------
def test_retry_retries_transient_and_meters_every_attempt(monkeypatch, no_sleep):
    _fake_errors(monkeypatch)
    calls = {"n": 0}
    metered = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise wmshare.errors.TransportError("read timeout")
        return "ok"

    out = fetch.call_with_retry(
        fn, label="x/kline",
        meter_cb=lambda: metered.__setitem__("n", metered["n"] + 1),
        warn_cb=lambda w: None,
    )
    assert out == "ok"
    assert calls["n"] == 2
    assert metered["n"] == 2, "meter must fire after EVERY attempt (EC honesty)"
    assert no_sleep and no_sleep[0] >= fetch.RETRY_BASE_DELAY


def test_retry_never_retries_quota(monkeypatch, no_sleep):
    mod = _fake_errors(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        # 429 WITH exhausted quota: the LimitError-shaped retry must NOT fire
        raise mod.QuotaError("EC_BALANCE_EXHAUSTED", code="EC_BALANCE_EXHAUSTED",
                             status=429)

    with pytest.raises(mod.QuotaError):
        fetch.call_with_retry(fn, label="x", meter_cb=lambda: None,
                              warn_cb=lambda w: None)
    assert calls["n"] == 1, "quota exhaustion must NOT retry (LimitError subclass!)"
    assert no_sleep == []


def test_retry_never_retries_permanent(monkeypatch, no_sleep):
    mod = _fake_errors(monkeypatch)
    for exc in (mod.UsageError("bad param"), mod.ApiError("biz error")):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise exc

        with pytest.raises(type(exc)):
            fetch.call_with_retry(fn, label="x", meter_cb=lambda: None,
                                  warn_cb=lambda w: None)
        assert calls["n"] == 1
    assert no_sleep == []


def test_retry_respects_retry_after(monkeypatch):
    mod = _fake_errors(monkeypatch)
    seen: list[float] = []
    monkeypatch.setattr(fetch, "_sleep", lambda s: seen.append(s))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise mod.LimitError("rate limited", code="rate_limited", status=429,
                                 retry_after=3.0)
        return "ok"

    warnings: list[str] = []
    out = fetch.call_with_retry(fn, label="x", meter_cb=lambda: None,
                                warn_cb=warnings.append)
    assert out == "ok"
    # retry_after (3s) must win over the backoff curve (0.5s); jitter adds
    # [0, 0.25) on top of the chosen delay
    assert seen and 3.0 <= seen[0] < 3.3
    assert any("attempt 1/2" in w for w in warnings), "retries are never silent"


def test_retry_exhausts_and_reraises(monkeypatch, no_sleep):
    mod = _fake_errors(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise mod.TransportError("down")

    with pytest.raises(mod.TransportError):
        fetch.call_with_retry(fn, label="x", meter_cb=lambda: None,
                              warn_cb=lambda w: None)
    assert calls["n"] == fetch.RETRY_ATTEMPTS


# --- EC estimator -------------------------------------------------------------
def test_estimator_formulas_match_measured_anchors():
    # 3-point fit: 156 + 67n -> n=10: 826, n=60: 4176, served-500: 33656
    assert estimate.kline_call(10) == pytest.approx(826.0)
    assert estimate.kline_call(60) == pytest.approx(4176.0)
    assert estimate.kline_call(500) == pytest.approx(33656.0)
    assert estimate.moneyflow_call(15) == pytest.approx(396.0)
    # bulk hfq live, n=60: 2 kline + 4 valuation + moneyflow
    bulk = estimate.bulk_symbol(60, live=True, adjust_mode="hfq_factor",
                                valuation_metrics=4)
    assert bulk == pytest.approx(2 * 4176.0 + 4 * 712.0 + (156.0 + 16 * 60))
    # demo single pull, pe-only, moneyflow capped at 30
    demo = estimate.bulk_symbol(60, live=False, adjust_mode="qfq_const",
                                valuation_metrics=1)
    assert demo == pytest.approx(4176.0 + 712.0 + (156.0 + 16 * 30))
    # tail k=6, valuation due: 2 kline + moneyflow + 4 valuation
    tail = estimate.tail_symbol(6, valuation_due=True)
    assert tail == pytest.approx(2 * (156.0 + 67 * 6) + (156.0 + 16 * 6) + 4 * 712.0)
    # snapshot: stocks.list + per ticket
    assert estimate.snapshot_batch(300) == pytest.approx(66.0 + 300 * 100.0)


# --- --dry-run quotes ---------------------------------------------------------
def test_dry_run_dump_history_quotes_without_store_or_request(tmp_path, capsys):
    out_dir = tmp_path / "out"
    args = argparse.Namespace(symbols=["600519", "000858"], live=True, n=60,
                              freq="day", out=str(out_dir), store=None, workers=1,
                              max_ec=1e9, adjust=None, resume=False, dry_run=True)
    cli.cmd_dump_history(args)
    captured = capsys.readouterr().out
    assert "[dry-run]" in captured
    assert not (tmp_path / "out" / "_resume").exists(), "dry-run must not write a store"
    per = 2 * (156.0 + 67 * 60) + 4 * 712.0 + (156.0 + 16 * 60)
    assert f"{per * 2:.1f}" in captured  # 2 symbols


def test_dry_run_flags_ceiling_breach(tmp_path, capsys):
    args = argparse.Namespace(symbols=["600519"], live=True, n=60,
                              freq="day", out=str(tmp_path / "o"), store=None,
                              workers=1, max_ec=1000.0, adjust=None, resume=False,
                              dry_run=True)
    cli.cmd_dump_history(args)
    assert "exceeds max_ec" in capsys.readouterr().out


def test_dry_run_cross_section_snapshot_and_screener(capsys):
    args = argparse.Namespace(via="snapshot", live=True, as_of=None,
                              symbols=["a", "b", "c"], max_ec=1e9,
                              idx=None, out="out/score", dry_run=True)
    cli.cmd_dump_cross_section(args)
    out = capsys.readouterr().out
    assert "est_ec" in out and "366.0" in out  # 66 + 3*100

    args2 = argparse.Namespace(via="screener", live=False, as_of=None,
                               symbols=None, max_ec=None, idx=None,
                               out="out/score", dry_run=True)
    cli.cmd_dump_cross_section(args2)
    out2 = capsys.readouterr().out
    assert str(int(config.EC_SCREENER_FULL_UNIVERSE + config.EC_STOCKS_LIST)) in out2


def test_dry_run_snapshot_requires_symbols():
    args = argparse.Namespace(via="snapshot", live=True, as_of=None,
                              symbols=None, max_ec=1e9, idx=None,
                              out="out/score", dry_run=True)
    with pytest.raises(SystemExit, match="--symbols"):
        cli.cmd_dump_cross_section(args)


# --- P1-2 deep history ---------------------------------------------------------
def test_years_to_n_maps_and_clamps():
    assert config.years_to_n(1) == 250
    assert config.years_to_n(2.5) == 625
    assert config.years_to_n(0.24) == 60
    assert config.years_to_n(40) == 10000, "clamped at the SDK served-bar cap"
    assert config.years_to_n(100) == 10000
    with pytest.raises(ValueError, match="positive"):
        config.years_to_n(0)


def test_fetch_history_panel_threads_max_limit(monkeypatch):
    import datetime as dt

    import pandas as pd

    captured: dict = {}

    class _K:
        def bars(self, symbol, n=60, adj=None):
            d = dt.date(2026, 11, 17)
            dates = [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
            scale = 3.0 if adj == "hfq" else 1.0
            return pd.DataFrame({
                "date": dates, "symbol": symbol,
                "open": [(10.0 + i) * scale for i in range(n)],
                "high": [(11.0 + i) * scale for i in range(n)],
                "low": [(9.0 + i) * scale for i in range(n)],
                "close": [(10.5 + i) * scale for i in range(n)],
                "volume": [100.0 + i for i in range(n)],
                "amount": [1050.0 + i for i in range(n)],
            })

    class _M:
        def bars(self, symbol, n=60):
            d = dt.date(2026, 11, 17)
            dates = [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
            return pd.DataFrame({"date": dates, "symbol": symbol, "net_mf": [1.0] * n,
                                 "elg_net": [1.0] * n, "lg_net": [1.0] * n,
                                 "md_net": [1.0] * n, "sm_net": [1.0] * n})

    class _V:
        def hist_stats(self, symbol, metric="pe", window="3y"):
            return {
                "ok": True, "as_of": "2026-11-17",
                "data": {"series": pd.DataFrame({
                    "date": ["2026-09-29", "2026-09-30"], "value": [20.0, 21.0],
                })},
                "meta": {},
            }

    class _C:
        def __init__(self):
            self.kline, self.moneyflow, self.valuation = _K(), _M(), _V()
            self.last_ec_cost = 1.0
            self.stocks = type("S", (), {"list": staticmethod(lambda: pd.DataFrame(
                {"symbol": ["600519", "000858"], "exchange": ["sh", "sz"]}))})()

    def fake_client(live, max_limit=None):
        captured["live"] = live
        captured["max_limit"] = max_limit
        return _C()

    monkeypatch.setattr(fetch, "_client", fake_client)
    r = fetch.fetch_history_panel(["600519", "000858"], live=True, n=700,
                                  max_limit=700, max_ec=10**9)
    assert captured["max_limit"] == 700, "deep history must raise the served-bar cap"
    assert r.metering.mode == "live"


def test_dry_run_years_flag_extrapolation_note(tmp_path, capsys):
    out = tmp_path / "out"
    args = argparse.Namespace(symbols=["600519"], live=True, n=120,
                              freq="day", out=str(out), store=None, workers=1,
                              max_ec=10**9, adjust=None, resume=False,
                              dry_run=True, years=3)
    cli.cmd_dump_history(args)
    captured = capsys.readouterr().out
    assert '"n": 750' in captured  # 3 x 250
    assert "EXTRAPOLATED" in captured, "deep history must flag unverified pricing"
    assert not (out / "_resume").exists()


def test_years_and_n_are_mutually_exclusive(tmp_path):
    args = argparse.Namespace(symbols=["600519"], live=False, n=60,
                              freq="day", out=str(tmp_path / "o"), store=None,
                              workers=1, max_ec=None, adjust=None, resume=False,
                              dry_run=True, years=3)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        cli.cmd_dump_history(args)
