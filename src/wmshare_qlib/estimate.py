"""Pre-flight EC estimator (P1-3 ``--dry-run``): pure functions, zero I/O.

Every number here is an ESTIMATE over the measured deterministic anchors
(``config.EC_*``, roadmap §2.5) - a quote is only ever the paid
``metering.ec_cost``. The estimator's job is to let the operator size the
universe and the ``--max-ec`` ceiling BEFORE firing, not to promise a bill.
"""

from __future__ import annotations

from . import config


def kline_call(n_bars: int) -> float:
    """One kline call: base + per-SERVED bar (deterministic, 3-point fit)."""
    return config.EC_KLINE_BASE + config.EC_KLINE_PER_BAR * max(0, int(n_bars))


def moneyflow_call(n_bars: int) -> float:
    return config.EC_MONEYFLOW_BASE + config.EC_MONEYFLOW_PER_BAR * max(0, int(n_bars))


def valuation_refresh(metrics: int = 4) -> float:
    """A full 3y valuation refresh (per-symbol; metrics x hist_stats)."""
    return config.EC_VALUATION_PER_METRIC * max(0, int(metrics))


def bulk_symbol(
    n: int,
    *,
    live: bool,
    adjust_mode: str,
    valuation_metrics: int,
    moneyflow: bool = True,
    daily_basic: bool = False,
) -> float:
    """One symbol in a bulk ``dump-history`` run.

    live hfq_factor: kline twice (adj + raw). live qfq_const: once.
    demo: one kline (n clamped to 120 by the fetch layer), pe-only valuation.
    ``daily_basic`` adds the materialized-table read (floor + per-doc, fitted
    on the 2026-11-18 probes; not a cache hit).
    """
    pulls = 1
    if live and adjust_mode == "hfq_factor":
        pulls = 2
    n_eff = min(n, 120) if not live else n
    est = pulls * kline_call(n_eff)
    if valuation_metrics:
        est += valuation_refresh(valuation_metrics)
    if moneyflow:
        est += moneyflow_call(min(n, 30) if not live else n)
    if daily_basic:
        est += daily_basic_read(n_eff)
    return est


def daily_basic_read(n_docs: int) -> float:
    """One ``valuation.daily_basic`` read (measured 2026-11-18, NOT cached).

    Fitted between the 1-day floor (1,707 EC) and the full-history read
    (18,317 EC / 726 docs): floor + ~23 EC/doc. The paid ``metering.ec_cost``
    stays the only real quote.
    """
    return config.EC_DAILY_BASIC_FLOOR + config.EC_DAILY_BASIC_PER_DOC * max(0, int(n_docs))


def margin_query(n_rows: int) -> float:
    """``margin.query`` (the SDK has no model; measured ~124 EC/row)."""
    return config.EC_MARGIN_PER_ROW * max(0, int(n_rows))


def tail_symbol(k: int, *, valuation_due: bool, valuation_metrics: int = 4,
                moneyflow: bool = True) -> float:
    """One symbol in an incremental ``update-history`` run (P0-1)."""
    est = 2 * kline_call(k)  # dual pull: adj + raw (always, see P0-1)
    if moneyflow:
        est += moneyflow_call(k)
    if valuation_due:
        est += valuation_refresh(valuation_metrics)
    return est


def snapshot_batch(n_symbols: int) -> float:
    """``--via snapshot`` cross-section: stocks.list + per-ticket batch."""
    return config.EC_STOCKS_LIST + config.EC_SNAPSHOT_PER_TICKET * max(0, int(n_symbols))


def screener_full(metrics: int = 4) -> float:
    """``--via screener`` full-market: measured anchor scaled by column count."""
    per_column = config.EC_SCREENER_FULL_UNIVERSE / 4.0
    return per_column * max(0, int(metrics)) + config.EC_STOCKS_LIST
