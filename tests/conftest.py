"""Shared offline fixtures for the Tier-1 suite (zero network, zero EC).

0.7.0: the offline demo snapshot was retired with the SDK's ``demo_client``
(wmshare 3.0). Tests that exercised the demo path now run against
``OfflineHistoryClient`` - synthetic frames served through the real
SDK-shaped API surface. Tests that build CLI clients must monkeypatch
``fetch._client`` (or inject ``client=``) so no test ever reaches the
network: since the public-pool free trial exists, a missing-credentials
client CONSTRUCTS fine and would silently bill shared quota.
"""

from __future__ import annotations

import pandas as pd
import pytest

SYMBOLS = ["600519", "000858", "601318"]
DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
CLOSES = [1400.0, 1410.0, 1425.0, 1440.0, 1450.0]
PE = [21.5, 21.7, 21.9, 22.1, 22.3]


class OfflineHistoryClient:
    """SDK-shaped history client: kline/valuation/moneyflow/stocks only."""

    last_ec_cost = None  # no meter -> result ec_cost stays None

    class _Kline:
        def __init__(self, adj_scale: float):
            self._adj_scale = adj_scale

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
                    ["open", "high", "low", "close"]] * self._adj_scale
            elif adj == "qfq":
                df[["open", "high", "low", "close"]] = df[
                    ["open", "high", "low", "close"]] * 0.5
            return df.tail(min(n, len(DATES))).reset_index(drop=True)

    class _Valuation:
        def hist_stats(self, symbol, metric="pe", window="3y"):
            return {
                "ok": True,
                "as_of": DATES[-1],
                "data": {"series": pd.DataFrame({"date": list(DATES), "value": list(PE)})},
                "meta": {"window": window},
            }

    class _Moneyflow:
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

    class _Stocks:
        def list(self):
            return pd.DataFrame({"symbol": SYMBOLS, "exchange": ["sh", "sz", "sh"]})

    class _Margin:
        """margin.latest / margin.query over the 2.0 P1 margin face.

        ``empty_symbols`` simulates tickets outside the margin universe
        (latest -> empty frame)."""

        def __init__(self, empty_symbols: tuple[str, ...] = ()):
            self.empty_symbols = empty_symbols
            self.last_limit = None

        def _frame(self, symbol: str, n: int) -> pd.DataFrame:
            dates = DATES[-n:]
            return pd.DataFrame({
                "trade_date": dates,
                "security_code": symbol,
                "secucode": f"{symbol}.SH",
                "security_name": f"name-{symbol}",
                "fin_buy_amt": [1_000_000.0] * len(dates),
                "fin_repay_amt": [900_000.0] * len(dates),
                "fin_balance": [50_000_000.0] * len(dates),
                "loan_sell_vol": [2_000.0] * len(dates),
                "loan_repay_vol": [1_500.0] * len(dates),
                "loan_balance": [300_000.0] * len(dates),
            })

        def latest(self, symbol):
            if symbol in self.empty_symbols:
                return pd.DataFrame()
            return self._frame(symbol, 1)

        def query(self, symbol, limit=20):
            self.last_limit = limit
            if symbol in self.empty_symbols:
                return pd.DataFrame()
            return self._frame(symbol, min(limit, len(DATES)))

    def __init__(self, adj_scale: float = 2.0, margin_empty: tuple[str, ...] = ()):
        self.kline = self._Kline(adj_scale)
        self.valuation = self._Valuation()
        self.moneyflow = self._Moneyflow()
        self.stocks = self._Stocks()
        self.margin = self._Margin(margin_empty)
        self.auth_mode = "credentials"


@pytest.fixture()
def offline_history_client():
    return OfflineHistoryClient()


@pytest.fixture()
def no_real_client(monkeypatch):
    """Tripwire: any test reaching ``fetch._client`` fails HERE instead of
    firing a real request (guards-before-network contract)."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "fetch._client was called in an offline test - a guard moved "
            "behind a client build, or a stub/monkeypatch is missing"
        )

    from wmshare_qlib import fetch

    monkeypatch.setattr(fetch, "_client", _boom)
