"""Field lists, metric-code selections, market/suffix mapping and unit declarations.

Single source of truth for what enters Qlib. Every field here must have a declared
unit and a history flag - this is what makes the adapter auditable.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Qlib required/optional base fields -------------------------------------
# Qlib standard data needs at least open/close/high/low/volume/factor.
# amount/vwap are optional. We emit amount because wmshare provides it.
BASE_FIELDS: tuple[str, ...] = (
    "open", "high", "low", "close", "volume", "factor", "amount",
)
VALUATION_FIELDS: tuple[str, ...] = ("pe", "pb", "ps", "pr")
MONEYFLOW_FIELDS: tuple[str, ...] = ("net_mf", "elg_net", "lg_net", "md_net", "sm_net")

# Fields that carry real history and may enter the Qlib .bin panel.
HISTORY_FIELDS: tuple[str, ...] = BASE_FIELDS + VALUATION_FIELDS + MONEYFLOW_FIELDS

# Unit declaration. Amount in the raw layer is CNY yuan; Qlib 'amount' expects
# turnover in yuan as well. Ratio/ratio-like fields are dimensionless.
FIELD_UNITS: dict[str, str] = {
    "open": "CNY/share", "high": "CNY/share", "low": "CNY/share",
    "close": "CNY/share", "volume": "shares", "amount": "CNY",
    "factor": "ratio", "pe": "ratio", "pb": "ratio", "ps": "ratio",
    "pr": "ratio", "net_mf": "CNY", "elg_net": "CNY", "lg_net": "CNY",
    "md_net": "CNY", "sm_net": "CNY",
}

# Direction of each upstream series (AGENTS.md anti-pitfall #3: points[0]).
# kline.bars -> newest first; valuation.hist_stats.series -> oldest first.
# We do NOT assume; normalize asserts the first/last date after sort.
SERIES_EXPECTED_ORDER: dict[str, str] = {
    "kline.bars": "desc",        # demo + live both newest -> oldest
    "valuation.hist_stats": "asc",  # [unix_ts, value] ascending
    "moneyflow.bars": "desc",
}

# Exchange suffix mapping (wmshare bare code -> Qlib instrument).
# Values verified from demo_client().stocks.list(): 'sh' / 'sz' / 'bj'.
EXCHANGE_SUFFIX: dict[str, str] = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


@dataclass(frozen=True)
class MetricCode:
    """A current-snapshot precomputed metric selected for the cross-section layer."""

    code: str
    meaning: str
    unit: str
    direction: str  # 'higher_better' | 'lower_better' | 'neutral' -> score sign


# Cross-section metrics: PER-STOCK domain, PRE-MATERIALIZED light columns
# only (verified offline via cat.metric; cost tiers measured live 2026-11-17).
#
# EC COST TIERS (measured, per single screener.query - materialization is
# paid once per request, never per row):
#   * light pre-materialized columns ($PE_TTM_LAST/$PB_TTM_LAST/$ROE_TTM_LAST/
#     $BONUS_RATE_3Y_AVG_LAST): 349 EC for the whole combo -> sustainable daily.
#   * on-the-fly computed columns ($PCT_*_10Y percentiles): ~110 EC PER SYMBOL
#     (621,399 EC for one full-universe query) - NEVER use for daily accumulation.
#   * industry-domain codes ($ID_*) on the stock index: NaN -> 0 rows, still billed.
CROSS_SECTION_METRICS: tuple[MetricCode, ...] = (
    MetricCode("$PE_TTM_LAST", "PE(TTM) latest", "ratio", "lower_better"),
    MetricCode("$PB_TTM_LAST", "PB(TTM) latest", "ratio", "lower_better"),
    MetricCode("$ROE_TTM_LAST", "ROE(TTM) latest", "ratio", "higher_better"),
    MetricCode("$BONUS_RATE_3Y_AVG_LAST", "Dividend yield 3y average", "ratio", "higher_better"),
)

# screener.query `where` MUST reference at least one metric code (server
# contract: "where 里没有 $指标码" rejects '1=1'). We anchor on per-stock
# PB > 0 (positive book value): the least biased filter - PE > 0 would
# silently drop loss-makers from the cross-section. $PB_TTM_LAST is the
# per-stock code; the $ID_ family would be NaN here (see above).
CROSS_SECTION_WHERE: str = "$PB_TTM_LAST > 0"


def unit_value_range(unit: str) -> tuple[float, float]:
    """Plausible magnitude range for a declared unit (single source of truth).

    Shared by normalize.guard_unit (per-value reference implementation) and
    panel.assert_panel_integrity (vectorized bulk check). A value outside its
    unit's range means scale drift (e.g. 万元 reported as 元) and must fail
    loud, never enter Qlib.
    """
    if unit == "pct01":
        # percentile codes ($PCT_*): decimal in [0, 1] by contract
        return (0.0, 1.0)
    if unit == "ratio":
        # pe/pb/ps/pr/factor: positive and bounded; 0 can be legitimate.
        return (0.0, 1e4)
    # CNY, CNY/share, shares: negative allowed for flows; absurd magnitude
    # (>1e15) means a unit/scale bug.
    return (-1e15, 1e15)


def qfq_factor_mode() -> str:
    """Which factor convention we use in v1.

    'qfq_const' = prices are already front-adjusted (qfq), so factor=1 for every
    date. This avoids the most common Qlib/broker mismatch (double adjustment).
    """
    return "qfq_const"


def history_days_threshold() -> int:
    """Minimum observations before a factor may enter backtest (cold-cache guard)."""
    return 60
