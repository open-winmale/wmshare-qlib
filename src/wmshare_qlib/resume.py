"""Resumable per-symbol fragment cache for history-panel fetching.

Why fragments (not per-symbol .bin): Qlib .bin offsets are relative to a GLOBAL
calendar (the union of all instruments' dates). Writing one instrument at a time
would produce wrong seek offsets. So we cache each fetched symbol as a long-panel
parquet fragment + a manifest, and only merge + dump once all symbols are done.

Crash safety: ``on_symbol`` in fetch writes a fragment atomically (temp + replace)
and immediately records it in the manifest. A re-run with ``--resume`` reads the
manifest, skips already-cached symbols, and reuses their data — never re-fetching.

No Qlib import here (core layer). No data fabrication: a partial run simply has
fewer fragments; the merge is honest about which symbols are present.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Iterable

import pandas as pd

MANIFEST_NAME = "manifest.json"
FRAG_DIR = "fragments"
TAIL_DIR = "tail"
COLS = ["date", "symbol", "instrument", "field", "value"]
# Reconciliation tolerance for (date, instrument, field) rows present in BOTH
# the bulk fragment and the tail: hfq/raw prices, moneyflow and historical
# valuation series are immutable upstream, so overlapping rows must agree
# within float noise. Anything larger is an upstream revision and fails loud
# (research integrity: silent history rewrites are exactly what the honesty
# contract forbids).
RECONCILE_ATOL = 1e-6
RECONCILE_RTOL = 1e-6


def _manifest_path(store: str | Path) -> Path:
    return Path(store) / MANIFEST_NAME


def _frag_path(store: str | Path, symbol: str) -> Path:
    # symbol is a bare code (digits); safe as a filename. Keep instrument in the
    # frame body; the filename key is the bare symbol the caller passes around.
    return Path(store) / FRAG_DIR / f"{symbol}.parquet"


def _tail_path(store: str | Path, symbol: str) -> Path:
    return Path(store) / TAIL_DIR / f"{symbol}.parquet"


def load_manifest(store: str | Path) -> dict:
    p = _manifest_path(store)
    if not p.exists():
        return {"version": 1, "symbols": {}, "params": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _write_manifest_atomic(store: str | Path, manifest: dict) -> None:
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    tmp = _manifest_path(store).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _manifest_path(store))


def init_manifest(
    store: str | Path,
    *,
    params: dict,
    resume: bool,
) -> dict:
    """Create or reuse a manifest. Guard against mixing incompatible params.

    ``params`` should include n / fields / live / freq / adjust. If a
    non-resume run targets an existing store, it starts a fresh manifest
    (overwrite is explicit by not passing --resume); if --resume mismatches
    params, fail loud.

    Two mismatch shapes, both loud:
    - same key, different value (e.g. n=60 vs n=120);
    - a NEW key absent from the store's manifest while the store already holds
      fragments (e.g. resuming a pre-'adjust' store after the hfq_factor
      change): the old fragments' convention is unknowable, so mixing them
      with new ones would silently corrupt price bases. Start a fresh --out.
    """
    if resume:
        manifest = load_manifest(store)
        old = manifest.get("params", {})
        has_fragments = bool(manifest.get("symbols"))
        for key, value in params.items():
            if key in old:
                if old[key] != value:
                    raise ValueError(
                        f"resume param mismatch: {key}={value!r} but store has {old[key]!r}; "
                        "use a different --out or drop --resume"
                    )
            elif has_fragments:
                raise ValueError(
                    f"resume store predates param {key!r} (store manifest has no "
                    f"value for it) - fragments may follow an incompatible "
                    f"convention; use a fresh --out instead of mixing"
                )
        manifest["params"] = {**old, **params}
        return manifest
    manifest = {"version": 1, "symbols": {}, "params": dict(params)}
    _write_manifest_atomic(store, manifest)
    return manifest


def done_symbols(manifest: dict) -> set[str]:
    return set(manifest.get("symbols", {}).keys())


def record_fragment(
    store: str | Path,
    manifest: dict,
    symbol: str,
    instrument: str,
    frames: list[pd.DataFrame],
    *,
    today: str | None = None,
) -> dict:
    """Atomically persist one symbol's frames and record it in the manifest.

    Returns an UPDATED manifest dict (the caller should keep using the returned
    object). A fragment write failure raises; the symbol is NOT marked done, so
    a retry will re-fetch just that symbol.
    """
    frag = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    if not frag.empty and "value" in frag.columns:
        frag["value"] = frag["value"].astype(float)
    for col in COLS:
        if col not in frag.columns:
            frag[col] = pd.Series(dtype="object")

    out = _frag_path(store, symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    frag[COLS].to_parquet(tmp, index=False)
    os.replace(tmp, out)

    # A fresh bulk fragment SUPERSEDES any incremental tail for this symbol
    # (the entry below drops the tail state): keeping the stale file would mix
    # pre-rebuild rows - possibly under another convention - back into the
    # merged panel.
    tail_file = _tail_path(store, symbol)
    if tail_file.exists():
        tail_file.unlink()

    manifest = dict(manifest)
    # Pre-0.9 manifests carried an "ec_spent" accumulator; new runs drop it
    # so legacy stores converge on the current manifest shape.
    manifest.pop("ec_spent", None)
    symbols = dict(manifest.get("symbols", {}))
    dates = pd.to_datetime(frag["date"], errors="coerce").dropna()
    entry = {
        "instrument": instrument,
        "rows": int(len(frag)),
        "fields": sorted(frag["field"].unique().tolist()),
        # P0-1: fetch-date bookkeeping drives the incremental tail cadence
        # (gap days -> tail n; valuation refresh due). ``today`` is injectable
        # so tests can run the stub clock; production always passes None.
        "bulk_date": today or dt.date.today().isoformat(),
        "last_date": (dates.max().date().isoformat() if len(dates) else None),
    }
    symbols[symbol] = entry
    manifest["symbols"] = symbols
    _write_manifest_atomic(store, manifest)
    return manifest


def record_tail_fragment(
    store: str | Path,
    manifest: dict,
    symbol: str,
    instrument: str,
    frames: list[pd.DataFrame],
    *,
    valuation_refreshed: bool,
    daily_basic_last_date: str | None = None,
    today: str | None = None,
) -> dict:
    """Persist one symbol's incremental tail and record its state.

    The tail holds bars appended AFTER the bulk fragment (P0-1). It is MERGED
    with the tail already on disk, never overwritten: the fetch window reaches
    back only ``gap + overlap`` bars, so an overwrite would destroy the older
    part of a previous catch-up tail (bars after the bulk end that are not
    re-covered) - a silent hole the next full re-dump would write into the
    .bin. Overlapping rows are reconciled by ``_merge_rows`` (disagreement
    raises). ``last_date`` advances only when the tail actually carries rows;
    ``valuation_last_date`` advances only when a valuation refresh was
    included, so the cadence decision survives interruption.
    ``daily_basic_last_date`` advances only when a daily_basic read actually
    returned rows (P3-1): it windows the NEXT read, so a failed/empty read must
    not advance it (the retry stays windowed rather than silently re-scanning
    full history).
    """
    frag = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    if not frag.empty and "value" in frag.columns:
        frag["value"] = frag["value"].astype(float)
    for col in COLS:
        if col not in frag.columns:
            frag[col] = pd.Series(dtype="object")

    out = _tail_path(store, symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        merged = _merge_rows([pd.read_parquet(out), frag], label=f"{symbol} tail")
    else:
        merged = frag[COLS].reset_index(drop=True)
    if not merged.empty or out.exists():
        tmp = out.with_suffix(".parquet.tmp")
        merged[COLS].to_parquet(tmp, index=False)
        os.replace(tmp, out)

    manifest = dict(manifest)
    manifest.pop("ec_spent", None)  # legacy pre-0.9 key: dropped, never rewritten
    symbols = dict(manifest.get("symbols", {}))
    entry = dict(symbols.get(symbol, {"instrument": instrument}))
    dates = pd.to_datetime(merged["date"], errors="coerce").dropna()
    tail_state = dict(entry.get("tail") or {})
    if len(dates):
        tail_state["last_date"] = max(
            str(tail_state.get("last_date") or ""), dates.max().date().isoformat()
        )
        tail_state["rows"] = int(len(merged))
    if valuation_refreshed:
        tail_state["valuation_last_date"] = today or dt.date.today().isoformat()
    if daily_basic_last_date:
        tail_state["daily_basic_last_date"] = str(daily_basic_last_date)
    entry["tail"] = tail_state
    symbols[symbol] = entry
    manifest["symbols"] = symbols
    _write_manifest_atomic(store, manifest)
    return manifest


def _merge_rows(frames: list[pd.DataFrame], *, label: str) -> pd.DataFrame:
    """Concat long-panel frames and collapse duplicate keys, reconciling values.

    Overlapping ``(date, instrument, field)`` rows MUST agree within float
    tolerance: hfq/raw prices, moneyflow, valuation and daily_basic series are
    immutable upstream, so a disagreement is an upstream revision (or a
    convention mix that slipped past the manifest guard) and raises - history is
    never silently rewritten. Equal-value duplicates collapse (the first, i.e.
    the OLDEST source, wins); a NaN/NaN pair is not a conflict, and NaN vs a
    value keeps the value (missing is never allowed to shadow a real reading).
    """
    panel = pd.concat(frames, ignore_index=True)
    panel["value"] = panel["value"].astype(float)
    panel = panel[COLS]

    key = ["date", "instrument", "field"]
    dup_mask = panel.duplicated(key, keep=False)
    if dup_mask.any():
        dups = panel.loc[dup_mask]
        g = dups.groupby(key, sort=False)["value"]
        lo = g.transform("min")
        hi = g.transform("max")
        tol = RECONCILE_ATOL + RECONCILE_RTOL * pd.concat([lo.abs(), hi.abs()], axis=1).max(axis=1)
        conflict = (hi - lo) > tol
        if conflict.any():
            bad = dups.loc[conflict, key].drop_duplicates().head(3)
            detail = "; ".join(f"{r.date}/{r.instrument}/{r.field}" for r in bad.itertuples())
            raise ValueError(
                f"{label} reconciliation FAILED: overlapping rows disagree "
                f"beyond tolerance (e.g. {detail}) - upstream history revision "
                f"or mixed conventions; refusing to merge"
            )
        # NaN sorts after any value, so keep="first" prefers a real reading.
        panel = panel.assign(_isnan=panel["value"].isna()).sort_values(
            key + ["_isnan"], kind="stable"
        )
        panel = panel.drop(columns=["_isnan"]).drop_duplicates(key, keep="first")
    return panel.reset_index(drop=True)


def load_panel(store: str | Path, manifest: dict | None = None) -> pd.DataFrame:
    """Merge all cached bulk fragments into one long panel, in symbol order."""
    manifest = manifest if manifest is not None else load_manifest(store)
    order = sorted(manifest.get("symbols", {}).keys())
    parts = []
    for symbol in order:
        p = _frag_path(store, symbol)
        if p.exists():
            parts.append(pd.read_parquet(p))
    if not parts:
        return pd.DataFrame(columns=COLS)
    panel = pd.concat(parts, ignore_index=True)
    panel["value"] = panel["value"].astype(float)
    return panel[COLS]


def load_panel_with_tail(store: str | Path, manifest: dict | None = None) -> pd.DataFrame:
    """Bulk fragments + incremental tails, with a loud reconciliation gate (P0-1).

    Overlapping (date, instrument, field) rows between a bulk fragment and a
    tail MUST agree within float tolerance (see ``_merge_rows``); a
    disagreement raises - history is never silently rewritten. Only symbols the
    manifest still records a ``tail`` state for are read, so an orphaned tail
    file from a superseded bulk can never leak back into the panel.
    """
    manifest = manifest if manifest is not None else load_manifest(store)
    order = sorted(manifest.get("symbols", {}).keys())
    parts: list[pd.DataFrame] = []
    for symbol in order:
        p = _frag_path(store, symbol)
        if p.exists():
            parts.append(pd.read_parquet(p))
    for symbol in order:  # tails appended AFTER all bulks -> bulk wins ties
        entry = (manifest.get("symbols") or {}).get(symbol) or {}
        if not entry.get("tail"):
            continue
        p = _tail_path(store, symbol)
        if p.exists():
            parts.append(pd.read_parquet(p))
    if not parts:
        return pd.DataFrame(columns=COLS)
    return _merge_rows(parts, label="bulk/tail")


def pending_symbols(symbols: Iterable[str], manifest: dict) -> list[str]:
    """Symbols still to fetch (dedup, preserve caller order)."""
    done = done_symbols(manifest)
    seen: set[str] = set()
    out = []
    for s in symbols:
        if s in done or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def summary(manifest: dict) -> dict:
    """Human-readable cache state for CLI logging."""
    syms = manifest.get("symbols", {})
    return {
        "cached_symbols": sorted(syms.keys()),
        "n_cached": len(syms),
        "rows": int(sum(v.get("rows", 0) for v in syms.values())),
        "params": manifest.get("params", {}),
    }
