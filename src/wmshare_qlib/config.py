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

# --- daily_basic (upstream-gated, 2026-11-17) --------------------------------
# The server-side materialized valuation wide table (alt.valuation_daily;
# plan: docs/upstream-daily-basic-plan.md v3). Upstream names are the CONTRACT;
# units are 2.0 base units (rates decimal, money CNY), same as units.yaml.
DAILY_BASIC_FIELDS: tuple[str, ...] = (
    "pe_ttm", "pb", "ps", "pr", "total_mv", "circ_mv", "turnover_rate", "dv_ttm",
)
# The five fields that EXTEND the .bin panel (no collision with hist_stats'
# pe/pb/ps/pr). daily_basic's pb/ps/pr duplicate the hist_stats series by
# design (Gate A: same server core) - they are consumed by the reconcile
# gate only, never double-written into the panel.
PANEL_DAILY_BASIC_FIELDS: tuple[str, ...] = (
    "pe_ttm", "total_mv", "circ_mv", "turnover_rate", "dv_ttm",
)
# Fields that carry real history and may enter the Qlib .bin panel.
HISTORY_FIELDS: tuple[str, ...] = BASE_FIELDS + VALUATION_FIELDS + MONEYFLOW_FIELDS

# --- margin (两融, upstream 2.0 P1 face; probed 2026-11-24) --------------------
# Per-symbol daily margin series (margin.query / margin.latest). The six data
# columns are the CONTRACT; secucode/security_code/security_name/trade_date
# are key columns and never become panel fields. Units follow the 2.0 base
# convention (money CNY yuan, volume shares). Margin accumulation is Layer-3
# PIT: forward-only daily snapshots, never backfilled.
MARGIN_FIELDS: tuple[str, ...] = (
    "fin_buy_amt", "fin_repay_amt", "fin_balance",
    "loan_sell_vol", "loan_repay_vol", "loan_balance",
)

# Window for valuation.hist_stats series (pe/pb/ps/pr panel fields).
# The engine itself has NO year cap (kernel parseWindow accepts any Ny/Nq/Nm;
# live-probed 2026-11-20: 600519 pe 10y = 2,450 daily points from 2016-11-21).
# The old 3y ceiling was an SDK allowlist; wmshare 2.5.0 relaxed it (on PyPI
# 2026-11-20) and this library now requires that floor, so 10y is the default:
# the deeper series simply extend the panel backwards and the date-alignment
# guard is one-sided safe.
VALUATION_WINDOW: str = "10y"

# Unit declaration. Amount in the raw layer is CNY yuan; Qlib 'amount' expects
# turnover in yuan as well. Ratio/ratio-like fields are dimensionless.
FIELD_UNITS: dict[str, str] = {
    "open": "CNY/share", "high": "CNY/share", "low": "CNY/share",
    "close": "CNY/share", "volume": "shares", "amount": "CNY",
    # vwap is DERIVED at dump time (amount / volume, dump._append_vwap): the
    # upstream kline surface has no vwap field. Stored volume is already
    # divided by factor, so the quotient is consistent with adjusted prices.
    "vwap": "CNY/share",
    "factor": "ratio", "pe": "ratio", "pb": "ratio", "ps": "ratio",
    "pr": "ratio", "net_mf": "CNY", "elg_net": "CNY", "lg_net": "CNY",
    "md_net": "CNY", "sm_net": "CNY",
    # daily_basic (upstream-gated): 2.0 base units
    "pe_ttm": "ratio", "total_mv": "CNY", "circ_mv": "CNY",
    "turnover_rate": "pct01", "dv_ttm": "pct01",
    # margin (两融): 2.0 base units (money CNY yuan, volume shares)
    "fin_buy_amt": "CNY", "fin_repay_amt": "CNY", "fin_balance": "CNY",
    "loan_sell_vol": "shares", "loan_repay_vol": "shares", "loan_balance": "shares",
}

# Direction of each upstream series (AGENTS.md anti-pitfall #3: points[0]).
# Since wmshare 2.0 ALL facade series return ASCENDING (oldest -> newest,
# newest at iloc[-1]); we do NOT assume - normalize asserts first/last date.
SERIES_EXPECTED_ORDER: dict[str, str] = {
    "kline.bars": "asc",            # 2.x ascending contract
    "valuation.hist_stats": "asc",  # envelope data.series ascending
    "moneyflow.bars": "asc",        # 2.x ascending contract
}

# Exchange suffix mapping (wmshare bare code -> Qlib instrument).
# sh/sz/bj verified from demo_client().stocks.list(); 'hk' is the DECLARED
# adapter convention (kline docstring covers HK 5-digit codes) - mapping-ready
# but NOT end-to-end validated (no HK source verified in the demo snapshot;
# prices are HKD, see FIELD_UNITS caveat). Never infer suffixes from digit
# ranges - HK codes are 4/5 digits and collide with nothing here, but the
# mapping stays explicit.
EXCHANGE_SUFFIX: dict[str, str] = {"sh": "SH", "sz": "SZ", "bj": "BJ", "hk": "HK"}


@dataclass(frozen=True)
class MetricCode:
    """A current-snapshot precomputed metric selected for the cross-section layer."""

    code: str
    meaning: str
    unit: str
    direction: str  # 'higher_better' | 'lower_better' | 'neutral' -> score sign


# Cross-section metrics: PER-STOCK domain, PRE-MATERIALIZED light columns
# only (verified offline via cat.metric).
#
# Column selection rules (server contract, verified live):
#   * light pre-materialized columns ($PE_TTM_LAST/$PB_TTM_LAST/$ROE_TTM_LAST/
#     $BONUS_RATE_3Y_AVG_LAST): one flat-cost request for the whole combo ->
#     sustainable as a daily cadence.
#   * on-the-fly computed columns ($PCT_*_10Y percentiles): computed PER SYMBOL
#     server-side -> prohibitively heavy at full-universe scale, NEVER use for
#     daily accumulation.
#   * industry-domain codes ($ID_*) on the stock index: NaN -> 0 rows.
CROSS_SECTION_METRICS: tuple[MetricCode, ...] = (
    MetricCode("$PE_TTM_LAST", "PE(TTM) latest", "ratio", "lower_better"),
    MetricCode("$PB_TTM_LAST", "PB(TTM) latest", "ratio", "lower_better"),
    MetricCode("$ROE_TTM_LAST", "ROE(TTM) latest", "ratio", "higher_better"),
    MetricCode("$BONUS_RATE_3Y_AVG_LAST", "Dividend yield 3y average", "ratio", "higher_better"),
)

# Snapshot-batch score fields (P0-3): columns of the kline.snapshot batch
# (field names verified against the SDK's _SNAPSHOT_BATCH_FIELDS mapping).
# This is a TECHNICAL COMPOSITE (momentum / size tilt), a different score
# family from the screener fundamentals above - always labeled as such in
# the score meta, never mixed into one composite. Directions are a DECLARED
# research choice, not upstream truth.
# This is also the ONLY client-side-subset path (an explicit --symbols list;
# screener --idx scoped queries are not served by the server).
SNAPSHOT_METRICS: tuple[MetricCode, ...] = (
    MetricCode("chg_20d", "20-day price momentum (decimal)", "ratio", "higher_better"),
    MetricCode("roi_1y", "1-year return (decimal)", "ratio", "higher_better"),
    MetricCode("ma60_up", "close above MA60 (0/1 trend flag)", "flag", "higher_better"),
    MetricCode("mv", "total market value in CNY (small-cap tilt)", "CNY", "lower_better"),
)

# screener.query `where` MUST reference at least one metric code (server
# contract: "where 里没有 $指标码" rejects '1=1'). We anchor on per-stock
# PB > 0 (positive book value): the least biased filter - PE > 0 would
# silently drop loss-makers from the cross-section. $PB_TTM_LAST is the
# per-stock code; the $ID_ family would be NaN here (see above).
CROSS_SECTION_WHERE: str = "$PB_TTM_LAST > 0"


# Operational cap measured live (2026-11-17): a single kline.snapshot batch
# over ~81 tickets failed (server-side eval/timeout), while <=30-ticket
# chunks succeed deterministically. Snapshot-batch callers chunk client-side.
SNAPSHOT_BATCH_CHUNK = 30

# Deep history (P1-2): n-bars path only (range queries are bounded by the
# server's anchor window - a falsified probe; see docs/data-model.md). The
# SDK caps one call at 10000 served bars, which covers A-share history back
# to 1991 (~8500 trading days).
TRADING_DAYS_PER_YEAR = 250
KLINE_MAX_SERVED_BARS = 10000


def years_to_n(years: float) -> int:
    """Deep-history years -> bars per symbol (250 trading days/year)."""
    if years <= 0:
        raise ValueError(f"--years must be positive, got {years}")
    return min(int(round(years * TRADING_DAYS_PER_YEAR)), KLINE_MAX_SERVED_BARS)


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
    """DEPRECATED alias kept for one release; use adjustment_mode().

    Historically v1 used qfq everywhere (factor=1). The default has been
    'hfq_factor' since the P0-2' switch (see adjustment_mode).
    """
    return "qfq_const"


# --- price adjustment conventions --------------------------------------------
# Two supported conventions (single source of truth; fetch/panel/cli read this):
#
# 'hfq_factor' (default):
#   prices stored in open/high/low/close are BACK-adjusted (hfq) values served
#   directly by wmshare; factor = hfq/raw per date. hfq is anchored at the IPO,
#   so PAST values never change when a new corporate action occurs -> fragments
#   stay valid and daily bars append cleanly (incremental-safe).
#   Qlib convention verified against microsoft/qlib
#   scripts/data_collector/yahoo/collector.py (YahooNormalize1d.adjusted_price,
#   read 2026-11-17): factor = adjusted/raw, prices stored ARE adjusted,
#   raw = close / factor, volume_adj = raw / factor.
#   Probe 2026-11-17 (600519, n=10): raw vs hfq dates align 1:1,
#   factor=8.6463 monotonic; upstream volume is NOT adjusted -> we divide by
#   factor here to keep close*volume == amount invariant across ex-div dates.
#   hfq_factor issues two kline calls per window (one adj + one raw).
#
# 'qfq_const' (explicit only):
#   prices are qfq with factor=1.0 emitted explicitly. Caveat stays
#   documented: qfq re-anchors at every corporate action, so fragments
#   fetched across an ex-div event are mutually inconsistent. 0.7.0: the
#   offline demo snapshot was retired with wmshare 3.x, so nothing selects
#   this automatically anymore; the value remains valid so stores dumped
#   under 0.6.x still resume with a recognized params value.
ADJUSTMENT_MODES: tuple[str, ...] = ("hfq_factor", "qfq_const")


def adjustment_mode(requested: str | None = None) -> str:
    """Resolve the effective adjustment mode (single resolution point).

    ``requested=None`` -> auto: 'hfq_factor'. An explicit request is
    validated against ADJUSTMENT_MODES.
    """
    mode = requested or "hfq_factor"
    if mode not in ADJUSTMENT_MODES:
        raise ValueError(
            f"unknown adjustment mode {mode!r}; expected one of {list(ADJUSTMENT_MODES)}"
        )
    return mode


def history_days_threshold() -> int:
    """Accumulate's promotion guard default (single source of truth, P2-2 wiring).

    Cold-start guard only: the number of REAL accumulated snapshot days before
    ``accumulate`` auto-promotes the PIT series to a backtestable panel. The
    series itself is available from its first captured day - this threshold
    gates promotion (a sanity minimum against reading signal into a handful of
    days), never data access.
    """
    return 60
