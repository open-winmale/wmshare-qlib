"""M3-lite tests: concurrent==serial, resume skips done, hard budget ceiling.

All tests are ZERO real EC: they use the demo snapshot or a tiny stub client that
mimics ``last_ec_cost``. Nothing here touches credentials or bills the account.

Stub discipline (verification-system hardening): every stub method MUST be
param-sensitive - it must consume the arguments it is given (e.g. ``n`` rows)
instead of returning a fixed fixture. A fixed-size stub is blind to parameter
pollution: the P0-1 bug (concurrent ``n`` shadowing) slipped through exactly
because the old stub always returned 3 rows regardless of ``n``.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from wmshare_qlib import config, fetch, resume

SYMS = ["600519", "000858", "601318"]
KEY = ["date", "instrument", "field", "value"]


def _asc_dates(rows: int, *, end: str = "2026-10-30") -> list[str]:
    """`rows` trading-like dates, OLDEST first (2.x asc) - stub kline/mf shape."""
    d = dt.date.fromisoformat(end)
    return [(d - dt.timedelta(days=rows - 1 - i)).isoformat() for i in range(rows)]


def _canonical(frame: pd.DataFrame) -> pd.DataFrame:
    """Order-independent view for value comparison across serial/concurrent."""
    out = frame[KEY].copy()
    out["value"] = out["value"].astype(float)
    return out.sort_values(KEY).reset_index(drop=True)


# --- gate 1: per-worker fresh clients keep concurrency deterministic ---------
def test_concurrent_workers_deterministic_vs_serial(monkeypatch):
    """Each worker owns a FRESH client (monkeypatched here), so concurrency
    must return EXACTLY the serial result - the regression guard for the
    60-rows->3 corruption class. The caller-client clamp warning is covered
    separately; this pins the supported concurrent path."""
    serial = fetch.fetch_history_panel(SYMS, n=60, max_workers=1,
                                       client=_StubClient(ec_per_call=1.0))
    monkeypatch.setattr(fetch, "_client", lambda **kw: _StubClient(ec_per_call=1.0))
    forced = fetch.fetch_history_panel(SYMS, n=60, max_workers=3)
    a, b = _canonical(serial.frame), _canonical(forced.frame)
    assert len(a) == len(b), f"row count diverges: {len(a)} vs {len(b)}"
    pd.testing.assert_frame_equal(a, b, check_dtype=False)
    serial_close = int((a["field"] == "close").sum())
    close_rows = int((b["field"] == "close").sum())
    assert close_rows == serial_close, (
        f"kline corrupted under workers=3: {close_rows} != serial {serial_close}"
    )
    assert serial.metering.requests == forced.metering.requests


def test_caller_client_clamps_concurrency_with_warning():
    """P1-2 regression: a caller-supplied client is shared and of unknown
    thread-safety. Instead of silently ignoring it (the old bug), we clamp to
    serial and SAY SO. The client's data must still be used."""
    client = _StubClient(ec_per_call=1.0)
    r = fetch.fetch_history_panel(SYMS, n=7, max_workers=3, client=client)
    assert any("caller-supplied client" in w for w in r.warnings), r.warnings
    closes = r.frame[r.frame["field"] == "close"]
    per_symbol = closes.groupby("instrument").size()
    assert (per_symbol == 7).all(), f"n polluted with caller client: {dict(per_symbol)}"


# --- gate 2: every symbol is fetched exactly once even under concurrency -----
def test_on_symbol_called_once_per_symbol():
    seen: list[str] = []
    fetch.fetch_history_panel(
        SYMS, n=60, max_workers=3, client=_StubClient(ec_per_call=1.0),
        on_symbol=lambda sym, inst, frames, ec: seen.append(sym),
    )
    assert sorted(seen) == sorted(SYMS), f"callback fired for {seen}"


# --- gate 3: resume skips cached symbols, merges full panel ------------------
def test_resume_skips_done_and_merges(tmp_path):
    store = tmp_path / "resume"
    params = {"n": 60, "fields": list(config.HISTORY_FIELDS), "live": False, "freq": "day"}
    manifest = resume.init_manifest(store, params=params, resume=False)

    seen: list[str] = []

    def _save(sym, inst, frames, ec):
        nonlocal manifest
        seen.append(sym)
        manifest = resume.record_fragment(store, manifest, sym, inst, frames)

    # First pass: only 2 symbols.
    fetch.fetch_history_panel(
        SYMS[:2], n=60, max_workers=1, client=_StubClient(ec_per_call=1.0), on_symbol=_save
    )
    assert resume.done_symbols(manifest) == set(SYMS[:2])

    # Second pass: resume asks for all 3 -> only the missing one is fetched.
    todo = resume.pending_symbols(SYMS, manifest)
    assert todo == [SYMS[2]], f"pending mismatch: {todo}"
    seen.clear()
    fetch.fetch_history_panel(
        todo, n=60, max_workers=1, client=_StubClient(ec_per_call=1.0), on_symbol=_save
    )
    assert seen == [SYMS[2]], f"re-fetched cached symbols: {seen}"

    merged = resume.load_panel(store, manifest)
    assert set(merged["instrument"].unique()) == {"SH600519", "SZ000858", "SH601318"}
    assert resume.summary(manifest)["n_cached"] == 3


# --- gate 4: param mismatch on resume fails loud (never silently mix panels) -
def test_resume_param_guard(tmp_path):
    store = tmp_path / "resume"
    resume.init_manifest(
        store, params={"n": 60, "live": False, "freq": "day"}, resume=False
    )
    with pytest.raises(ValueError, match="resume param mismatch"):
        resume.init_manifest(
            store, params={"n": 120, "live": False, "freq": "day"}, resume=True
        )


def test_resume_new_param_key_refused_when_fragments_exist(tmp_path):
    """P0-2' guard: a store whose manifest predates the 'adjust' key holds
    fragments of unknowable convention (pre-hfq_factor stores are qfq_const;
    new runs are hfq_factor). Mixing them would place two different price
    bases in one dump - the manifest guard must refuse BEFORE any fetch.
    An empty store (no fragments) may adopt the new key freely.
    """
    store = tmp_path / "resume"
    m = resume.init_manifest(store, params={"n": 60, "live": False}, resume=False)
    frag = pd.DataFrame({
        "date": _asc_dates(2), "symbol": "600519", "instrument": "SH600519",
        "field": "close", "value": [10.5, 10.6],
    })
    resume.record_fragment(store, m, "600519", "SH600519", [frag])
    with pytest.raises(ValueError, match="predates param 'adjust'"):
        resume.init_manifest(
            store, params={"n": 60, "live": False, "adjust": "hfq_factor"},
            resume=True,
        )
    # empty store adopts the key freely (nothing to mix)
    empty = tmp_path / "empty"
    resume.init_manifest(empty, params={"n": 60}, resume=False)
    resume.init_manifest(empty, params={"n": 60, "adjust": "hfq_factor"}, resume=True)


# --- gate 5: hard budget ceiling stops before submitting remaining symbols ---
class _StubGroup:
    """A sub-client whose methods set the parent's last_ec_cost on each call."""

    def __init__(self, parent, ec_per_call):
        self._parent = parent
        self._ec = ec_per_call

    def _bill(self):
        # Real wmshare reports the LAST call's cost (assignment, not cumulative).
        self._parent.last_ec_cost = self._ec


class _StubKline(_StubGroup):
    def bars(self, symbol, n=60, adj=None):
        self._bill()
        # PARAM-SENSITIVE: exactly n rows, OLDEST first (2.x asc) - normalize
        # asserts this direction. A fixed-size fixture would hide the
        # P0-1 concurrency n-shadowing bug.
        rows = max(1, int(n or 3))
        dates = _asc_dates(rows)
        # PARAM-SENSITIVE PER adj TOO (P0-2' discipline): hfq prices are a
        # strict 3x of raw, so factor=1.0 can never happen by accident - an
        # adj-blind stub (identical values both pulls) would silently emit
        # factor=1.0 and hide a lost dual-pull. volume is NOT scaled (upstream
        # volume is unadjusted, probe 2026-11-17); the adapter divides by
        # factor per the Qlib convention.
        scale = 3.0 if adj == "hfq" else 1.0
        return pd.DataFrame({
            "date": dates,
            "open": [(10.0 + i) * scale for i in range(rows)],
            "high": [(11.0 + i) * scale for i in range(rows)],
            "low": [(9.0 + i) * scale for i in range(rows)],
            "close": [(10.5 + i) * scale for i in range(rows)],
            "volume": [100.0 + i for i in range(rows)],
            "amount": [1050.0 + i for i in range(rows)],
        })


class _StubValuation(_StubGroup):
    def hist_stats(self, symbol, metric="pe", window="3y"):
        self._bill()
        # 2.x envelope: {ok, as_of, data:{series: DF[date, value]}}, asc.
        return {
            "ok": True,
            "as_of": "2026-10-30",
            "data": {"series": pd.DataFrame({
                "date": _asc_dates(2, end="2026-09-30"),
                "value": [20.0, 21.0],
            })},
            "meta": {},
        }


class _StubMoneyflow(_StubGroup):
    def bars(self, symbol, n=60):
        self._bill()
        # PARAM-SENSITIVE: n rows (P0-1 discipline, see module docstring).
        rows = max(1, int(n or 2))
        dates = _asc_dates(rows)
        return pd.DataFrame({
            "date": dates,
            "net_mf": [100.0 - i for i in range(rows)],
            "elg_net": [60.0 - i for i in range(rows)],
            "lg_net": [40.0 - i for i in range(rows)],
            "md_net": [10.0 - i for i in range(rows)],
            "sm_net": [-10.0 + i for i in range(rows)],
        })


class _StubStocks:
    def list(self):
        return pd.DataFrame({
            "symbol": SYMS,
            "exchange": ["sh", "sz", "sh"],
        })


class _StubClient:
    """Fully standalone wmshare-shaped client. No demo singleton mutation."""

    def __init__(self, ec_per_call: float):
        self.last_ec_cost = 0.0
        self.stocks = _StubStocks()
        self.kline = _StubKline(self, ec_per_call)
        self.valuation = _StubValuation(self, ec_per_call)
        self.moneyflow = _StubMoneyflow(self, ec_per_call)


def test_ec_hard_ceiling_stops_early():
    # Each symbol = 1 kline + 4 valuation + 1 moneyflow = 6 calls x 10 EC = 60.
    # Ceiling=25 -> 1st symbol alone (60) exceeds it; 2nd/3rd NOT submitted.
    client = _StubClient(ec_per_call=10.0)
    r = fetch.fetch_history_panel(
        SYMS, n=60, max_workers=1, client=client, max_ec=25.0
    )
    instruments = set(r.frame["instrument"].unique())
    assert instruments == {"SH600519"}, f"EC ceiling did not stop: {instruments}"
    assert r.metering.ec_cost is not None and r.metering.ec_cost >= 25.0
    assert any("budget ceiling" in w for w in r.warnings), r.warnings


def test_ec_metering_accumulates_in_serial_mode():
    client = _StubClient(ec_per_call=10.0)
    r = fetch.fetch_history_panel(SYMS[:2], n=60, max_workers=1, client=client)
    # 2 symbols x 7 billed calls (2 kline adj+raw + 4 valuation + 1 moneyflow)
    # x 10 EC = 140
    assert r.metering.ec_cost == pytest.approx(140.0)
    assert r.metering.requests == 14


# --- P1 (2026-11-24): --anonymous must reach EVERY worker client -------------
def test_bulk_worker_clients_honor_anonymous(monkeypatch):
    """The pool path used to call ``_client()`` bare: with credentials present
    it billed the private balance while metering (built from the anonymous main
    client) reported ``public_pool`` -- a metering lie on a real bill."""
    calls: list[dict] = []

    def fake_client(**kw):
        calls.append(dict(kw))
        c = _StubClient(ec_per_call=1.0)
        c.auth_mode = "public_pool" if kw.get("anonymous") else "credentials"
        return c

    monkeypatch.setattr(fetch, "_client", fake_client)
    r = fetch.fetch_history_panel(SYMS, n=5, anonymous=True, max_workers=3)
    assert len(calls) > 1, "no per-worker clients were built"
    assert all(kw.get("anonymous") is True for kw in calls), calls
    assert r.metering.auth_mode == "public_pool"


def test_tail_worker_clients_honor_anonymous(monkeypatch):
    calls: list[dict] = []

    def fake_client(**kw):
        calls.append(dict(kw))
        c = _StubClient(ec_per_call=1.0)
        c.auth_mode = "public_pool" if kw.get("anonymous") else "credentials"
        return c

    monkeypatch.setattr(fetch, "_client", fake_client)
    syms = SYMS[:2]
    r = fetch.fetch_history_tail(
        syms,
        last_dates={s: "2026-10-28" for s in syms},
        today="2026-10-30",
        anonymous=True,
        max_workers=2,
        fields=("close",),
    )
    assert len(calls) > 1, "no per-worker clients were built"
    assert all(kw.get("anonymous") is True for kw in calls), calls
    assert r.metering.auth_mode == "public_pool"


# --- data-completeness visibility (2026-11-24) --------------------------------
def test_bulk_warns_when_upstream_serves_fewer_bars():
    """The real store lost its 60th bar (n=60 -> 59, 2026-11-23) silently.

    Upstream short windows must never be filled, but they must be reported.
    """
    client = _StubClient(ec_per_call=1.0)
    orig = client.kline.bars
    client.kline.bars = lambda symbol, n=60, adj=None: orig(
        symbol, n=max(1, int(n) - 1), adj=adj
    )
    r = fetch.fetch_history_panel(SYMS[:1], n=10, client=client)
    assert any("served 9 of 10 requested bar(s)" in w for w in r.warnings), r.warnings


def test_tail_warns_when_upstream_serves_fewer_bars():
    client = _StubClient(ec_per_call=1.0)
    orig = client.kline.bars
    client.kline.bars = lambda symbol, n=60, adj=None: orig(
        symbol, n=max(1, int(n) - 1), adj=adj
    )
    r = fetch.fetch_history_tail(
        SYMS[:1],
        last_dates={SYMS[0]: "2026-10-28"},
        today="2026-10-30",
        fields=("close",),
        client=client,
    )
    assert any("kline-tail: upstream served" in w for w in r.warnings), r.warnings


# --- P0-1 regression: requested n must reach EVERY symbol under concurrency --
def test_live_concurrent_delivers_n_rows_per_symbol(monkeypatch):
    """The original P0-1 bug: the concurrent branch reassigned the loop
    variable ``n = min(batch_size, _affordable())``, shadowing the FUNCTION
    parameter ``n`` that the ``_run`` closure reads at call time. Every
    concurrent symbol then fetched only n=batch_size rows, while the 3y
    valuation series kept the total row count looking plausible.

    This test is the request-response reconciliation gate: the stub is
    param-sensitive (returns EXACTLY n rows), so a polluted n can no longer
    hide behind a fixed-size fixture.
    """
    monkeypatch.setattr(fetch, "_client", lambda **kw: _StubClient(ec_per_call=1.0))
    n = 7
    r = fetch.fetch_history_panel(SYMS, n=n, max_workers=2)
    closes = r.frame[r.frame["field"] == "close"]
    per_symbol = closes.groupby("instrument").size()
    assert (per_symbol == n).all(), f"n polluted under concurrency: {dict(per_symbol)}"
    assert int(per_symbol.sum()) == n * len(SYMS)
    # full request accounting (hfq_factor, P0-2'): 2 kline (adj + raw)
    # + 4 valuation + 1 moneyflow per symbol
    assert r.metering.requests == len(SYMS) * 7, r.metering.requests


def test_live_hfq_factor_emitted_and_volume_scaled(monkeypatch):
    """P0-2' gate: under hfq_factor the panel carries a REAL factor column
    (stub hfq = 3x raw -> factor exactly 3.0) and volume is divided by it
    (upstream volume is unadjusted; Qlib convention volume_adj = raw/factor).
    An adj-blind stub or a lost dual-pull would emit factor=1.0 - this test
    fails on that exact shape.
    """
    monkeypatch.setattr(fetch, "_client", lambda **kw: _StubClient(ec_per_call=1.0))
    n = 7
    r = fetch.fetch_history_panel(SYMS[:1], n=n)
    f = r.frame[r.frame["field"] == "factor"]
    assert len(f) == n, f"factor rows missing: {len(f)} != {n}"
    assert ((f["value"] - 3.0).abs() < 1e-9).all(), sorted(f["value"].unique())
    closes = r.frame[r.frame["field"] == "close"].sort_values("date")
    assert (closes["value"] > 30.0).all(), "close must be the hfq (3x) series"
    vols = r.frame[r.frame["field"] == "volume"].sort_values("date")
    # ascending dates: iloc[0] is the OLDEST bar (raw volume 100) / factor 3.0
    assert vols["value"].iloc[0] == pytest.approx(100.0 / 3.0)
    assert vols["value"].iloc[-1] == pytest.approx(106.0 / 3.0)
    qfq = fetch.fetch_history_panel(
        SYMS[:1], n=n, client=_StubClient(ec_per_call=1.0), adjust_mode="qfq_const"
    )
    f_qfq = qfq.frame[qfq.frame["field"] == "factor"]
    assert (f_qfq["value"] == 1.0).all()


def test_live_concurrent_max_ec_stops_before_submit(monkeypatch):
    """The hard budget ceiling must hold in the CONCURRENT branch too (probe + bound)."""
    monkeypatch.setattr(fetch, "_client", lambda **kw: _StubClient(ec_per_call=10.0))
    r = fetch.fetch_history_panel(SYMS, n=7, max_workers=2, max_ec=50.0)
    instruments = set(r.frame["instrument"].unique())
    assert len(instruments) == 1, f"ceiling did not stop concurrent submit: {instruments}"
    assert r.metering.ec_cost is not None and r.metering.ec_cost >= 50.0
    assert any("budget ceiling" in w for w in r.warnings), r.warnings


# --- manifest backward compatibility (pre-0.9 "ec_spent" key) -----------------
def test_manifest_with_legacy_ec_spent_loads_and_migrates(tmp_path):
    """Manifests written before the ec_spent removal load unchanged, and the
    next fragment record converges them onto the current shape (the key is
    dropped, never rewritten)."""
    import json

    store = tmp_path / "store"
    (store / "fragments").mkdir(parents=True)
    legacy = {
        "version": 1,
        "symbols": {
            "600519": {"instrument": "SH600519", "rows": 2, "fields": ["close"],
                       "bulk_date": "2026-01-05", "last_date": "2026-01-06"}
        },
        "ec_spent": 12345.0,
        "params": {"n": 60, "live": False, "freq": "day"},
    }
    (store / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    manifest = resume.load_manifest(store)
    assert manifest["ec_spent"] == 12345.0, "legacy key must not break loading"

    frag = pd.DataFrame({
        "date": _asc_dates(2), "symbol": "000858", "instrument": "SZ000858",
        "field": "close", "value": [10.5, 10.6],
    })
    updated = resume.record_fragment(store, manifest, "000858", "SZ000858", [frag])
    assert "ec_spent" not in updated
    on_disk = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    assert "ec_spent" not in on_disk
    assert "ec_spent" not in resume.summary(updated)
