"""History panel assembly helpers.

fetch.fetch_history_panel already returns a long panel. This module provides the
wide-table transform and sanity assertions required before dumping to Qlib .bin.
"""

from __future__ import annotations

import pandas as pd

from . import config


def assert_dates_aligned_across_fields(
    panel: pd.DataFrame,
    *,
    anchor: str = "close",
    fields: tuple[str, ...] = config.VALUATION_FIELDS,
    min_coverage: float = 0.9,
) -> dict:
    """Cross-field date alignment guard (P0-2 regression).

    For each instrument, every checked field must trade on the SAME trading
    dates as the anchor price field:
        coverage = |anchor_dates ∩ field_dates| / |anchor_dates|
    A systematic one-day offset (e.g. a timezone bug in epoch->date conversion)
    drives coverage to ~0 and fails LOUD here instead of silently misaligning
    $pe against $close inside Qlib. Valuation series may extend OLDER than the
    anchor window (valuation window vs n-day kline) - that only increases |field| and
    never lowers coverage, so the ratio is one-sided safe.

    Fields absent for an instrument are skipped (coverage is a data-availability
    concern handled by assert_field_declaration_roundtrip, not an alignment one).
    Fields whose dates ALL predate the anchor window are stale (upstream stopped
    publishing, e.g. BJ boards losing pe when financials lag) - recorded in
    ``stale``, NOT a failure: a systematic offset interleaves field dates with
    the anchor window, it cannot put every field date strictly before it.
    """
    issues: list[str] = []
    stale: list[str] = []
    coverage: dict[str, float] = {}
    if panel.empty:
        return {"ok": True, "issues": ["empty panel; skipped"], "coverage": coverage}
    present = set(panel["field"].unique())
    if anchor not in present:
        return {"ok": True, "issues": [f"anchor {anchor!r} absent; skipped"], "coverage": coverage}
    for inst, sub in panel.groupby("instrument"):
        anchor_dates = set(sub.loc[sub["field"] == anchor, "date"])
        if not anchor_dates:
            continue
        for f in fields:
            f_dates = set(sub.loc[sub["field"] == f, "date"])
            if not f_dates:
                continue
            inter = anchor_dates & f_dates
            cov = len(inter) / len(anchor_dates)
            coverage[f"{inst}/{f}"] = round(cov, 4)
            if cov < min_coverage:
                if pd.to_datetime(max(f_dates)) < pd.to_datetime(min(anchor_dates)):
                    stale.append(
                        f"{inst}/{f}: last {max(f_dates)} predates anchor window; "
                        f"stale (availability), not offset"
                    )
                    continue
                issues.append(
                    f"{inst}/{f}: date coverage {cov:.2%} < {min_coverage:.0%} "
                    f"({len(inter)}/{len(anchor_dates)} anchor dates aligned; "
                    f"suspect systematic offset)"
                )
    return {"ok": not issues, "issues": issues, "coverage": coverage, "stale": stale}


def assert_panel_integrity(panel: pd.DataFrame) -> dict:
    """Run the pre-dump assertions (the plan's DoD + P0-2/P1-3 hardening).

    Checks performed (each failure is collected, never silently passed):
    - every field in the panel has a unit declaration
    - dates are parseable
    - instruments match <SUFFIX><code> pattern
    - (date, instrument, field) is unique (upstream duplicates are deduped at
      the fetch layer; duplicates here mean a pipeline bug)
    - value magnitudes are within their unit's declared range (vectorized form
      of normalize.guard_unit; catches scale drift before it enters Qlib)
    - cross-field date alignment (see assert_dates_aligned_across_fields)
    - factor is positive and non-decreasing per instrument (P0-2': hfq_factor
      emits the real cumulative ratio; a decrease means mixed-convention
      fragments or upstream inconsistency - the manifest param guard is the
      first line of defense, this is the dump-time backstop)

    NULL values are counted and reported in ``null_values`` but do NOT fail
    ``ok``: missing data is legitimate (cold cache, suspension); wrong data is
    not. The failure criteria are the structural issues above.
    """
    issues: list[str] = []
    for field in panel["field"].unique():
        if field not in config.FIELD_UNITS:
            issues.append(f"field {field!r} has no unit declaration")
    dates = pd.to_datetime(panel["date"], errors="coerce")
    if dates.isna().any():
        issues.append(f"{int(dates.isna().sum())} unparseable dates")
    bad_inst = [i for i in panel["instrument"].unique()
                if not (str(i)[:2].upper() in {"SH", "SZ", "BJ", "HK"}
                        and str(i)[2:].isdigit())]
    if bad_inst:
        issues.append(f"malformed instruments: {bad_inst[:5]}")
    nas = int(panel["value"].isna().sum())
    # (date, instrument, field) uniqueness: fetch now dedups identical upstream
    # duplicates at the source, so ANY duplicate reaching here is a bug in the
    # pipeline and would fan out wide-table joins and vnpy left-joins.
    dups = int(panel.duplicated(["date", "instrument", "field"]).sum())
    if dups:
        issues.append(f"{dups} duplicate (date, instrument, field) rows in panel")
    # Unit magnitude guard (P1-3 wiring): vectorized normalize.guard_unit.
    for f, grp in panel.groupby("field"):
        unit = config.FIELD_UNITS.get(f)
        if unit is None:
            continue  # already reported above
        lo, hi = config.unit_value_range(unit)
        vals = pd.to_numeric(grp["value"], errors="coerce")
        bad_mask = (vals < lo) | (vals > hi)
        if bad_mask.any():
            first = grp.index[bad_mask][0]
            issues.append(
                f"field {f}: {int(bad_mask.sum())} value(s) outside "
                f"[{lo}, {hi}] for unit {unit} "
                f"(e.g. date={grp.loc[first, 'date']} value={grp.loc[first, 'value']})"
            )
    # Cross-field date alignment (P0-2 regression): valuation dates must match
    # the anchor price dates per instrument; a systematic offset fails loud.
    alignment = assert_dates_aligned_across_fields(panel)
    issues.extend(alignment["issues"])
    # Factor gate (P0-2'): per instrument the cumulative adjustment factor must
    # be positive and NON-DECREASING along dates. hfq_factor emits the real
    # hfq/raw ratio (jumps UP at ex-div dates only); qfq_const emits 1.0 and
    # passes trivially. A decrease means upstream inconsistency or - the
    # dangerous silent case - fragments fetched under MIXED conventions being
    # concatenated (manifest param guard is the first line; this is the backstop).
    factors = panel[panel["field"] == "factor"]
    if not factors.empty:
        for inst, sub in factors.groupby("instrument"):
            ordered = sub.sort_values("date")
            s = ordered["value"].astype(float).reset_index(drop=True)
            dates = ordered["date"].tolist()
            nonpos = int((s <= 0).sum())
            if nonpos:
                issues.append(f"{inst}: {nonpos} non-positive factor value(s)")
            bad = s.diff().dropna()
            bad = bad[bad < -1e-9]
            if not bad.empty:
                p = int(bad.index[0])  # positional; diff() drops row 0, so p >= 1
                issues.append(
                    f"{inst}: factor DECREASES at {dates[p]} "
                    f"({s.iloc[p - 1]:.6g} -> {s.iloc[p]:.6g}); "
                    f"hfq factor is cumulative and only jumps up"
                )
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "rows": int(len(panel)),
        "fields": sorted(panel["field"].unique().tolist()),
        "instruments": int(panel["instrument"].nunique()),
        "null_values": nas,
        "date_alignment": alignment["coverage"],
    }


def assert_field_declaration_roundtrip(
    emitted: set[str] | list[str], declared: set[str] | list[str]
) -> dict:
    """Two-way field coverage between what was emitted and what was declared.

    M2 DoD: zero phantom (emitted but undeclared) and zero missing column
    (declared but not emitted). ``emitted`` comes from the panel/dump manifest,
    ``declared`` from config.FIELD_UNITS. Catches silent field drift that a
    one-way check misses.
    """
    emitted, declared = set(emitted), set(declared)
    phantom = sorted(emitted - declared)   # emitted but no unit declaration
    missing = sorted(declared - emitted)   # declared but never emitted
    return {
        "ok": not phantom and not missing,
        "phantom": phantom,
        "missing": missing,
        "emitted": sorted(emitted),
        "declared": sorted(declared),
    }


def to_wide(panel: pd.DataFrame) -> pd.DataFrame:
    """Pivot long panel to [date, instrument, field1, field2, ...]."""
    wide = panel.pivot_table(
        index=["date", "instrument"], columns="field", values="value", aggfunc="last"
    ).reset_index()
    wide.columns.name = None
    return wide.sort_values(["instrument", "date"]).reset_index(drop=True)


def demo_universe() -> list[str]:
    """The 10 codes of the wmshare demo snapshot (the default `--symbols`).

    Mirrors ``wmshare._demo._UNIVERSE`` — every code here is guaranteed
    dump-able in demo mode, so a bare ``dump-history`` (no ``--symbols``)
    always works offline. (The previous list contained 002415, which the demo
    snapshot does not serve, and rejected the very run it was meant to serve.)
    """
    return [
        "600519",
        "000858",
        "601088",
        "600900",
        "601318",
        "000001",
        "600036",
        "000333",
        "300750",
        "688981",
    ]
