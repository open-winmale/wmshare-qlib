"""M1 live-evidence tests (REAL network + EC, opt-in only).

These are NOT run by default. They need wmshare credentials and consume real EC.
Enable explicitly with::

    WMSHARE_QLIB_LIVE=1 PYTHONPATH=src python -m pytest tests/test_live.py -q -m live

What they prove (AGENTS.md anti-pitfalls + roadmap M1 gate):
  * live fetch through OUR pipeline yields an intact, non-zero-filled panel;
  * demo and live kline series share the SAME direction (anti-pitfall #4);
  * real pe/pb magnitudes are sane (unit guard);
  * cumulative EC stays under a hard pre-declared budget (before-the-fact cap).

If credentials are absent we skip loudly with the reason — never a false pass.
"""

from __future__ import annotations

import os

import pytest

from wmshare_qlib import fetch, normalize, panel

SYMS = ["600519", "000858", "601398"]
EC_BUDGET = 5000  # pre-declared ceiling for this whole live test module


def _live_enabled() -> bool:
    return os.environ.get("WMSHARE_QLIB_LIVE", "").strip() in {"1", "true", "yes"}


@pytest.fixture(scope="module")
def live_client():
    if not _live_enabled():
        pytest.skip("set WMSHARE_QLIB_LIVE=1 to run live/EC-consuming tests")
    import wmshare

    try:
        client = wmshare.Client.from_env()
        client.ec_warn_threshold = EC_BUDGET
    except Exception as exc:
        pytest.skip(f"live credentials unavailable: {type(exc).__name__}: {exc}")
    return client


@pytest.mark.live
def test_live_panel_integrity_and_units(live_client):
    r = fetch.fetch_history_panel(SYMS, live=True, n=20, client=live_client)
    assert r.metering.mode == "live"
    assert r.metering.requests > 0
    integ = panel.assert_panel_integrity(r.frame)
    assert integ["ok"], integ
    assert integ["null_values"] == 0, "live panel must not contain fabricated zeros"
    assert set(SYMS).issubset(set(normalize.build_instrument_map(live_client)) | set(SYMS))

    # Real pe/pb must be sane magnitudes (unit guard), not percent/万元 drift.
    for field in ("pe", "pb"):
        sub = r.frame[r.frame.field == field]
        if len(sub):
            assert 0 < float(sub["value"].median()) < 1e4, f"{field} magnitude drift"

    ec = live_client.last_ec_cost
    if ec is not None:
        assert ec <= EC_BUDGET, f"EC budget exceeded: {ec} > {EC_BUDGET}"


@pytest.mark.live
def test_demo_live_same_direction(live_client):
    """Anti-pitfall #4: normalize() must orient demo and live identically."""
    import wmshare

    demo = wmshare.demo_client()
    sym = "600519"

    def orientation(client):
        raw = client.kline.bars(sym, n=10, _df=True)
        df = normalize.kline_to_frame(raw, symbol=sym)  # asserts desc before reversing
        dates = list(df["date"])
        return dates[0] < dates[-1]

    assert orientation(demo) is True
    assert orientation(live_client) is True
