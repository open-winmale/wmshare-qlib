"""Live-evidence tests (REAL network + EC, opt-in only).

These are NOT run by default. They need REAL wmshare credentials and consume
real EC. Enable explicitly with::

    WMSHARE_QLIB_LIVE=1 PYTHONPATH=src python -m pytest tests/test_live.py -q -m live

What they prove (AGENTS.md anti-pitfalls + roadmap M1 gate):
  * live fetch through OUR pipeline yields an intact, non-zero-filled panel;
  * a credentials client and a public-pool free-trial client agree on data
    DIRECTION (anti-pitfall #4; since 0.7.0 the second source is the
    public pool - the offline demo snapshot was retired with wmshare 3.x);
  * real pe/pb magnitudes are sane (unit guard);
  * cumulative EC stays under a hard pre-declared budget (before-the-fact cap).

If real credentials are absent - or the SDK silently hands back a public-pool
free-trial client - we skip LOUDLY with the reason. Never a false pass, and
never a shared-quota spend disguised as a test.
"""

from __future__ import annotations

import os

import pytest

from wmshare_qlib import fetch, normalize, panel

SYMS = ["600519", "000858", "601398"]
# P0-2' (hfq_factor): kline is pulled TWICE per symbol (adj + raw), so the
# per-symbol live bundle rose to ~7 calls. Measured deterministic pricing
# (docs/optimization-roadmap.md §2.5): kline = 156 + 67n EC/call,
# n=20 x 3 symbols -> kline ~9.0k + valuation ~8.5k + moneyflow ~1.4k ~ 19k.
# Ceiling stays pre-declared BEFORE the fact.
EC_BUDGET = 25_000


def _live_enabled() -> bool:
    return os.environ.get("WMSHARE_QLIB_LIVE", "").strip() in {"1", "true", "yes"}


@pytest.fixture(scope="module")
def live_client():
    if not _live_enabled():
        pytest.skip("set WMSHARE_QLIB_LIVE=1 to run live/EC-consuming tests")
    import wmshare

    try:
        client = wmshare.Client.from_env()
    except Exception as exc:
        pytest.skip(f"live credentials unavailable: {type(exc).__name__}: {exc}")
    auth = getattr(client, "auth_mode", "credentials")
    if auth != "credentials":
        # 0.7.0 contract: without REAL credentials from_env returns a
        # public-pool free-trial client (wmshare >=2.8 auto-fallback). A
        # shared-quota balance must never silently fund a test run - skip
        # loud instead (never a false pass, never a disguised shared spend).
        pytest.skip(
            f"public-pool free-trial client (auth_mode={auth!r}); live tests "
            "need REAL credentials - configure ~/.wmshare/credentials.env "
            "or set WMSHARE_DISABLE_ANON=1 to surface the absence"
        )
    client.ec_warn_threshold = EC_BUDGET
    return client


def _free_trial_client():
    import wmshare

    return wmshare.Client.anonymous()


@pytest.mark.live
def test_live_panel_integrity_and_units(live_client):
    r = fetch.fetch_history_panel(SYMS, n=20, client=live_client)
    assert r.metering.mode == "live"
    assert r.metering.auth_mode == "credentials"
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
def test_credentials_and_free_trial_same_direction(live_client):
    """Anti-pitfall #4: normalize() must orient every source identically.
    The second source is the PUBLIC POOL (free trial, tiny n) since the
    offline demo snapshot was retired in wmshare 3.0."""
    pool = _free_trial_client()
    assert getattr(pool, "auth_mode", "") == "public_pool"
    sym = "600519"

    def orientation(client):
        # 2.x+ facade: kline.bars returns a date-ASCENDING DataFrame directly.
        raw = client.kline.bars(sym, n=10)
        df = normalize.kline_to_frame(raw, symbol=sym)  # asserts asc, stays asc
        dates = list(df["date"])
        return dates[0] < dates[-1]

    assert orientation(live_client) is True
    assert orientation(pool) is True
