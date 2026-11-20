"""CLI: dump-history / update-history / dump-cross-section / accumulate / verify."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from . import config, cross_section, dump, estimate, fetch, normalize, panel, qlib_io, resume

DEFAULT_DUMP_N = 120


def _print_cross_section_estimate(args: argparse.Namespace) -> None:
    """P1-3 --dry-run: pre-flight EC quote, no request fired."""
    via = getattr(args, "via", "screener")
    if via == "snapshot":
        if not args.symbols:
            raise SystemExit("--via snapshot requires --symbols (>=3 equity codes)")
        n = len(args.symbols)
        est = estimate.snapshot_batch(n)
    else:
        n = "full universe"
        est = estimate.screener_full()
        idx = getattr(args, "idx", None)
        if idx:
            # The anchor is a FULL-market probe; a scoped idx= query is cheaper
            # by an unprobed factor. Say so instead of presenting the full-market
            # number as if it were an index-sized quote.
            n = f"idx={idx} (anchor unprobed; quoting the full-market worst case)"
    print(
        "[dry-run]",
        json.dumps(
            {
                "dry_run": True,
                "via": via,
                "mode": "live" if args.live else "demo",
                "symbols": n,
                "est_ec": round(est, 1),
                "max_ec": args.max_ec,
                "ceiling_warning": (
                    f"estimate {est:.0f} EC exceeds max_ec {args.max_ec}"
                    if args.max_ec is not None and est > args.max_ec
                    else None
                ),
                "note": "estimate over measured anchors (config.EC_*); "
                "the paid metering.ec_cost is the only quote; "
                "--max-ec stays the binding ceiling; demo spends 0 EC",
            },
            ensure_ascii=False,
        ),
    )


def cmd_dump_history(args: argparse.Namespace) -> None:
    symbols = args.symbols or panel.csi300_universe()
    store = args.store or str(Path(args.out) / "_resume")
    try:
        adjust_mode = config.adjustment_mode(bool(args.live), args.adjust)
    except ValueError as exc:
        raise SystemExit(str(exc))
    # P1-2 deep history: --years maps to bars (250/year), one call with a
    # raised served-bar cap (range queries are falsified - see roadmap §2.5).
    max_limit: int | None = None
    if getattr(args, "years", None):
        if getattr(args, "n", None) not in (None, DEFAULT_DUMP_N):
            raise SystemExit("--years and --n are mutually exclusive")
        args.n = config.years_to_n(args.years)
        max_limit = max(args.n, 500)

    # M3-lite: resumable fragment cache. Params guard prevents mixing panels
    # fetched with different n/fields/live/freq - and now adjustment modes -
    # into one global-calendar dump (a pre-'adjust' store predating hfq_factor
    # is refused: its fragments' convention is unknowable).
    if args.dry_run:
        # P1-3 pre-flight quote BEFORE any store write or request.
        metrics = 4 if args.live else 1  # demo snapshot stores pe only
        per = estimate.bulk_symbol(
            args.n,
            live=bool(args.live),
            adjust_mode=adjust_mode,
            valuation_metrics=metrics,
            daily_basic=bool(getattr(args, "daily_basic", False)),
        )
        print(
            "[dry-run]",
            json.dumps(
                {
                    "dry_run": True,
                    "mode": "live" if args.live else "demo",
                    "adjust": adjust_mode,
                    "symbols": len(symbols),
                    "n": args.n,
                    "est_per_symbol_ec": round(per, 1),
                    "est_total_ec": round(per * len(symbols), 1),
                    "max_ec": args.max_ec,
                    "ceiling_warning": (
                        f"estimate {per * len(symbols):.0f} EC exceeds max_ec "
                        f"{args.max_ec}: some symbols will NOT be submitted"
                        if args.live
                        and args.max_ec is not None
                        and per * len(symbols) > args.max_ec
                        else None
                    ),
                    "deep_history_note": (
                        f"n={args.n} > 500 served bars: per-bar pricing is EXTRAPOLATED "
                        f"beyond the 500-bar probe (linear fit, roadmap §2.5) - probe "
                        f"pending; treat the estimate as a lower bound until re-probed"
                        if args.live and args.n > 500
                        else None
                    ),
                    "note": "estimate over measured anchors (config.EC_*); live assumes "
                    "all 4 valuation metrics land (cold cache would bill less); "
                    "demo spends 0 EC",
                },
                ensure_ascii=False,
            ),
        )
        return
    fields = config.HISTORY_FIELDS
    if getattr(args, "daily_basic", False):
        fields = fields + config.PANEL_DAILY_BASIC_FIELDS
    params = {
        "n": args.n,
        "fields": list(fields),
        "live": bool(args.live),
        "freq": args.freq,
        "adjust": adjust_mode,
        "valuation_window": config.VALUATION_WINDOW,
    }
    manifest = resume.init_manifest(store, params=params, resume=args.resume)
    todo = resume.pending_symbols(symbols, manifest)
    cached = resume.done_symbols(manifest) & set(symbols)
    print(
        "[resume]",
        json.dumps(
            {
                "enabled": bool(args.resume),
                "cached": sorted(cached),
                "todo": todo,
                "cache_state": resume.summary(manifest),
            },
            ensure_ascii=False,
        ),
    )

    last_metering = None
    all_warnings: list[str] = []
    if todo:
        # on_symbol persists each fragment atomically BEFORE the next symbol,
        # so an interruption keeps already-paid-for data and EC accounting.
        def _save(symbol, instrument, frames, ec):
            nonlocal manifest
            manifest = resume.record_fragment(store, manifest, symbol, instrument, frames, ec)

        result = fetch.fetch_history_panel(
            todo,
            live=args.live,
            n=args.n,
            client=None,
            fields=fields,
            max_workers=args.workers,
            max_ec=args.max_ec,
            adjust_mode=adjust_mode,
            max_limit=max_limit,
            on_symbol=_save,
        )
        last_metering = result.metering.as_meta()
        all_warnings = list(result.warnings)
    else:
        # Bookkeeping, NOT metering: requests=0 / ec_cost=None mean "served from
        # resume cache, nothing was billed THIS run". Cached fragments were
        # already paid for in their original runs (see manifest ec_spent).
        last_metering = {
            "mode": "live" if args.live else "demo",
            "requests": 0,
            "ec_cost": None,
            "note": "all symbols served from resume cache; ec_cost=None "
            "is bookkeeping (fragments paid in original runs), "
            "not a metering reading",
        }

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
                "adjust": adjust_mode,
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


def cmd_update_history(args: argparse.Namespace) -> None:
    """P0-1 incremental update: fetch NEW bars only (dual hfq/raw tail),
    reconcile every overlapping row against stored history, then re-dump the
    FULL .bin from the merged store (local IO, zero EC - only the fetch bills).

    Live-only (demo has no adj=/window support) and hfq_factor-only
    (incremental appends need an immutable basis; qfq re-anchors at every
    ex-div event, so appending under qfq_const would silently corrupt the
    panel across corporate actions).
    """
    if not args.live:
        raise SystemExit(
            "update-history is live-only (demo snapshot supports neither "
            "adj= nor n-window refetch); use dump-history for the full demo path"
        )
    adjust_mode = config.adjustment_mode(True, args.adjust)
    if adjust_mode != "hfq_factor":
        raise SystemExit(
            "update-history requires hfq_factor: incremental appends need an "
            "immutable basis (qfq_const re-anchors at every ex-div event, so "
            "appended bars would not line up with stored history)"
        )
    if args.max_ec is None:
        raise SystemExit(
            "update-history requires --max-ec: tails bill per symbol "
            "(kline adj+raw = 2x(156+67k) EC, deterministic; see "
            "docs/optimization-roadmap.md section 2.5)"
        )
    store = args.store or str(Path(args.out) / "_resume")
    manifest = resume.load_manifest(store)
    if not manifest.get("symbols"):
        raise SystemExit(f"no fragment store at {store}; run dump-history first")
    if manifest.get("params", {}).get("adjust") != "hfq_factor":
        raise SystemExit(
            "store was not built under hfq_factor (or predates the adjust "
            "param): tail rows cannot reconcile against an unknown convention; "
            "rebuild the store with dump-history --live first"
        )

    symbols = args.symbols or sorted(manifest["symbols"].keys())
    last_dates: dict[str, str] = {}
    v_last: dict[str, str] = {}
    db_last: dict[str, str | None] = {}
    no_bulk: list[str] = []
    for s in symbols:
        entry = manifest["symbols"].get(s) or {}
        tail = entry.get("tail") or {}
        last = tail.get("last_date") or entry.get("last_date")
        if not last:
            no_bulk.append(s)
            continue
        last_dates[s] = str(last)
        # valuation cadence: last refresh if any, else the bulk fetch date
        v_last[s] = str(tail.get("valuation_last_date") or entry.get("bulk_date") or "")
        # daily_basic read window (P3-1): resume pointer; None = first-ever
        # read (the fetch layer falls back to the tail window start)
        db_last[s] = (
            str(tail.get("daily_basic_last_date")) if tail.get("daily_basic_last_date") else None
        )
    if no_bulk:
        print("[skip]", json.dumps({"no_bulk_fragment": sorted(no_bulk)}, ensure_ascii=False))
        symbols = [s for s in symbols if s not in no_bulk]
    if not symbols:
        print("[update]", "nothing to update (no symbols with bulk fragments)")
        return

    fields = config.HISTORY_FIELDS
    want_db = bool(getattr(args, "daily_basic", False))
    if want_db:
        fields = fields + config.PANEL_DAILY_BASIC_FIELDS

    if args.dry_run:
        # P1-3 pre-flight quote: pure local plan over the manifest, zero EC.
        today = dt.date.today()
        plan = []
        total = 0.0
        for s in symbols:
            last = last_dates.get(s)
            if not last:
                plan.append({"symbol": s, "action": "skip: no bulk fragment"})
                continue
            gap = (today - dt.date.fromisoformat(last)).days
            k = gap + args.tail_overlap
            if k > args.tail_max_n:
                plan.append({"symbol": s, "action": f"skip: gap {gap}d needs k={k} > tail_max_n"})
                continue
            v = v_last.get(s)
            v_due = (not v) or ((today - dt.date.fromisoformat(v)).days >= args.valuation_days)
            db_span = 0
            if want_db:
                # windowed read span: resume pointer back-span, tail window on
                # first-ever read (mirrors fetch_history_tail's plan phase)
                db = db_last.get(s)
                anchor = (
                    dt.date.fromisoformat(db)
                    if db
                    else (today - dt.timedelta(days=args.tail_max_n))
                )
                db_span = max(1, (today - anchor).days)
            e = estimate.tail_symbol(
                k, valuation_due=v_due, daily_basic=want_db, daily_basic_span_days=db_span
            )
            total += e
            plan.append(
                {
                    "symbol": s,
                    "gap_days": gap,
                    "k": k,
                    "valuation_due": v_due,
                    "est_ec": round(e, 1),
                }
            )
        print(
            "[dry-run]",
            json.dumps(
                {
                    "dry_run": True,
                    "mode": "live",
                    "adjust": adjust_mode,
                    "symbols": len(symbols),
                    "est_total_ec": round(total, 1),
                    "max_ec": args.max_ec,
                    "ceiling_warning": (
                        f"estimate {total:.0f} EC exceeds max_ec {args.max_ec}: "
                        f"some symbols will NOT be submitted"
                        if args.max_ec is not None and total > args.max_ec
                        else None
                    ),
                    "per_symbol": plan[:20],
                    "note": "estimate over measured anchors (config.EC_*); "
                    "--max-ec stays the binding ceiling",
                },
                ensure_ascii=False,
            ),
        )
        return

    def _save_tail(symbol, instrument, frames, ec):
        nonlocal manifest
        # valuation_refreshed is inferred from the RESULT, not the plan: if the
        # refresh was planned but every metric failed (cold cache), the cadence
        # must NOT advance - the next run retries it.
        refreshed = any(
            set(f.get("field", pd.Series(dtype=object)).unique()) & set(config.VALUATION_FIELDS)
            for f in frames
            if hasattr(f, "columns") and "field" in f.columns
        )
        # daily_basic pointer likewise advances on RESULT only (P3-1): an
        # empty/failed read leaves the window pointer where it was, so the
        # next run retries the same window instead of silently skipping.
        db_dates: list[str] = []
        for f in frames:
            if not hasattr(f, "columns") or "field" not in f.columns:
                continue
            if set(f["field"].unique()) & set(config.PANEL_DAILY_BASIC_FIELDS):
                d = pd.to_datetime(f["date"], errors="coerce").dropna()
                db_dates.extend(d.dt.date.astype(str).tolist())
        manifest = resume.record_tail_fragment(
            store,
            manifest,
            symbol,
            instrument,
            frames,
            ec,
            valuation_refreshed=refreshed,
            daily_basic_last_date=max(db_dates) if db_dates else None,
        )

    fields = config.HISTORY_FIELDS
    result = fetch.fetch_history_tail(
        symbols,
        last_dates=last_dates,
        valuation_last_dates={k: (v or None) for k, v in v_last.items()},
        daily_basic_last_dates=dict(db_last),
        live=True,
        overlap=args.tail_overlap,
        tail_max_n=args.tail_max_n,
        valuation_days=args.valuation_days,
        fields=fields,
        client=None,
        max_workers=args.workers,
        max_ec=args.max_ec,
        on_symbol=_save_tail,
    )
    print("[metering]", json.dumps(result.metering.as_meta(), ensure_ascii=False))
    for w in result.warnings[:10]:
        print("[warn]", w)

    # Reconcile bulk+tail and FULL re-dump from the merged store (zero EC).
    full_panel = resume.load_panel_with_tail(store, manifest)
    integrity = panel.assert_panel_integrity(full_panel)
    print("[integrity]", json.dumps(integrity, ensure_ascii=False))
    if not integrity["ok"]:
        raise SystemExit("panel integrity failed after tail merge; abort before dump")
    dump_manifest = dump.dump_panel(full_panel, args.out, freq=args.freq)
    print("[dump manifest]", json.dumps(dump_manifest, ensure_ascii=False)[:800])
    Path(args.out, "fetch_meta.json").write_text(
        json.dumps(
            {
                "mode": "live",
                "update": True,
                "adjust": adjust_mode,
                "metering": result.metering.as_meta(),
                "integrity": integrity,
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _fetch_cross_section_by_via(args: argparse.Namespace):
    """Dispatch the cross-section fetch by --via (P0-3).

    screener: full-market fundamentals composite (server scans everything;
    ~204k EC full market) - the monthly cadence path.
    snapshot: client-side-subset technical composite via kline.snapshot
    (~100 EC/票, the only path where the operator controls the scanned
    universe) - the hs300-scale daily cadence path. Requires --symbols.
    """
    if getattr(args, "via", "screener") == "snapshot":
        if not args.symbols:
            raise SystemExit(
                "--via snapshot requires --symbols (an explicit universe of "
                "equity codes, e.g. your index membership file; the server "
                "does not serve index subsets)"
            )
        try:
            return fetch.fetch_cross_section_snapshot(
                args.symbols,
                live=args.live,
                as_of=args.as_of,
                max_ec=args.max_ec,
            )
        except normalize.NormalizeError as exc:
            raise SystemExit(str(exc))
    return fetch.fetch_cross_section(
        live=args.live, as_of=args.as_of, max_ec=args.max_ec, idx=args.idx
    )


def cmd_dump_cross_section(args: argparse.Namespace) -> None:
    if getattr(args, "dry_run", False):
        _print_cross_section_estimate(args)
        return
    result = _fetch_cross_section_by_via(args)
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
    if getattr(args, "dry_run", False):
        _print_cross_section_estimate(args)
        return
    result = _fetch_cross_section_by_via(args)
    out = cross_section.accumulate(result.frame, args.out, as_of=args.as_of)
    print("[accumulate]", out)
    files = sorted(Path(args.out).glob("*.parquet"))  # ISO stems sort chronologically
    available_from = files[0].stem if files else None
    meta = cross_section.history_meta(len(files), args.min_days, available_from=available_from)
    print("[history-meta]", json.dumps(meta, ensure_ascii=False))
    # P2-2: automatic promotion check - when the configurable cold-start guard
    # passes, promote the accumulated snapshots to a backtestable panel here
    # instead of leaving the operator to remember promote_to_panel exists.
    if len(files) >= args.min_days:
        panel_out = Path(args.out).with_name(Path(args.out).name + "_panel.parquet")
        _frame, status = cross_section.promote_to_panel(
            args.out, min_days=args.min_days, out_parquet=panel_out
        )
        print("[promote]", status)


def cmd_write_universe(args: argparse.Namespace) -> None:
    """P1-1: write instruments/<name>.txt (Qlib convention) so
    ``D.instruments("<name>")`` works directly.

    Sources: ``--index <key>`` (live, real membership via indices.members -
    cost not yet probed) or ``--codes-file`` (one code per line; qlib-style
    SH600519 passes through, bare 600519 maps via stocks.list - never
    inferred from digit ranges). Optional ST/suspension hygiene pre-pass
    (live snapshot, ~100 EC/票). The universe is INTERSECTED with the dumped
    panel's actual data coverage - a universe file can only make instruments
    backtestable that the dump contains.
    """
    qlib_dir = Path(args.qlib_dir)
    if not (qlib_dir / "instruments" / "all.txt").exists():
        raise SystemExit(
            f"no dumped panel at {qlib_dir} (missing instruments/all.txt); run dump-history first"
        )
    codes: list[str] = []
    if args.index:
        if not args.live:
            raise SystemExit(
                "--index requires --live (demo client has no indices namespace; verified offline)"
            )
        result = fetch.fetch_index_members(fetch._client(live=True), args.index)
        codes = result.frame["symbol"].astype(str).tolist()
        print("[metering]", json.dumps(result.metering.as_meta(), ensure_ascii=False))
        for w in result.warnings[:5]:
            print("[warn]", w)
    elif args.codes_file:
        codes = [
            ln.strip()
            for ln in Path(args.codes_file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    else:
        raise SystemExit("one of --index <key> or --codes-file <path> is required")
    if not codes:
        raise SystemExit("universe source produced no codes")

    # hygiene guards BEFORE any client creation: the CLI must fail fast on
    # argument mistakes without touching credentials (CI has none - a live
    # client built first turned a SystemExit into an AuthError there)
    if args.exclude_st or args.exclude_suspended:
        if not args.live:
            raise SystemExit("hygiene filtering needs --live (snapshot batch)")
        if args.max_ec is None:
            raise SystemExit(
                "hygiene filtering requires --max-ec (snapshot batch bills per ticket, ~100 EC/票)"
            )

    # normalize codes -> qlib instruments (explicit mapping; no digit-range guesses)
    client = fetch._client(live=bool(args.live))
    instrument_map = normalize.build_instrument_map(client)
    instruments: list[str] = []
    unmapped: list[str] = []
    for code in codes:
        c = code.upper()
        if c[:2] in {"SH", "SZ", "BJ"} and c[2:].isdigit():
            instruments.append(c)  # already qlib-style
        elif code in instrument_map:
            instruments.append(instrument_map[code])
        else:
            unmapped.append(code)
    if unmapped:
        print("[warn]", f"{len(unmapped)} code(s) unmapped (dropped): {unmapped[:5]}")

    if args.exclude_st or args.exclude_suspended:
        hyg = fetch.filter_universe_hygiene(
            instruments,
            client=client,
            exclude_st=args.exclude_st,
            exclude_suspended=args.exclude_suspended,
            max_ec=args.max_ec,
        )
        instruments = hyg.frame["instrument"].tolist()
        print("[metering]", json.dumps(hyg.metering.as_meta(), ensure_ascii=False))
        for w in hyg.warnings[:5]:
            print("[warn]", w)

    ranges = dump.instrument_date_ranges(qlib_dir, freq=args.freq)
    covered = {inst: ranges[inst] for inst in instruments if inst in ranges}
    missing = [inst for inst in instruments if inst not in ranges]
    if missing:
        print(
            "[warn]",
            f"{len(missing)} instrument(s) not in the dumped panel (dropped, e.g. {missing[:5]})",
        )
    if not covered:
        raise SystemExit("no universe member has dumped data; nothing to write")
    out = dump.write_universe_file(qlib_dir, args.name, covered)
    print(
        "[universe]",
        json.dumps(
            {
                "path": str(out),
                "name": args.name,
                "written": len(covered),
                "source": "index" if args.index else "codes-file",
                "hygiene": bool(args.exclude_st or args.exclude_suspended),
            },
            ensure_ascii=False,
        ),
    )


def cmd_reconcile_daily_basic(args: argparse.Namespace) -> None:
    """Gate B (plan v3 §5): accumulated PIT vs official daily_basic, 100% or fail.

    Two input modes:
      * ``--accumulated/--upstream`` files (operator hand-feed; original path);
      * ``--from-upstream --store <dir>`` (P3-3 automation): the official side
        is pulled via ``valuation.daily_basic`` over each symbol's accumulated
        window (windowed read, floor + ~23 EC/doc) - no hand-fed files, the
        gate can run on a cadence instead of gathering dust.
    """
    import pandas as pd

    from . import config, fetch, reconcile, resume

    def _read(path: str) -> pd.DataFrame:
        p = Path(path)
        if p.suffix == ".parquet":
            return pd.read_parquet(p)
        return pd.read_csv(p)

    if getattr(args, "from_upstream", False):
        if not args.store:
            raise SystemExit("--from-upstream requires --store (resume store dir)")
        manifest = resume.load_manifest(args.store)
        symbols = args.symbols or sorted(manifest["symbols"].keys())
        full = resume.load_panel_with_tail(args.store, manifest)
        acc = full[full["field"].isin(config.PANEL_DAILY_BASIC_FIELDS)].copy()
        if acc.empty:
            raise SystemExit(
                "accumulated store has no daily_basic fields; run "
                "update-history --daily-basic first (Gate B audits that side)"
            )
        client = fetch._client(live=True)
        ec_seen: list[float] = []

        def _meter() -> None:
            last = getattr(client, "last_ec_cost", None)
            if last is not None:
                ec_seen.append(float(last))

        ups_parts: list[pd.DataFrame] = []
        warns: list[str] = []
        n_pulled = 0
        for s in symbols:
            entry = manifest["symbols"].get(s) or {}
            inst = entry.get("instrument") or s
            sub = acc[acc["symbol"] == s]
            if sub.empty:
                warns.append(f"{s}: no accumulated daily_basic rows, skipped")
                continue
            cap = getattr(args, "max_ec", None)
            if cap is not None and sum(ec_seen) >= cap:
                warns.append(
                    f"stopped at --max-ec {cap:g} (spent {sum(ec_seen):g} EC) "
                    f"after {n_pulled}/{len(symbols)} symbols; rerun with a higher "
                    "budget or a narrower --symbols list"
                )
                break
            begin = pd.to_datetime(sub["date"]).min().date().isoformat()
            frames, w, _ = fetch._daily_basic_frames(
                client, s, inst, config.PANEL_DAILY_BASIC_FIELDS, _meter, begin=begin
            )
            warns.extend(w)
            ups_parts.extend(frames)
            n_pulled += 1
        ups = (
            pd.concat(ups_parts, ignore_index=True)
            if ups_parts
            else pd.DataFrame(columns=["date", "symbol", "instrument", "field", "value"])
        )
        if not ups.empty:
            ups["value"] = ups["value"].astype(float)
        print(
            "[gate-b fetch]",
            json.dumps(
                {
                    "symbols_requested": len(symbols),
                    "symbols_pulled": n_pulled,
                    "ec_cost": sum(ec_seen),
                    "warnings": warns[:6],
                },
                ensure_ascii=False,
            ),
        )
    else:
        if not (args.accumulated and args.upstream):
            raise SystemExit("provide --accumulated/--upstream files, or --from-upstream --store")
        acc = _read(args.accumulated)
        ups = _read(args.upstream)
    report = reconcile.reconcile_daily_basic(acc, ups, tol=args.tol)
    print(json.dumps(report.as_meta(), ensure_ascii=False, indent=2))
    if not report.ok:
        raise SystemExit(
            f"Gate B FAILED: {report.n_mismatch}/{report.n_compared} overlap "
            f"values disagree beyond tol={args.tol} - investigate before use"
        )
    print(
        f"[gate-b] PASS: {report.n_compared} overlap values agree "
        f"(missing accumulated={report.n_missing_accumulated}, "
        f"missing upstream={report.n_missing_upstream})"
    )


def cmd_verify(args: argparse.Namespace) -> None:
    checks = []
    if args.check:
        for item in args.check:
            # instrument:field:date:value
            inst, field, date, value = item.split(":")
            checks.append((inst, field, date, float(value)))
    result = qlib_io.verify_bin(args.qlib_dir, checks)
    status = result["status"]
    verdict = "PASS" if result["ok"] is True else "UNAVAILABLE" if result["ok"] is None else "FAIL"
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
    h.add_argument("--n", type=int, default=DEFAULT_DUMP_N)
    h.add_argument(
        "--years",
        type=float,
        default=None,
        help="deep history: bars = years x 250 (one call, raised "
        "served-bar cap; LIVE EC extrapolates beyond 500 "
        "bars - probe pending; mutually exclusive with --n)",
    )
    h.add_argument("--freq", default="day")
    h.add_argument("--out", default="out/qlib_data")
    h.add_argument(
        "--workers",
        type=int,
        default=1,
        help="concurrent fetch workers (default 1 = serial); each owns a client",
    )
    h.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="hard EC ceiling; stop submitting symbols once reached (batch granularity)",
    )
    h.add_argument(
        "--resume", action="store_true", help="reuse cached fragments, skip already-fetched symbols"
    )
    h.add_argument(
        "--daily-basic",
        action="store_true",
        help="also fetch the upstream-gated daily_basic panel fields "
        "(pe_ttm/total_mv/circ_mv/turnover_rate/dv_ttm; requires "
        "the server table to be live; unavailable -> explicit "
        "skip warning, panel unchanged)",
    )
    h.add_argument(
        "--adjust",
        default=None,
        choices=list(config.ADJUSTMENT_MODES),
        help="price adjustment convention: auto = hfq_factor on "
        "--live (factor = hfq/raw, volume/factor; kline billed "
        "twice), qfq_const for demo (factor=1); explicit "
        "qfq_const on --live keeps the v1 single-pull path",
    )
    h.add_argument(
        "--dry-run",
        action="store_true",
        help="print the pre-flight EC estimate and exit (no store write, no request)",
    )
    h.add_argument("--store", default=None, help="fragment cache dir (default: <out>/_resume)")
    h.set_defaults(func=cmd_dump_history)

    u = sub.add_parser(
        "update-history",
        help="incremental update: fetch new bars only (live hfq tails), "
        "reconcile against stored history, re-dump the full .bin",
    )
    u.add_argument(
        "--symbols", nargs="*", default=None, help="default: every symbol in the fragment store"
    )
    u.add_argument("--live", action="store_true", help="required: incremental fetch is live-only")
    u.add_argument("--out", default="out/qlib_data")
    u.add_argument("--store", default=None, help="fragment cache dir (default: <out>/_resume)")
    u.add_argument("--workers", type=int, default=1)
    u.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="hard EC ceiling (REQUIRED); tails bill "
        "2x(156+67k) EC per symbol, k = gap days + overlap",
    )
    u.add_argument(
        "--adjust",
        default=None,
        choices=list(config.ADJUSTMENT_MODES),
        help="must resolve to hfq_factor (auto on --live); "
        "incremental appends need the immutable hfq basis",
    )
    u.add_argument(
        "--tail-overlap",
        type=int,
        default=5,
        help="extra bars requested beyond the gap to bridge "
        "non-trading days and force overlap reconciliation",
    )
    u.add_argument(
        "--tail-max-n",
        type=int,
        default=30,
        help="skip symbols whose gap needs more bars than this (bulk-refetch them instead)",
    )
    u.add_argument(
        "--valuation-days",
        type=int,
        default=7,
        help="refresh the 3y valuation series only when this many "
        "days elapsed (valuation dominates EC: ~712 EC per metric)",
    )
    u.add_argument(
        "--daily-basic",
        action="store_true",
        help="also fetch the daily_basic panel fields on the tail "
        "(cached_eval read; unavailable upstream -> skip warning)",
    )
    u.add_argument(
        "--dry-run",
        action="store_true",
        help="print the per-symbol tail plan + EC estimate and exit",
    )
    u.add_argument("--freq", default="day")
    u.set_defaults(func=cmd_update_history)

    c = sub.add_parser("dump-cross-section", help="emit today's score CSV")
    c.add_argument("--live", action="store_true")
    c.add_argument("--as-of", default=None)
    c.add_argument(
        "--via",
        default="screener",
        choices=["screener", "snapshot"],
        help="screener = full-market fundamentals composite "
        "(~204k EC full market; monthly cadence); snapshot = "
        "kline.snapshot technical composite over an explicit "
        "--symbols universe (~100 EC/票; the hs300-scale daily path)",
    )
    c.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="explicit universe for --via snapshot (>=3 equity codes)",
    )
    c.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="hard EC ceiling, checked post-hoc (a single request "
        "cannot be pre-stopped); REQUIRED for --live",
    )
    c.add_argument(
        "--idx",
        default=None,
        help="server index key to shrink the scanned universe "
        "(e.g. index.hs300 ~300 names ≈ 11k EC vs full market "
        "~204k EC); recommended for daily accumulation",
    )
    c.add_argument(
        "--dry-run", action="store_true", help="print the pre-flight EC estimate and exit"
    )
    c.add_argument("--out", default="out/score")
    c.set_defaults(func=cmd_dump_cross_section)

    a = sub.add_parser("accumulate", help="append a daily snapshot")
    a.add_argument("--live", action="store_true")
    a.add_argument("--as-of", default=None)
    a.add_argument(
        "--via",
        default="screener",
        choices=["screener", "snapshot"],
        help="see dump-cross-section; snapshot needs --symbols",
    )
    a.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="explicit universe for --via snapshot (>=3 equity codes)",
    )
    a.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="hard EC ceiling, checked post-hoc; REQUIRED for --live",
    )
    a.add_argument(
        "--idx", default=None, help="server index key to shrink the universe (e.g. index.hs300)"
    )
    a.add_argument(
        "--min-days",
        type=int,
        default=config.history_days_threshold(),
        help="cold-start guard for promotion (configurable): real "
        "snapshot days before accumulate auto-promotes to a "
        "backtestable panel; the PIT series itself is "
        "available from its first captured day regardless",
    )
    a.add_argument(
        "--dry-run", action="store_true", help="print the pre-flight EC estimate and exit"
    )
    a.add_argument("--out", default="out/cs_accum")
    a.set_defaults(func=cmd_accumulate)

    r = sub.add_parser(
        "reconcile-daily-basic",
        help="Gate B: compare layer-3 accumulated PIT values against the "
        "official daily_basic history over the overlap (plan v3 §5)",
    )
    r.add_argument(
        "--accumulated",
        default=None,
        help="long frame parquet/csv [date, instrument, field, value] (layer 3)",
    )
    r.add_argument(
        "--upstream",
        default=None,
        help="long frame parquet/csv [date, instrument, field, value] (daily_basic)",
    )
    r.add_argument(
        "--from-upstream",
        action="store_true",
        help="pull the official side via valuation.daily_basic over "
        "the accumulated window (P3-3; needs --store, live only)",
    )
    r.add_argument(
        "--store", default=None, help="resume store dir (with --from-upstream; layer-3 side)"
    )
    r.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="with --from-upstream: subset (default: whole store)",
    )
    r.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="budget ceiling for --from-upstream: stop pulling symbols once the "
        "metered spend reaches it (reads bill floor + ~23 EC/doc)",
    )
    r.add_argument(
        "--tol",
        type=float,
        default=1e-6,
        help="absolute tolerance on base-unit values (default 1e-6)",
    )
    r.set_defaults(func=cmd_reconcile_daily_basic)

    v = sub.add_parser("verify", help="read .bin back with Qlib and check values")
    v.add_argument("--qlib-dir", default="out/qlib_data")
    v.add_argument("--check", nargs="*", default=[])
    v.set_defaults(func=cmd_verify)

    w = sub.add_parser(
        "write-universe",
        help="write instruments/<name>.txt from index membership or a codes file",
    )
    w.add_argument("--qlib-dir", default="out/qlib_data")
    w.add_argument("--name", required=True, help="universe file name (e.g. csi300)")
    w.add_argument(
        "--index",
        default=None,
        help="live index key (e.g. index.hs300); requires --live; "
        "indices.members cost not yet probed",
    )
    w.add_argument(
        "--codes-file",
        default=None,
        help="one code per line (# comments ok): qlib-style "
        "SH600519 or bare 600519 (mapped via stocks.list)",
    )
    w.add_argument("--live", action="store_true")
    w.add_argument(
        "--exclude-st", action="store_true", help="drop ST symbols via a live snapshot hygiene pass"
    )
    w.add_argument(
        "--exclude-suspended", action="store_true", help="drop suspended symbols via the same pass"
    )
    w.add_argument(
        "--max-ec", type=float, default=None, help="REQUIRED with hygiene filtering (~100 EC/票)"
    )
    w.add_argument("--freq", default="day")
    w.set_defaults(func=cmd_write_universe)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
