"""Valuation window plumbing: the bulk path uses config.VALUATION_WINDOW.

The engine has no year cap (10y verified live at 2,450 points); the configured
window is what both bulk and cadence-gated tail refreshes request.
"""

from __future__ import annotations

import pandas as pd

from wmshare_qlib import config, fetch


class _Val:
    def __init__(self) -> None:
        self.windows: list[str] = []

    def hist_stats(self, symbol, metric="pe", window="3y"):
        self.windows.append(window)
        return {
            "ok": True,
            "as_of": "2026-01-03",
            "data": {
                "series": pd.DataFrame(
                    {"date": ["2026-01-01", "2026-01-02"], "value": [1.0, 2.0]}
                )
            },
            "meta": {"metric": metric, "window": window},
        }


class _Client:
    def __init__(self) -> None:
        self.valuation = _Val()


def test_live_defaults_to_configured_window():
    assert config.VALUATION_WINDOW == "10y"
    c = _Client()
    frames, warnings, requests = fetch._valuation_frames(
        c, "600519", "SH600519", ("pe",), lambda: None
    )
    assert c.valuation.windows == ["10y"]
    assert len(frames) == 1 and not warnings and requests == 1


def test_demo_window_override_is_honoured():
    c = _Client()
    fetch._valuation_frames(c, "600519", "SH600519", ("pe",), lambda: None, window="3y")
    assert c.valuation.windows == ["3y"]
