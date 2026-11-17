#!/usr/bin/env python3
"""Reproducible benchmark: wmshare precomputed data vs local self-fetch.

Three metrics, all with a fixed, auditable definition:

  1. CONSISTENCY  - demo snapshot (unadjusted close) vs an independent local
                    source (unadjusted close), aligned on the SAME calendar date.
                    Reports the per-day relative error distribution. A value of
                    0 means byte-identical prices; we report the real number,
                    never round to zero and never fabricate a match.
  2. LATENCY      - wall-clock time to fetch the same N symbols on both paths.
                    Both paths loop per symbol (demo client has no batch API),
                    so this compares per-symbol fetch+parse cost, NOT a batch
                    advantage. Cold vs warm is stated, not hidden.
  3. CODE SIZE    - non-blank, non-comment source lines of two *reference*
                    implementations that produce the same (date, close) frame.
                    Counted from the functions below via inspect, so anyone can
                    re-run and reproduce the number exactly.

Network honesty:
  akshare hits public endpoints that may be blocked by a proxy. The script
  tries multiple sources; if ALL fail, consistency/latency are reported as
  status="unavailable" with the reason. It NEVER substitutes demo data for the
  independent source (that would make consistency trivially 0 and be a lie).

Scope:
  Offline demo snapshot (zero EC). No live calls, no credentials needed for
  the wmshare side. Run with:

    PYTHONPATH=src python examples/benchmark/benchmark.py \
      --symbols 600519 000858 601318 --n 60 --out-json /tmp/bench.json
"""

from __future__ import annotations

import argparse
import inspect
import json
import statistics
import sys
import time
from datetime import datetime

# --------------------------------------------------------------------------
# Reference implementations (source of the CODE SIZE metric)
# --------------------------------------------------------------------------


def reference_wmshare(symbols, n):
    """Fetch (date, close) frames via wmshare. Demo client -> zero EC."""
    import wmshare

    client = wmshare.demo_client()
    out = {}
    for sym in symbols:
        df = client.kline.bars(sym, n=n, _df=True)
        out[sym] = df[["date", "close"]].copy()
    return out


def reference_local(symbols, n, fetcher):
    """Fetch (date, close) frames via a local source, then normalise to the
    same shape as the wmshare path. NOTE: this normalisation (rename, sort,
    date parse) is work the wmshare path does NOT need - that is the point of
    the CODE SIZE comparison."""
    out = {}
    for sym in symbols:
        df = fetcher(sym, n)
        df = df.rename(columns={"date": "date", "close": "close"})
        df = df[["date", "close"]].copy()
        df["date"] = df["date"].astype(str).str[:10]
        df = df.sort_values("date").reset_index(drop=True)
        out[sym] = df
    return out


def _count_loc(fn) -> int:
    """Non-blank source lines of a function's statements (docstring excluded).

    Uses the AST so a multi-line docstring can never swallow the real body.
    Counts every source line covered by a statement node, which is exact and
    reproducible for anyone re-running this file.
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(fn))
    func = ast.parse(src).body[0]
    body = list(func.body)
    # Drop the leading docstring expression if present.
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    lines: set[int] = set()
    for node in body:
        lo = getattr(node, "lineno", None)
        hi = getattr(node, "end_lineno", lo)
        if lo is not None:
            lines.update(range(lo, (hi or lo) + 1))
    return len(lines)


# --------------------------------------------------------------------------
# Independent data source with graceful multi-source fallback
# --------------------------------------------------------------------------


def _make_local_fetcher(prefer_sina: bool, start_date: str, end_date: str):
    """Return (fetcher, provenance) or (None, reason).

    akshare exposes several A-share daily endpoints. Some are unreachable
    behind certain proxies. Try the ones most likely to work; the Sina daily
    endpoint is unadjusted when adjust="" - matching the demo snapshot.
    Window is CLI-parameterized (--start/--end, YYYYMMDD) - hardcoded dates
    silently rot as the demo snapshot moves forward in time.
    """
    try:
        import akshare as ak
    except Exception as e:  # pragma: no cover - dependency optional
        return None, f"akshare import failed: {type(e).__name__}: {e}"

    def via_sina(sym, n):
        prefix = "sh" if sym.startswith(("6", "9")) else "sz"
        df = ak.stock_zh_a_daily(
            symbol=f"{prefix}{sym}", start_date=start_date, end_date=end_date, adjust=""
        )
        df = df.rename(columns={"date": "date", "close": "close"})
        return df.tail(n).reset_index(drop=True)

    def via_eastmoney(sym, n):
        df = ak.stock_zh_a_hist(
            symbol=sym, period="daily", start_date=start_date, end_date=end_date, adjust=""
        )
        df = df.rename(columns={"日期": "date", "收盘": "close"})
        return df.tail(n).reset_index(drop=True)

    order = (via_sina, via_eastmoney) if prefer_sina else (via_eastmoney, via_sina)
    errors = []
    for fn in order:
        try:
            probe = fn("600519", 3)
            if probe is not None and len(probe) > 0:
                return fn, fn.__name__
        except Exception as e:
            errors.append(f"{fn.__name__}: {type(e).__name__}: {str(e)[:80]}")
    return None, "all sources failed: " + " | ".join(errors)


# --------------------------------------------------------------------------
# Metric 1: consistency
# --------------------------------------------------------------------------


def measure_consistency(wmshare_frames, local_frames) -> dict:
    """Per-day relative error |w - l| / |l| on the intersection of dates."""
    rel_errors = []
    matched = 0
    total = 0
    anomalies = []
    for sym, wdf in wmshare_frames.items():
        ldf = local_frames.get(sym)
        if ldf is None:
            continue
        wm_row = wdf.copy()
        loc_row = ldf.copy()
        wm_row["date"] = wm_row["date"].astype(str).str[:10]
        loc_row["date"] = loc_row["date"].astype(str).str[:10]
        merged = wm_row.merge(loc_row, on="date", suffixes=("_w", "_l"), how="inner")
        total += len(wm_row)
        matched += len(merged)
        for _, r in merged.iterrows():
            lv = float(r["close_l"])
            wv = float(r["close_w"])
            if lv == 0:
                anomalies.append(f"{sym} zero local close on {r['date']}")
                continue
            rel_errors.append(abs(wv - lv) / abs(lv))
    if not rel_errors:
        return {
            "status": "unavailable",
            "reason": "no overlapping (symbol, date) pairs",
            "matched_days": matched,
            "total_wmshare_days": total,
        }
    return {
        "status": "ok",
        "matched_days": matched,
        "total_wmshare_days": total,
        "coverage": round(matched / total, 4) if total else None,
        "rel_error_max": round(max(rel_errors), 8),
        "rel_error_median": round(statistics.median(rel_errors), 8),
        "rel_error_mean": round(statistics.mean(rel_errors), 8),
        "exact_matches": sum(1 for e in rel_errors if e == 0.0),
        "anomalies": anomalies[:5],
    }


# --------------------------------------------------------------------------
# Metric 2: latency
# --------------------------------------------------------------------------


def measure_latency(symbols, n, fetcher) -> dict:
    """Wall-clock fetch time. Both paths loop per symbol. Report each run."""
    if fetcher is None:
        return {"status": "unavailable", "reason": "independent source unavailable"}

    t0 = time.perf_counter()
    reference_wmshare(symbols, n)
    wmshare_s = round(time.perf_counter() - t0, 4)

    t0 = time.perf_counter()
    reference_local(symbols, n, fetcher)
    local_s = round(time.perf_counter() - t0, 4)

    return {
        "status": "ok",
        "symbols": len(symbols),
        "n_per_symbol": n,
        "wmshare_seconds": wmshare_s,
        "local_seconds": local_s,
        "speedup": round(local_s / wmshare_s, 2) if wmshare_s > 0 else None,
        "note": (
            "NOT an apples-to-apples speedup: wmshare demo is a local in-memory "
            "snapshot (zero network, precomputed); local path is live HTTP. The "
            "number measures 'precomputed snapshot vs live fetch', which is the "
            "actual trade-off. Single run; warm proxy cache may bias local down."
        ),
    }


# --------------------------------------------------------------------------
# Metric 3: code size
# --------------------------------------------------------------------------


def measure_code_size() -> dict:
    w = _count_loc(reference_wmshare)
    loc_local = _count_loc(reference_local)
    return {
        "status": "ok",
        "wmshare_reference_loc": w,
        "local_reference_loc": loc_local,
        "ratio": round(loc_local / w, 2) if w else None,
        "note": "non-blank non-comment lines of the two reference functions in this file",
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="*", default=["600519", "000858", "601318"])
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--start", default="20260601",
                    help="independent-source window start, YYYYMMDD")
    ap.add_argument("--end", default="20261101",
                    help="independent-source window end, YYYYMMDD")
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--prefer-sina", action="store_true",
                    help="try the Sina daily endpoint first (more proxy-tolerant)")
    args = ap.parse_args()

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "offline demo snapshot (zero EC), unadjusted close",
        "symbols": args.symbols,
        "n": args.n,
        "python": sys.version.split()[0],
    }

    try:
        wmshare_frames = reference_wmshare(args.symbols, args.n)
        result["wmshare_path"] = "ok"
    except Exception as e:
        result["wmshare_path"] = f"error: {type(e).__name__}: {e}"
        wmshare_frames = {}

    fetcher, provenance = _make_local_fetcher(
        prefer_sina=args.prefer_sina, start_date=args.start, end_date=args.end
    )
    result["local_source"] = provenance

    if fetcher is not None and wmshare_frames:
        try:
            local_frames = reference_local(args.symbols, args.n, fetcher)
            result["consistency"] = measure_consistency(wmshare_frames, local_frames)
        except Exception as e:
            result["consistency"] = {
                "status": "unavailable",
                "reason": f"local fetch raised {type(e).__name__}: {str(e)[:120]}",
            }
    else:
        result["consistency"] = {
            "status": "unavailable",
            "reason": f"wmshare_path={result['wmshare_path']}, local_source={provenance}",
        }

    result["latency"] = measure_latency(args.symbols, args.n, fetcher)
    result["code_size"] = measure_code_size()

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[saved] {args.out_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
