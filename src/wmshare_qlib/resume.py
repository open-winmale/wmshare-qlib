"""Resumable per-symbol fragment cache for history-panel fetching.

Why fragments (not per-symbol .bin): Qlib .bin offsets are relative to a GLOBAL
calendar (the union of all instruments' dates). Writing one instrument at a time
would produce wrong seek offsets. So we cache each fetched symbol as a long-panel
parquet fragment + a manifest, and only merge + dump once all symbols are done.

Crash safety: ``on_symbol`` in fetch writes a fragment atomically (temp + replace)
and immediately records it in the manifest. A re-run with ``--resume`` reads the
manifest, skips already-cached symbols, and reuses their EC — never re-billing.

No Qlib import here (core layer). No data fabrication: a partial run simply has
fewer fragments; the merge is honest about which symbols are present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import pandas as pd

MANIFEST_NAME = "manifest.json"
FRAG_DIR = "fragments"
COLS = ["date", "symbol", "instrument", "field", "value"]


def _manifest_path(store: str | Path) -> Path:
    return Path(store) / MANIFEST_NAME


def _frag_path(store: str | Path, symbol: str) -> Path:
    # symbol is a bare code (digits); safe as a filename. Keep instrument in the
    # frame body; the filename key is the bare symbol the caller passes around.
    return Path(store) / FRAG_DIR / f"{symbol}.parquet"


def load_manifest(store: str | Path) -> dict:
    p = _manifest_path(store)
    if not p.exists():
        return {"version": 1, "symbols": {}, "ec_spent": 0.0, "params": {}}
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

    ``params`` should include n / fields / live / freq. If a non-resume run
    targets an existing store, it starts a fresh manifest (overwrite is explicit
    by not passing --resume); if --resume mismatches params, fail loud.
    """
    if resume:
        manifest = load_manifest(store)
        old = manifest.get("params", {})
        for key, value in params.items():
            if key in old and old[key] != value:
                raise ValueError(
                    f"resume param mismatch: {key}={value!r} but store has {old[key]!r}; "
                    "use a different --out or drop --resume"
                )
        manifest["params"] = {**old, **params}
        return manifest
    manifest = {"version": 1, "symbols": {}, "ec_spent": 0.0, "params": dict(params)}
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
    ec: float | None,
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

    manifest = dict(manifest)
    symbols = dict(manifest.get("symbols", {}))
    symbols[symbol] = {
        "instrument": instrument,
        "rows": int(len(frag)),
        "fields": sorted(frag["field"].unique().tolist()),
    }
    manifest["symbols"] = symbols
    if ec is not None:
        manifest["ec_spent"] = float(manifest.get("ec_spent", 0.0)) + float(ec)
    _write_manifest_atomic(store, manifest)
    return manifest


def load_panel(store: str | Path, manifest: dict | None = None) -> pd.DataFrame:
    """Merge all cached fragments into one long panel, in symbol order."""
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
        "ec_spent": float(manifest.get("ec_spent", 0.0)),
        "params": manifest.get("params", {}),
    }
