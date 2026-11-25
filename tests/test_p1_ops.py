"""P1-3 ops tests: transient retry/backoff, --dry-run scope previews.

Retry tests fake ``wmshare.errors`` ON THE PACKAGE (``from wmshare import
errors`` resolves via the parent module's attribute, not sys.modules) and
inject the clock (fetch._sleep), so the policy table is pinned exactly:
TransportError / ServerError / rate-limit-429 retry; QuotaError (a LimitError
subclass!), UsageError, ApiError never do. retry_after wins over backoff.
"""

from __future__ import annotations

import argparse
import json

import pytest
import wmshare

from wmshare_qlib import cli, config, fetch, panel


# --- fake SDK error module ----------------------------------------------------
class _WErr(RuntimeError):
    def __init__(self, msg, code="", status=None, retry_after=None):
        super().__init__(msg)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _fake_errors(monkeypatch):
    mod = type("errors", (), {})()
    for name in (
        "TransportError",
        "ServerError",
        "LimitError",
        "QuotaError",
        "AuthError",
        "UsageError",
        "ApiError",
    ):
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
        fn,
        label="x/kline",
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
        raise mod.QuotaError("EC_BALANCE_EXHAUSTED", code="EC_BALANCE_EXHAUSTED", status=429)

    with pytest.raises(mod.QuotaError):
        fetch.call_with_retry(fn, label="x", meter_cb=lambda: None, warn_cb=lambda w: None)
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
            fetch.call_with_retry(fn, label="x", meter_cb=lambda: None, warn_cb=lambda w: None)
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
            raise mod.LimitError("rate limited", code="rate_limited", status=429, retry_after=3.0)
        return "ok"

    warnings: list[str] = []
    out = fetch.call_with_retry(fn, label="x", meter_cb=lambda: None, warn_cb=warnings.append)
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
        fetch.call_with_retry(fn, label="x", meter_cb=lambda: None, warn_cb=lambda w: None)
    assert calls["n"] == fetch.RETRY_ATTEMPTS


# --- --dry-run scope previews -------------------------------------------------
def test_dry_run_dump_history_previews_without_store_or_request(tmp_path, capsys):
    out_dir = tmp_path / "out"
    args = argparse.Namespace(
        symbols=["600519", "000858"],
        n=60,
        freq="day",
        out=str(out_dir),
        store=None,
        workers=1,
        max_ec=1e9,
        adjust=None,
        resume=False,
        dry_run=True,
        daily_basic=False,
    )
    cli.cmd_dump_history(args)
    captured = capsys.readouterr().out
    assert "[dry-run]" in captured
    payload = json.loads(captured.split("[dry-run] ", 1)[1])
    assert payload["dry_run"] is True
    assert payload["symbols"] == 2 and payload["n"] == 60
    assert "est_ec" not in payload and "ceiling_warning" not in payload
    assert not (tmp_path / "out" / "_resume").exists(), "dry-run must not write a store"


def test_dry_run_cross_section_snapshot_and_screener(capsys):
    args = argparse.Namespace(
        via="snapshot",
        as_of=None,
        symbols=["a", "b", "c"],
        max_ec=1e9,
        idx=None,
        out="out/score",
        dry_run=True,
    )
    cli.cmd_dump_cross_section(args)
    out = capsys.readouterr().out
    payload = json.loads(out.split("[dry-run] ", 1)[1])
    assert payload["dry_run"] is True and payload["via"] == "snapshot"
    assert payload["symbols"] == 3
    assert "est_ec" not in payload and "ceiling_warning" not in payload

    args2 = argparse.Namespace(
        via="screener",
        as_of=None,
        symbols=None,
        max_ec=None,
        idx=None,
        out="out/score",
        dry_run=True,
    )
    cli.cmd_dump_cross_section(args2)
    out2 = capsys.readouterr().out
    assert "full universe" in out2


def test_dry_run_snapshot_requires_symbols():
    args = argparse.Namespace(
        via="snapshot",
        as_of=None,
        symbols=None,
        max_ec=1e9,
        idx=None,
        out="out/score",
        dry_run=True,
    )
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
            return pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": [(10.0 + i) * scale for i in range(n)],
                    "high": [(11.0 + i) * scale for i in range(n)],
                    "low": [(9.0 + i) * scale for i in range(n)],
                    "close": [(10.5 + i) * scale for i in range(n)],
                    "volume": [100.0 + i for i in range(n)],
                    "amount": [1050.0 + i for i in range(n)],
                }
            )

    class _M:
        def bars(self, symbol, n=60):
            d = dt.date(2026, 11, 17)
            dates = [(d - dt.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
            return pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "net_mf": [1.0] * n,
                    "elg_net": [1.0] * n,
                    "lg_net": [1.0] * n,
                    "md_net": [1.0] * n,
                    "sm_net": [1.0] * n,
                }
            )

    class _V:
        def hist_stats(self, symbol, metric="pe", window="3y"):
            return {
                "ok": True,
                "as_of": "2026-11-17",
                "data": {
                    "series": pd.DataFrame(
                        {
                            "date": ["2026-09-29", "2026-09-30"],
                            "value": [20.0, 21.0],
                        }
                    )
                },
                "meta": {},
            }

    class _C:
        def __init__(self):
            self.kline, self.moneyflow, self.valuation = _K(), _M(), _V()
            self.last_ec_cost = 1.0
            self.stocks = type(
                "S",
                (),
                {
                    "list": staticmethod(
                        lambda: pd.DataFrame(
                            {"symbol": ["600519", "000858"], "exchange": ["sh", "sz"]}
                        )
                    )
                },
            )()

    def fake_client(max_limit=None, **kw):
        captured["max_limit"] = max_limit
        return _C()

    monkeypatch.setattr(fetch, "_client", fake_client)
    r = fetch.fetch_history_panel(
        ["600519", "000858"], n=700, max_limit=700, max_ec=10**9
    )
    assert captured["max_limit"] == 700, "deep history must raise the served-bar cap"
    assert r.metering.mode == "live"


def test_dry_run_years_flag_scope_preview(tmp_path, capsys):
    out = tmp_path / "out"
    args = argparse.Namespace(
        symbols=["600519"],
        n=120,
        freq="day",
        out=str(out),
        store=None,
        workers=1,
        max_ec=10**9,
        adjust=None,
        resume=False,
        dry_run=True,
        years=3,
        daily_basic=False,
    )
    cli.cmd_dump_history(args)
    captured = capsys.readouterr().out
    assert '"n": 750' in captured  # 3 x 250
    payload = json.loads(captured.split("[dry-run] ", 1)[1])
    assert payload["n"] == 750
    assert not (out / "_resume").exists()


def test_years_and_n_are_mutually_exclusive(tmp_path):
    args = argparse.Namespace(
        symbols=["600519"],
        n=60,
        freq="day",
        out=str(tmp_path / "o"),
        store=None,
        workers=1,
        max_ec=None,
        adjust=None,
        resume=False,
        dry_run=True,
        years=3,
    )
    with pytest.raises(SystemExit, match="mutually exclusive"):
        cli.cmd_dump_history(args)


def test_dump_history_requires_max_ec(tmp_path):
    """README promises `--max-ec` is required on live paths. The guard
    sits AFTER the --dry-run branch (previews fire nothing) so --dry-run
    stays guard-less; the guard itself is unconditional."""
    args = argparse.Namespace(
        symbols=["600519"],
        n=60,
        freq="day",
        out=str(tmp_path / "o"),
        store=None,
        workers=1,
        max_ec=None,
        adjust=None,
        resume=False,
        dry_run=False,
    )
    with pytest.raises(SystemExit, match="max-ec"):
        cli.cmd_dump_history(args)


def test_bare_dump_history_defaults_to_starter_universe(tmp_path, monkeypatch):
    """Omitting --symbols must produce a deterministic 10-code starter panel.
    The client is stubbed offline: this test pins the DEFAULT SYMBOLS + dump
    plumbing, not the network (the guard passed before any fetch)."""
    out = tmp_path / "out"
    args = argparse.Namespace(
        symbols=None,
        n=30,
        freq="day",
        out=str(out),
        store=None,
        workers=1,
        max_ec=10_000,
        adjust=None,
        resume=False,
        dry_run=False,
        anonymous=False,
        daily_basic=False,
        years=None,
    )
    uni = panel.demo_universe()
    dates = ["2026-10-28", "2026-10-29", "2026-10-30"]

    import pandas as pd

    def fake_panel(symbols, **kw):
        assert symbols == uni, "default --symbols must be the starter universe"
        on_symbol = kw["on_symbol"]
        all_rows = []
        for c in symbols:
            inst = (f"SH{c}" if c.startswith(("60", "68"))
                    else f"SZ{c}" if c.startswith("00") else f"BJ{c}")
            sym_rows = []
            for i, d in enumerate(dates):
                close = 1400.0 + i * 10.0
                for f, v in (
                    ("open", close - 5), ("high", close + 5), ("low", close - 10),
                    ("close", close), ("volume", 100_000.0), ("amount", close * 100_000.0),
                    ("factor", 2.0), ("pe", 22.0),
                ):
                    sym_rows.append({"date": d, "symbol": c, "instrument": inst,
                                     "field": f, "value": v})
            sym_df = pd.DataFrame(sym_rows)
            on_symbol(c, inst, [sym_df], 100.0)
            all_rows.extend(sym_rows)
        return fetch.FetchResult(
            frame=pd.DataFrame(all_rows),
            metering=fetch.Metering(mode="live"),
            warnings=[],
        )

    monkeypatch.setattr(cli.fetch, "fetch_history_panel", fake_panel)
    cli.cmd_dump_history(args)
    all_txt = (out / "instruments" / "all.txt").read_text(encoding="utf-8")
    assert len(all_txt.strip().splitlines()) == 10, "the starter universe is 10 codes"
    assert (out / "fetch_meta.json").exists()


def test_starter_universe_well_formed():
    """The default --symbols set stays a small, well-formed starter universe
    (membership in stocks.list is a live-run concern since the demo snapshot
    was retired; this pins the offline contract instead)."""
    uni = panel.demo_universe()
    assert len(uni) == 10
    assert len(set(uni)) == 10, "duplicate codes in the starter universe"
    assert all(len(c) == 6 and c.isdigit() for c in uni)


# --- 0.4.0 public-surface hardening ------------------------------------------
def test_fatal_errors_are_not_downgraded(monkeypatch):
    """Auth/quota must abort the symbol, not become a silent missing field.

    A swallowed AuthError upstream of the panel merge yields a thinner panel
    with exit code 0 - the caller cannot tell it apart from a legitimately
    shorter history.
    """
    _fake_errors(monkeypatch)
    assert fetch.is_fatal(wmshare.errors.AuthError("no creds"))
    assert fetch.is_fatal(wmshare.errors.QuotaError("empty"))
    assert not fetch.is_fatal(wmshare.errors.UsageError("bad param"))

    hint = fetch._credential_hint(wmshare.errors.AuthError("no creds"))
    assert "WMSHARE_CLIENT_ID" in hint
    assert "open.winmale.com" in hint

    qhint = fetch._credential_hint(wmshare.errors.QuotaError("empty"))
    assert "quota" in qhint.lower()


def test_public_all_lists_full_surface():
    import wmshare_qlib

    declared = set(wmshare_qlib.__all__)
    for name in ("config", "normalize", "fetch", "panel", "cross_section", "dump",
                 "format", "qlib_io", "resume", "reconcile"):
        assert name in declared, f"{name} missing from __all__"
        assert hasattr(wmshare_qlib, name), f"{name} declared but not importable"


def test_metering_as_meta_carries_no_guard_state():
    """as_meta() feeds fetch_meta.json and the [metering] print: it must not
    leak the internal ec_cost accumulator (the --max-ec executor's state)."""
    m = fetch.Metering(mode="live", requests=3, ec_cost=42.0, auth_mode="credentials")
    meta = m.as_meta()
    assert "ec_cost" not in meta
    assert meta["requests"] == 3 and meta["auth_mode"] == "credentials"
    assert m.ec_cost == 42.0, "internal guard state stays untouched"
