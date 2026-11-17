"""CLI: dump-history / dump-cross-section / accumulate / verify."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import config, cross_section, dump, fetch, panel, qlib_io, resume


def cmd_dump_history(args: argparse.Namespace) -> None:
    symbols = args.symbols or panel.csi300_universe()
    store = args.store or str(Path(args.out) / "_resume")

    # M3-lite: resumable fragment cache. Params guard prevents mixing panels
    # fetched with different n/fields/live/freq into one global-calendar dump.
    params = {
        "n": args.n,
        "fields": list(config.HISTORY_FIELDS),
        "live": bool(args.live),
        "freq": args.freq,
    }
    manifest = resume.init_manifest(store, params=params, resume=args.resume)
    todo = resume.pending_symbols(symbols, manifest)
    cached = resume.done_symbols(manifest) & set(symbols)
    print("[resume]", json.dumps({
        "enabled": bool(args.resume),
        "cached": sorted(cached),
        "todo": todo,
        "cache_state": resume.summary(manifest),
    }, ensure_ascii=False))

    last_metering = None
    all_warnings: list[str] = []
    if todo:
        # on_symbol persists each fragment atomically BEFORE the next symbol,
        # so an interruption keeps already-paid-for data and EC accounting.
        def _save(symbol, instrument, frames, ec):
            nonlocal manifest
            manifest = resume.record_fragment(
                store, manifest, symbol, instrument, frames, ec
            )

        result = fetch.fetch_history_panel(
            todo, live=args.live, n=args.n, client=None,
            max_workers=args.workers, max_ec=args.max_ec, on_symbol=_save,
        )
        last_metering = result.metering.as_meta()
        all_warnings = list(result.warnings)
    else:
        # Bookkeeping, NOT metering: requests=0 / ec_cost=None mean "served from
        # resume cache, nothing was billed THIS run". Cached fragments were
        # already paid for in their original runs (see manifest ec_spent).
        last_metering = {"mode": "live" if args.live else "demo", "requests": 0,
                         "ec_cost": None,
                         "note": "all symbols served from resume cache; ec_cost=None "
                                 "is bookkeeping (fragments paid in original runs), "
                                 "not a metering reading"}

    # Merge ALL cached fragments (old + new) then dump once with a single
    # global calendar — per-symbol .bin would break Qlib seek offsets.
    full_panel = resume.load_panel(store, manifest)
    integrity = panel.assert_panel_integrity(full_panel)
    print("[integrity]", json.dumps(integrity, ensure_ascii=False))
    print("[metering]", json.dumps(last_metering, ensure_ascii=False))
    cache_state = resume.summary(manifest)
    print("[cache-state]", json.dumps(cache_state, ensure_ascii=False))
    for w in all_warnings[:10]:
        print("[warn]", w)
    if not integrity["ok"]:
        raise SystemExit("panel integrity failed; abort before dump")
    dump_manifest = dump.dump_panel(full_panel, args.out, freq=args.freq)
    print("[dump manifest]", json.dumps(dump_manifest, ensure_ascii=False)[:800])
    Path(args.out, "fetch_meta.json").write_text(
        json.dumps(
            {
                "mode": "live" if args.live else "demo",
                "metering": last_metering,
                "integrity": integrity,
                "warnings": all_warnings,
                "cache_state": cache_state,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def cmd_dump_cross_section(args: argparse.Namespace) -> None:
    result = fetch.fetch_cross_section(live=args.live, as_of=args.as_of,
                                       max_ec=args.max_ec, idx=args.idx)
    out = cross_section.write_score_csv(result, args.out, as_of=args.as_of or "snapshot")
    print("[cross-section]", out)
    print("[metering]", json.dumps(result.metering.as_meta(), ensure_ascii=False))
    for w in result.warnings[:10]:
        print("[warn]", w)
    # Immediate validation: can Qlib ScoreFileModel (or equivalent) read it back?
    read = qlib_io.read_score(out, args.as_of or "snapshot")
    print("[readback]", f"backend={read.backend}", end="")
    if read.note:
        print(f" note={read.note}", end="")
    score = read.series
    print(" |", None if score is None else f"rows={len(score)} sum={float(score.sum()):.4f}")


def cmd_accumulate(args: argparse.Namespace) -> None:
    result = fetch.fetch_cross_section(live=args.live, as_of=args.as_of,
                                       max_ec=args.max_ec, idx=args.idx)
    out = cross_section.accumulate(result.frame, args.out, as_of=args.as_of)
    print("[accumulate]", out)
    meta = cross_section.history_meta(
        len(list(Path(args.out).glob("*.parquet"))), args.min_days
    )
    print("[history-meta]", json.dumps(meta, ensure_ascii=False))


def cmd_verify(args: argparse.Namespace) -> None:
    checks = []
    if args.check:
        for item in args.check:
            # instrument:field:date:value
            inst, field, date, value = item.split(":")
            checks.append((inst, field, date, float(value)))
    result = qlib_io.verify_bin(args.qlib_dir, checks)
    status = result["status"]
    verdict = "PASS" if result["ok"] is True else ("UNAVAILABLE" if result["ok"] is None else "FAIL")
    print("[verify]", verdict, f"(status={status}, backend={result.get('backend')})")
    for d in result["details"]:
        print("  ", d)
    for n in result.get("notes", []):
        print("   [note]", n)
    # Note: ok=None means "could not run" (Qlib absent / timeout), not a pass.
    # A missing optional dependency does not fail the CLI; a real mismatch does.
    if result["ok"] is False:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wmshare-qlib")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("dump-history", help="fetch history and dump Qlib .bin")
    h.add_argument("--symbols", nargs="*", default=None)
    h.add_argument("--live", action="store_true")
    h.add_argument("--n", type=int, default=120)
    h.add_argument("--freq", default="day")
    h.add_argument("--out", default="out/qlib_data")
    h.add_argument("--workers", type=int, default=1,
                   help="concurrent fetch workers (default 1 = serial); each owns a client")
    h.add_argument("--max-ec", type=float, default=None,
                   help="hard EC ceiling; stop submitting symbols once reached (batch granularity)")
    h.add_argument("--resume", action="store_true",
                   help="reuse cached fragments, skip already-fetched symbols")
    h.add_argument("--store", default=None,
                   help="fragment cache dir (default: <out>/_resume)")
    h.set_defaults(func=cmd_dump_history)

    c = sub.add_parser("dump-cross-section", help="emit today's score CSV")
    c.add_argument("--live", action="store_true")
    c.add_argument("--as-of", default=None)
    c.add_argument("--max-ec", type=float, default=None,
                   help="hard EC ceiling, checked post-hoc (a single request "
                        "cannot be pre-stopped); REQUIRED for --live")
    c.add_argument("--idx", default=None,
                   help="server index key to shrink the scanned universe "
                        "(e.g. index.hs300 ~300 names ≈ 11k EC vs full market "
                        "~204k EC); recommended for daily accumulation")
    c.add_argument("--out", default="out/score")
    c.set_defaults(func=cmd_dump_cross_section)

    a = sub.add_parser("accumulate", help="append a daily snapshot")
    a.add_argument("--live", action="store_true")
    a.add_argument("--as-of", default=None)
    a.add_argument("--max-ec", type=float, default=None,
                   help="hard EC ceiling, checked post-hoc; REQUIRED for --live")
    a.add_argument("--idx", default=None,
                   help="server index key to shrink the universe (e.g. index.hs300)")
    a.add_argument("--min-days", type=int, default=60)
    a.add_argument("--out", default="out/cs_accum")
    a.set_defaults(func=cmd_accumulate)

    v = sub.add_parser("verify", help="read .bin back with Qlib and check values")
    v.add_argument("--qlib-dir", default="out/qlib_data")
    v.add_argument("--check", nargs="*", default=[])
    v.set_defaults(func=cmd_verify)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
