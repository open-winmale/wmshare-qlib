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

# Snapshot-batch score fields (P0-3): columns of the kline.snapshot batch
# (field names verified against the SDK's _SNAPSHOT_BATCH_FIELDS mapping).
# This is a TECHNICAL COMPOSITE (momentum / size tilt), a different score
# family from the screener fundamentals above - always labeled as such in
# the score meta, never mixed into one composite. Directions are a DECLARED
# research choice, not upstream truth.
# Cost anchor (probe 2026-11-17): ~100 EC/票 at N=3 (docstring: ~58/票 at
# large batches); 23 fields ≈ 2.5-4.3 EC/field - cheaper per column than the
# screener light columns (~9 EC/row/column) and the ONLY client-side-subset
# path (screener --idx is not served by the server).
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


# --- EC pricing anchors (deterministic model, live-probed 2026-11-17) --------
# Single source of truth for the --dry-run pre-flight estimator (estimate.py).
# Formulas fitted exactly on three probes (roadmap §2.5); they are ESTIMATES -
# server-side pricing can change, so --max-ec stays the binding pre-flight
# declaration. Re-probe quarterly (roadmap §5.3).
EC_KLINE_BASE = 156.0            # per call
EC_KLINE_PER_BAR = 67.0          # per SERVED bar (range queries serve max_limit!)
EC_MONEYFLOW_BASE = 156.0        # per call
EC_MONEYFLOW_PER_BAR = 16.0      # per bar
EC_VALUATION_PER_METRIC = 712.0  # hist_stats 3y window, per metric (pe/pb/ps/pr)
EC_SNAPSHOT_PER_TICKET = 100.0   # kline.snapshot batch, N=3 measured (~58 at scale)
EC_STOCKS_LIST = 66.0            # whole universe, one call
EC_SCREENER_FULL_UNIVERSE = 204430.0  # measured: full market x 4 light columns
# RANGE QUERIES still bill the whole anchor WINDOW, not the returned rows
# (re-probed 2026-11-18 on wmshare 2.2.0 anchor pagination): a 49-trading-day
# kline window returned 49 rows but paid 33,688 EC = 156 + 67 x 500 (the SDK
# slices at max_limit=500 and the server materializes the full window). The
# 2.2.0 pagination lifts the COMPLETENESS constraints (any span, no silent
# truncation), NOT the short-window cost. Never use begin/end for increments;
# the measured anchor below keeps that decision auditable.
EC_KLINE_RANGE_WINDOW = 500      # bars per anchor window
EC_KLINE_RANGE_CALL = 33688.0    # measured: 49-row window, 2026-11-18
# daily_basic (LIVE 2026-11-18): NOT cached — bills by rows scanned.
# Measured in production (winmale 20261118110034 release window):
#   filtered read (13 trading days x 2 fields)  = 2,038 EC
#   full-history read (726 docs x all 8 fields) = 18,317 EC
# metering.ec_cost stays the only real quote.
EC_DAILY_BASIC = 2038.0
# Incremental daily cadence (measured 2026-11-18, prod DAG summary): one
# symbol/day = HIST_STATS("6m") latest + ITER snapshot = 312 EC/symbol/day
# (sample group 300 symbols ~= 93.6k EC/day; full market 5.7k ~= 1.78M EC/day
# - re-budget before expanding beyond the sample group).
EC_DAILY_BASIC_INCREMENTAL = 312.0
# Read floor (probed 2026-11-18): a single-day, single-field read still pays
# 1,707 EC; 1 day x 8 fields = 1,851 (fields are nearly free, the scan is
# not). Model for the --dry-run quote: floor + ~23 EC/doc, fitted between
# 1 day (1,707) and 726 docs x 8 fields (18,317). NOT a cache hit.
EC_DAILY_BASIC_FLOOR = 1707.0
EC_DAILY_BASIC_PER_DOC = 23.0
# margin pricing (probed 2026-11-18; the SDK has NO model for these):
#   margin.query(limit=20) = 2,472 EC (20 rows, ~124 EC/row)
#   margin.latest         = 182 EC (one row)
# Roadmap P2-2 (两融进 .bin) is sized from these anchors.
EC_MARGIN_PER_ROW = 124.0
EC_MARGIN_LATEST = 182.0

# Operational cap measured live (2026-11-17): a single kline.snapshot batch
# over ~81 tickets failed (server-side eval/timeout), while <=30-ticket
# chunks succeed deterministically (~66 EC/票 at that scale). Snapshot-batch
# callers chunk client-side; EC is the sum of chunk bills.
SNAPSHOT_BATCH_CHUNK = 30

# Deep history (P1-2): n-bars path only (range queries force-pay max_limit
# bars - falsified probe). The SDK caps one call at 10000 served bars, which
# covers A-share history back to 1991 (~8500 trading days). Pricing beyond
# 500 served bars is EXTRAPOLATED (linear) - probe pending (roadmap §5).
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

    Historically v1 used qfq everywhere (factor=1). Live now defaults to
    'hfq_factor' (see adjustment_mode); only the demo snapshot stays qfq_const.
    """
    return "qfq_const"


# --- price adjustment conventions --------------------------------------------
# Two supported conventions (single source of truth; fetch/panel/cli read this):
#
# 'hfq_factor' (LIVE default):
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
#   Live kline cost doubles (one adj call + one raw call, same EC formula).
#
# 'qfq_const' (DEMO, forced):
#   the demo snapshot supports neither adj= nor date ranges (UsageError,
#   probe 2026-11-17); prices are qfq with factor=1.0 emitted explicitly.
#   Caveat stays documented: qfq re-anchors at every corporate action, so
#   demo fragments fetched across an ex-div event are mutually inconsistent.
ADJUSTMENT_MODES: tuple[str, ...] = ("hfq_factor", "qfq_const")


def adjustment_mode(live: bool, requested: str | None = None) -> str:
    """Resolve the effective adjustment mode (single resolution point).

    ``requested=None`` -> auto: 'hfq_factor' live, 'qfq_const' demo.
    An explicit request is validated; demo CANNOT use 'hfq_factor' (upstream
    UsageError) - raise loud instead of silently degrading.
    """
    auto = "hfq_factor" if live else "qfq_const"
    mode = requested or auto
    if mode not in ADJUSTMENT_MODES:
        raise ValueError(
            f"unknown adjustment mode {mode!r}; expected one of {list(ADJUSTMENT_MODES)}"
        )
    if not live and mode != "qfq_const":
        raise ValueError(
            "demo snapshot supports only 'qfq_const' (no adj= support upstream); "
            "hfq_factor requires --live"
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
