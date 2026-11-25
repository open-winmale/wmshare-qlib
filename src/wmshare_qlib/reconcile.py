"""Gate B reconciliation: layer-3 accumulated PIT series vs official daily_basic.

Once the upstream materialized table (alt.valuation_daily) ships, the daily
snapshots this adapter has been accumulating (layer 3) can be checked against
OFFICIAL history over the overlap window - the same discipline as the
bulk/tail reconciliation (resume.py): immutable truth must agree, disagreement
is an alarm, never something to overwrite silently.

Inputs are LONG frames with the panel convention
``[date, instrument, field, value]`` (as produced by fetch/accumulate paths).
Two-sided overlap: a (date, instrument, field) present on only one side is
reported as ``missing_<side>`` (informational), not a mismatch - the
accumulation may legitimately lag upstream.

This module is consumed by ``wmshare-qlib reconcile-daily-basic`` once the
upstream endpoint is live (docs/upstream-daily-basic-plan.md v3 §5 Gate B).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

_TOL_DEFAULT = 1e-6


@dataclass
class ReconcileReport:
    n_compared: int = 0
    n_match: int = 0
    n_mismatch: int = 0
    n_missing_accumulated: int = 0
    n_missing_upstream: int = 0
    tol: float = _TOL_DEFAULT
    mismatches: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Gate B passes at 100% agreement on a NON-EMPTY overlap (plan v3 §5).

        An empty intersection certifies nothing: zero mismatches from zero
        comparisons (budget-truncated pull, disjoint sides, an all-NaN field)
        is absence of evidence, not evidence of agreement - it must never
        print PASS.
        """
        return self.n_compared > 0 and self.n_mismatch == 0

    def as_meta(self) -> dict:
        return {
            "n_compared": self.n_compared,
            "n_match": self.n_match,
            "n_mismatch": self.n_mismatch,
            "n_missing_accumulated": self.n_missing_accumulated,
            "n_missing_upstream": self.n_missing_upstream,
            "tol": self.tol,
            "ok": self.ok,
            "mismatch_sample": self.mismatches[:10],
        }


def _pivot(frame: pd.DataFrame, label: str) -> pd.Series:
    """Long (date, instrument, field, value) -> Series keyed by the triple."""
    required = {"date", "instrument", "field", "value"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{label}: frame missing columns {sorted(missing)}")
    s = frame.set_index(["date", "instrument", "field"])["value"].astype(float)
    if s.index.has_duplicates:
        dup = s.index[s.index.duplicated()].unique().tolist()[:5]
        raise ValueError(f"{label}: duplicate (date, instrument, field) keys, e.g. {dup}")
    return s


def reconcile_daily_basic(
    accumulated: pd.DataFrame,
    upstream: pd.DataFrame,
    *,
    tol: float = _TOL_DEFAULT,
) -> ReconcileReport:
    """Compare layer-3 accumulated values against official daily_basic values.

    ``tol`` is ABSOLUTE on the raw values (both sides are 2.0 base units;
    float noise only). A mismatch beyond tol FAILS the gate: the accumulated
    series and the official core disagree - stop, investigate, never overwrite.
    """
    acc = _pivot(accumulated, "accumulated")
    ups = _pivot(upstream, "upstream")
    report = ReconcileReport(tol=float(tol))
    common = acc.index.intersection(ups.index)
    report.n_compared = len(common)
    report.n_missing_accumulated = len(ups.index.difference(acc.index))
    report.n_missing_upstream = len(acc.index.difference(ups.index))
    for key in common:
        a, u = float(acc.loc[key]), float(ups.loc[key])
        if pd.isna(a) or pd.isna(u):
            report.n_mismatch += 1
            report.mismatches.append(
                {"date": key[0], "instrument": key[1], "field": key[2],
                 "accumulated": a, "upstream": u, "reason": "nan"}
            )
            continue
        if abs(a - u) > tol:
            report.n_mismatch += 1
            report.mismatches.append(
                {"date": key[0], "instrument": key[1], "field": key[2],
                 "accumulated": a, "upstream": u}
            )
    report.n_match = report.n_compared - report.n_mismatch
    return report
