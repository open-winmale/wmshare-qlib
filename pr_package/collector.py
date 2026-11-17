"""Qlib data_collector skeleton for wmshare-sourced A-share data.

STATUS: reference skeleton / PR material. NOT wired into Qlib and NOT submitted.

This follows the REAL structure documented in microsoft/qlib's
``scripts/data_collector/README.md`` (verified 2026-11-17 via GitHub API):

  - a collector.py defining three classes:
      * <Name>Collector(BaseCollector)  -> get_data / collector_data
      * <Name>Normalize(BaseNormalize)  -> normalize(df)
      * Run(BaseRun)                    -> CLI entry
  - BaseCollector lives at ``data_collector.base``; the README prepends
    ``CUR_DIR.parent.parent`` to sys.path so the import resolves when the
    package sits at scripts/data_collector/<name>/.
  - Qlib requires Features[Price/Volume], Calendar, Instruments; if prices
    are NOT adjusted, ``factor=1`` (README's explicit rule). Our wmshare
    path emits pre-adjusted qfq prices with factor=1 (qfq_const), matching
    that rule exactly.

Why this is material and not a drop-in PR:
  - It depends on wmshare (an external SDK), so upstream must first decide
    whether a third-party commercial-data dependency is acceptable.
  - ``wmshare-qlib`` is not yet a public repo (GitHub 404 as of 2026-11-17);
    there is no reviewer-accessible source for a PR to point at.
  - Signing the Microsoft CLA is a legal act that only the account owner can do.

Do NOT paste this file into Qlib and call it "contributed". See README.md here.
"""

from __future__ import annotations

import sys
from pathlib import Path

CUR_DIR = Path(__file__).resolve().parent
# Real path rule from Qlib collector README (scripts/data_collector/<name>/).
sys.path.append(str(CUR_DIR.parent.parent))

from data_collector.base import BaseCollector, BaseNormalize, BaseRun  # noqa: E402


class WmshareCollector(BaseCollector):
    """Fetch raw (date, symbol, OHLCV, pe) from wmshare's history panel.

    In production this calls wmshare_qlib.fetch.fetch_history_panel(...).
    Kept dependency-light here so the skeleton imports without wmshare.
    """

    def get_data(self, symbol: str, start_datetime=None, end_datetime=None, **kwargs):
        from wmshare_qlib import fetch  # optional dependency, declared in PR

        result = fetch.fetch_history_panel(
            [symbol], n=self.max_workers, client=None  # real code passes n=days
        )
        return result.frame

    def collector_data(self):
        for symbol in self.instrument_list:
            df = self.get_data(symbol)
            if df is None or df.empty:
                continue
            self.save_instrument(symbol, df)


class WmshareNormalize(BaseNormalize):
    """Sort to Qlib's ascending-date convention; enforce factor=1 for qfq."""

    def __init__(self, date_field_name="date", symbol_field_name="symbol", **kwargs):
        super().__init__(
            date_field_name=date_field_name,
            symbol_field_name=symbol_field_name,
            **kwargs,
        )

    def normalize(self, df):
        df = df.sort_values([self.symbol_field_name, self.date_field_name])
        if "factor" in df.columns:
            # qfq_const convention: pre-adjusted prices, factor is always 1.
            df["factor"] = 1.0
        return df


class Run(BaseRun):
    """CLI entry point matching Qlib's collector invocation convention.

    Abstract methods verified from data_collector/base.py (2026-11-17):
    collector_class_name / normalize_class_name / default_base_dir.
    Real CLI entry is ``fire.Fire(Run)`` (from yahoo/collector.py).
    """

    def __init__(self):
        super().__init__(
            source_dir=None,
            normalize_dir=None,
            max_workers=4,
            interval="1d",
        )

    def collector_class_name(self):
        return WmshareCollector

    def normalize_class_name(self):
        return WmshareNormalize

    def default_base_dir(self):
        return CUR_DIR / "wmshare_data"


if __name__ == "__main__":
    import fire  # Qlib collector CLI dependency

    fire.Fire(Run)
