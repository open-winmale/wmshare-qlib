"""0.7.0 public-pool free-trial integration tests (offline, zero EC).

wmshare 3.3.0 replaced the anonymous-credential claiming (2.6.0-2.8.0) with
the PUBLIC POOL: ``from_env()`` with no credentials returns a free-trial
client (``auth_mode == "public_pool"`` - shared quota, metered). The adapter
must (a) surface WHICH client paid in every metering record, (b) never retry
the pool's terminal refusals, (c) keep the live test suite on REAL
credentials only, and (d) still fail loud when the free trial is disabled.
"""

from __future__ import annotations

import argparse

import pytest

from wmshare_qlib import cli, fetch


class _PoolClient:
    """SDK-shaped public-pool client (auth_mode set, metered)."""

    auth_mode = "public_pool"
    last_ec_cost = 5.0


class _CredClient:
    auth_mode = "credentials"
    last_ec_cost = 5.0


# --- _client dispatch ---------------------------------------------------------
def test_client_dispatch_anonymous_vs_from_env(monkeypatch):
    """``anonymous=True`` must route to ``Client.anonymous`` (forced free
    trial, ignoring configured credentials); the default routes to
    ``from_env`` (credentials > free trial, SDK-resolved)."""
    import wmshare

    seen = {}

    class _FakeClientMgr:
        pass

    def fake_anonymous(**kw):
        seen["anonymous"] = kw
        return _PoolClient()

    def fake_from_env(**kw):
        seen["from_env"] = kw
        return _CredClient()

    monkeypatch.setattr(wmshare.Client, "anonymous", staticmethod(fake_anonymous))
    monkeypatch.setattr(
        wmshare.Client, "from_env", classmethod(lambda cls, **kw: fake_from_env(**kw))
    )

    c = fetch._client(anonymous=True, max_limit=999, timeout=42)
    assert isinstance(c, _PoolClient)
    assert seen["anonymous"].get("max_limit") == 999
    assert seen["anonymous"].get("timeout") == 42

    c = fetch._client()
    assert isinstance(c, _CredClient)
    assert "from_env" in seen


def test_client_auth_mode_probe_defaults_to_credentials():
    """Stubs without ``auth_mode`` (old clients, hand-rolled test doubles)
    are treated as credentialed - the probe never guesses."""
    assert fetch._auth_mode(_CredClient()) == "credentials"
    assert fetch._auth_mode(_PoolClient()) == "public_pool"
    assert fetch._auth_mode(object()) == "credentials"


# --- metering surfaces auth_mode ----------------------------------------------
def test_metering_carries_auth_mode(monkeypatch):
    """fetch_meta must record WHICH client paid - public-pool runs must be
    distinguishable from credentialed runs without reading warnings."""
    from conftest import OfflineHistoryClient

    r = fetch.fetch_history_panel(
        ["600519", "000858"], n=5, client=OfflineHistoryClient()
    )
    assert r.metering.mode == "live"
    assert r.metering.auth_mode == "credentials"  # stub has no auth_mode attr


def test_public_pool_metering_and_cli_hint(capsys, monkeypatch):
    """A public_pool client must land ``auth_mode="public_pool"`` in the
    metering dict AND trigger the one-line stderr hint."""
    from conftest import OfflineHistoryClient

    _SNAP_METRICS = ("chg_20d", "roi_1y", "ma60_up", "mv")

    class _Snap:
        results = {
            sym: {"is_st": False, "is_suspend": False,
                  **{m: 0.5 for m in _SNAP_METRICS}}
            for sym in ("600519", "000858", "601318")
        }
        errors = {}
        requests = 1

    stub = _PoolClient()
    stub.stocks = OfflineHistoryClient().stocks
    stub.kline = type("K", (), {"snapshot": staticmethod(
        lambda symbols, _fields=None: _Snap())})()
    r = fetch.fetch_cross_section_snapshot(
        ["600519", "000858", "601318"], client=stub, max_ec=10_000
    )
    assert r.metering.auth_mode == "public_pool"
    cli._free_trial_hint(r.metering.as_meta())
    err = capsys.readouterr().err
    assert "public-pool FREE TRIAL" in err


# --- public-pool terminal refusals are NEVER retried --------------------------
def test_ip_rate_limited_is_terminal(monkeypatch):
    """The pool's ip_rate_limited is terminal upstream (shared quota is not
    refilled by retrying): call_with_retry must raise after ONE attempt."""

    from wmshare import errors as _werr

    attempts = {"n": 0}
    sleeps: list[float] = []

    monkeypatch.setattr(fetch, "_sleep", lambda s: sleeps.append(s))

    def fire():
        attempts["n"] += 1
        raise _werr.LimitError(
            "请求被限流（HTTP 429，RATE_LIMITED）", code="ip_rate_limited", status=429
        )

    with pytest.raises(_werr.LimitError):
        fetch.call_with_retry(fire, label="probe", meter_cb=lambda: None,
                              warn_cb=lambda w: None)
    assert attempts["n"] == 1, f"terminal refusal retried {attempts['n']}x"
    assert sleeps == []


def test_regular_paid_rate_limit_still_retries(monkeypatch):
    """A PAID app's plain 429 rate limit keeps its bounded transient retry
    (retry_after honored) - only the public-pool refusal is terminal."""
    from wmshare import errors as _werr

    state = {"n": 0}

    monkeypatch.setattr(fetch, "_sleep", lambda s: None)

    def fire():
        state["n"] += 1
        if state["n"] == 1:
            raise _werr.LimitError("RATE_LIMITED", code="rate_limited", status=429)
        return "ok"

    out = fetch.call_with_retry(fire, label="probe", meter_cb=lambda: None,
                                warn_cb=lambda w: None)
    assert out == "ok" and state["n"] == 2


# --- DISABLE_ANON: fail loud returns ------------------------------------------
def test_credential_hint_mentions_free_trial(monkeypatch):
    """The AuthError hint must keep the registration pointers AND name the
    public-pool free trial + the DISABLE_ANON escape hatch."""
    from wmshare import errors as _werr

    hint = fetch._credential_hint(_werr.AuthError("rejected"))
    assert "WMSHARE_CLIENT_ID" in hint
    assert "open.winmale.com" in hint
    assert "FREE-TRIAL" in hint
    assert "WMSHARE_DISABLE_ANON=1" in hint


def test_credential_hint_quota_names_shared_pool():
    """Quota guidance must distinguish the shared public pool from a private
    balance (the 'wait for weekly refill' advice is wrong there)."""
    from wmshare import errors as _werr

    hint = fetch._credential_hint(_werr.QuotaError("exhausted"))
    assert "SHARED" in hint
    assert "open.winmale.com" in hint


# --- CLI: --anonymous plumbing -------------------------------------------------
def test_cli_anonymous_flag_reaches_fetch(monkeypatch, tmp_path):
    """`dump-history --anonymous` must thread the flag into the fetch layer."""
    captured: dict = {}

    def fake_panel(symbols, **kw):
        captured["anonymous"] = kw.get("anonymous")
        raise SystemExit("stop-before-network")  # guard past: capture only

    monkeypatch.setattr(cli.fetch, "fetch_history_panel", fake_panel)
    args = argparse.Namespace(
        symbols=["600519"], n=5, freq="day", out=str(tmp_path / "o"),
        store=None, workers=1, max_ec=10_000, adjust=None, resume=False,
        dry_run=False, anonymous=True, daily_basic=False, years=None,
    )
    with pytest.raises(SystemExit):
        cli.cmd_dump_history(args)
    assert captured["anonymous"] is True
