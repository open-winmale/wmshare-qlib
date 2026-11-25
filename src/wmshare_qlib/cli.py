"""CLI: dump-history / update-history / dump-cross-section / accumulate /
write-universe / reconcile-daily-basic / verify.

Since 0.7.0 there is ONE data mode (live). Without credentials the client is
a public-pool free-trial client; ``--anonymous`` forces that mode even when
credentials exist. The offline demo snapshot was retired together with the
SDK's ``demo_client`` (removed in wmshare 3.0).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

from . import (
    __version__,
    config,
    cross_section,
    dump,
    fetch,
    margin_store,
    normalize,
    panel,
    qlib_io,
    resume,
)

DEFAULT_DUMP_N = 120


def _is_qlib_instrument(code: str) -> bool:
    """Whether ``code`` is already Qlib-style (SH/SZ/BJ + digits).

    Suffixes are never INFERRED from digit ranges (wrong for BJ and some SZ
    codes); this only recognizes an explicit suffix the caller already wrote.
    """
    c = str(code).upper()
    return c[:2] in {"SH", "SZ", "BJ"} and c[2:].isdigit()


def _today_iso() -> str:
    """Today as an ISO date (single seam for tests; nothing else reads the clock)."""
    return dt.date.today().isoformat()


def _free_trial_hint(metering: dict) -> None:
    """One stderr line when the run ran on the public-pool free trial.

    ``auth_mode`` records which client ran; the operator gets told once so an
    unintended no-credentials run is never silent.
    """
    if metering.get("auth_mode") == "public_pool":
        print(
            "[free-trial] no credentials configured: running on the public-pool "
            "FREE TRIAL; configure ~/.wmshare/credentials.env (client_id + "
            "api-key) to use your own account",
            file=sys.stderr,
        )


def _print_cross_section_preview(args: argparse.Namespace) -> None:
    """--dry-run: preview the run scope, no request fired."""
    via = getattr(args, "via", "screener")
    if via == "snapshot":
        if not args.symbols:
            raise SystemExit("--via snapshot requires --symbols (>=3 equity codes)")
        n = len(args.symbols)
    else:
        n = "full universe"
        idx = getattr(args, "idx", None)
        if idx:
            n = f"idx={idx} (scoped universe)"
    print(
        "[dry-run]",
        json.dumps(
            {
                "dry_run": True,
                "via": via,
                "mode": "live",
                "symbols": n,
                "max_ec": args.max_ec,
                "note": "scope preview only; --max-ec stops the run once "
                "reached (checked post-hoc on one-shot paths)",
            },
            ensure_ascii=False,
        ),
    )


def _dump_store(store: str, manifest: dict, out: str, freq: str) -> tuple[dict, dict]:
    """Load the merged store (bulk + tails), gate integrity, dump the .bin.

    Local IO only: zero requests. Shared by dump-history, update-history and
    its --dump-only migration path, so every .bin on disk is produced from the
    SAME merged source and passes the same integrity gate.
    Returns (integrity_report, dump_manifest).
    """
    full_panel = resume.load_panel_with_tail(store, manifest)
    integrity = panel.assert_panel_integrity(full_panel)
    print("[integrity]", json.dumps(integrity, ensure_ascii=False))
    if not integrity["ok"]:
        raise SystemExit("panel integrity failed; abort before dump")
    dump_manifest = dump.dump_panel(full_panel, out, freq=freq)
    print("[dump manifest]", json.dumps(dump_manifest, ensure_ascii=False)[:800])
    return integrity, dump_manifest


def cmd_dump_history(args: argparse.Namespace) -> None:
    symbols = args.symbols or panel.demo_universe()
    store = args.store or str(Path(args.out) / "_resume")
    try:
        adjust_mode = config.adjustment_mode(args.adjust)
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
        # Scope preview BEFORE any store write or request.
        preview_fields = list(config.HISTORY_FIELDS)
        if getattr(args, "daily_basic", False):
            preview_fields = preview_fields + config.PANEL_DAILY_BASIC_FIELDS
        print(
            "[dry-run]",
            json.dumps(
                {
                    "dry_run": True,
                    "mode": "live",
                    "adjust": adjust_mode,
                    "symbols": len(symbols),
                    "n": args.n,
                    "fields": preview_fields,
                    "daily_basic": bool(getattr(args, "daily_basic", False)),
                    "resume": bool(args.resume),
                    "store": store,
                    "out": args.out,
                    "max_ec": args.max_ec,
                    "note": "scope preview only; --max-ec is the budget ceiling - "
                    "the run stops submitting symbols once it is reached "
                    "(re-run with a higher ceiling to resume)",
                },
                ensure_ascii=False,
            ),
        )
        return
    if args.max_ec is None:
        raise SystemExit(
            "dump-history requires --max-ec (budget ceiling); use --dry-run to "
            "preview the run without firing anything. If the run stops early "
            "with a ceiling message, raise the ceiling - --resume picks up "
            "where it left off"
        )
    fields = config.HISTORY_FIELDS
    if getattr(args, "daily_basic", False):
        fields = fields + config.PANEL_DAILY_BASIC_FIELDS
    params = {
        "n": args.n,
        "fields": list(fields),
        "live": True,
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
        # so an interruption keeps already-fetched data.
        def _save(symbol, instrument, frames, _ec):
            nonlocal manifest
            manifest = resume.record_fragment(store, manifest, symbol, instrument, frames)

        result = fetch.fetch_history_panel(
            todo,
            n=args.n,
            client=None,
            anonymous=bool(getattr(args, "anonymous", False)),
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
        # Bookkeeping, NOT metering: requests=0 means "served from resume
        # cache, nothing was fetched THIS run".
        last_metering = {
            "mode": "live",
            "requests": 0,
            "auth_mode": "cache_hit",
            "note": "all symbols served from resume cache (bookkeeping, "
            "not a metering reading)",
        }

    print("[metering]", json.dumps(last_metering, ensure_ascii=False))
    _free_trial_hint(last_metering)
    cache_state = resume.summary(manifest)
    print("[cache-state]", json.dumps(cache_state, ensure_ascii=False))
    for w in all_warnings[:10]:
        print("[warn]", w)

    # Merge ALL cached fragments (old + new) plus any incremental tails, then
    # dump once with a single global calendar — per-symbol .bin would break
    # Qlib seek offsets. Reading the tails here matters: a --resume re-dump
    # that skipped them would silently regress the panel to the bulk frontier.
    integrity, dump_manifest = _dump_store(store, manifest, args.out, args.freq)
    Path(args.out, "fetch_meta.json").write_text(
        json.dumps(
            {
                "mode": "live",
                "adjust": adjust_mode,
                "format": dump_manifest.get("format"),
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
    FULL .bin from the merged store (only the fetch touches the network).

    ``--dump-only`` skips the fetch entirely: it re-dumps the merged store
    locally (zero requests), which is the migration path after an on-disk
    layout change and the quickest way to refresh .bin after a merge.

    hfq_factor-only by design: incremental appends need an immutable basis
    (qfq re-anchors at every ex-div event, so appending under qfq_const would
    silently corrupt the panel across corporate actions).
    """
    adjust_mode = config.adjustment_mode(args.adjust)
    if adjust_mode != "hfq_factor":
        raise SystemExit(
            "update-history requires hfq_factor: incremental appends need an "
            "immutable basis (qfq_const re-anchors at every ex-div event, so "
            "appended bars would not line up with stored history)"
        )
    dump_only = bool(getattr(args, "dump_only", False))
    if not dump_only and args.max_ec is None:
        raise SystemExit(
            "update-history requires --max-ec (budget ceiling); --dump-only "
            "fires nothing and needs no budget"
        )
    store = args.store or str(Path(args.out) / "_resume")
    manifest = resume.load_manifest(store)
    if not manifest.get("symbols"):
        raise SystemExit(f"no fragment store at {store}; run dump-history first")
    if manifest.get("params", {}).get("adjust") != "hfq_factor":
        raise SystemExit(
            "store was not built under hfq_factor (or predates the adjust "
            "param): tail rows cannot reconcile against an unknown convention; "
            "rebuild the store with dump-history first"
        )

    if dump_only:
        # Zero-request re-dump of the merged store: the migration path for an
        # on-disk layout change, and the direct way to refresh .bin right
        # after a tail merge without another fetch.
        metering_meta = {
            "mode": "live",
            "requests": 0,
            "auth_mode": "cache_hit",
            "note": "dump-only: merged store re-dumped locally; nothing was "
            "fetched (bookkeeping, not a metering reading)",
        }
        warnings: list[str] = []
        _integrity, _dm = _dump_store(store, manifest, args.out, args.freq)
        Path(args.out, "fetch_meta.json").write_text(
            json.dumps(
                {
                    "mode": "live",
                    "update": True,
                    "dump_only": True,
                    "adjust": adjust_mode,
                    "format": _dm.get("format"),
                    "metering": metering_meta,
                    "integrity": _integrity,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return

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
        # Local plan over the manifest, zero requests.
        today = dt.date.today()
        plan = []
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
            plan.append(
                {
                    "symbol": s,
                    "gap_days": gap,
                    "k": k,
                    "valuation_due": v_due,
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
                    "daily_basic": want_db,
                    "max_ec": args.max_ec,
                    "per_symbol": plan[:20],
                    "note": "scope preview only; --max-ec is the budget ceiling - "
                    "the run stops submitting symbols once it is reached "
                    "(re-run with a higher ceiling to resume)",
                },
                ensure_ascii=False,
            ),
        )
        return

    def _save_tail(symbol, instrument, frames, _ec):
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
            valuation_refreshed=refreshed,
            daily_basic_last_date=max(db_dates) if db_dates else None,
        )

    result = fetch.fetch_history_tail(
        symbols,
        last_dates=last_dates,
        valuation_last_dates={k: (v or None) for k, v in v_last.items()},
        daily_basic_last_dates=dict(db_last),
        overlap=args.tail_overlap,
        tail_max_n=args.tail_max_n,
        valuation_days=args.valuation_days,
        fields=fields,
        client=None,
        anonymous=bool(getattr(args, "anonymous", False)),
        max_workers=args.workers,
        max_ec=args.max_ec,
        on_symbol=_save_tail,
    )
    metering_meta = result.metering.as_meta()
    print("[metering]", json.dumps(metering_meta, ensure_ascii=False))
    _free_trial_hint(metering_meta)
    for w in result.warnings[:10]:
        print("[warn]", w)

    # Reconcile bulk+tail and FULL re-dump from the merged store (local IO).
    integrity, dump_manifest = _dump_store(store, manifest, args.out, args.freq)
    Path(args.out, "fetch_meta.json").write_text(
        json.dumps(
            {
                "mode": "live",
                "update": True,
                "adjust": adjust_mode,
                "format": dump_manifest.get("format"),
                "metering": metering_meta,
                "integrity": integrity,
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _fetch_cross_section_by_via(args: argparse.Namespace, *, as_of: str | None = None):
    """Dispatch the cross-section fetch by --via (P0-3).

    screener: full-market fundamentals composite (server scans everything) -
    the monthly cadence path.
    snapshot: client-side-subset technical composite via kline.snapshot over
    an explicit --symbols universe (the only path where the operator controls
    the scanned universe) - the hs300-scale daily cadence path.

    ``as_of`` overrides ``args.as_of`` (the accumulate path resolves its own
    default without mutating the parsed namespace).
    """
    day = args.as_of if as_of is None else as_of
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
                as_of=day,
                anonymous=bool(getattr(args, "anonymous", False)),
                max_ec=args.max_ec,
            )
        except normalize.NormalizeError as exc:
            raise SystemExit(str(exc))
    return fetch.fetch_cross_section(
        as_of=day,
        anonymous=bool(getattr(args, "anonymous", False)),
        max_ec=args.max_ec,
        idx=args.idx,
    )


def cmd_dump_cross_section(args: argparse.Namespace) -> None:
    if getattr(args, "dry_run", False):
        _print_cross_section_preview(args)
        return
    result = _fetch_cross_section_by_via(args)
    out = cross_section.write_score_csv(result, args.out, as_of=args.as_of or "snapshot")
    print("[cross-section]", out)
    print("[metering]", json.dumps(result.metering.as_meta(), ensure_ascii=False))
    _free_trial_hint(result.metering.as_meta())
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
        _print_cross_section_preview(args)
        return
    # P0-3 (2026-11-24): --as-of defaulted to None, so every run landed on
    # None.parquet -- one rolling file, available_from="None", and the PIT
    # promotion guard could never pass. The day is the unit of this store, so
    # default it to today and pass the SAME value to the fetch (its datetime
    # column), the file name and the history meta.
    as_of = args.as_of or _today_iso()
    result = _fetch_cross_section_by_via(args, as_of=as_of)
    _free_trial_hint(result.metering.as_meta())
    out = cross_section.accumulate(result.frame, args.out, as_of=as_of)
    print("[accumulate]", out)
    days, ignored = cross_section.scan_accumulation(args.out)
    if ignored:
        print(
            "[warn]",
            f"ignored {len(ignored)} non-date parquet file(s) in {args.out} "
            f"({[f.name for f in ignored[:3]]}) - not PIT days; they never "
            "count toward the promotion guard",
        )
    available_from = days[0].stem if days else None  # ISO stems sort chronologically
    meta = cross_section.history_meta(len(days), args.min_days, available_from=available_from)
    print("[history-meta]", json.dumps(meta, ensure_ascii=False))
    # P2-2: automatic promotion check - when the configurable cold-start guard
    # passes, promote the accumulated snapshots to a backtestable panel here
    # instead of leaving the operator to remember promote_to_panel exists.
    if len(days) >= args.min_days:
        panel_out = Path(args.out).with_name(Path(args.out).name + "_panel.parquet")
        _frame, status = cross_section.promote_to_panel(
            args.out, min_days=args.min_days, out_parquet=panel_out
        )
        print("[promote]", status)


def cmd_accumulate_margin(args: argparse.Namespace) -> None:
    """Layer-3 margin accumulation: append today's real margin rows (PIT).

    Default path: one ``margin.latest`` per symbol. ``--catchup K``: the ONLY
    sanctioned backfill path - an explicit ``margin.query(limit=K+2)`` history
    read (1-2 missed days are better served by the daily snapshot runs).
    Forward-only otherwise; rows are never fabricated or interpolated.
    """
    if getattr(args, "dry_run", False):
        syms = args.symbols or []
        if not syms:
            raise SystemExit("accumulate-margin requires --symbols")
        print(
            "[dry-run]",
            json.dumps(
                {
                    "dry_run": True,
                    "mode": "live",
                    "symbols": len(syms),
                    "catchup_days": args.catchup,
                    "max_ec": args.max_ec,
                    "note": "scope preview only; --max-ec is the budget ceiling - "
                    "checked post-hoc on this one-shot path",
                },
                ensure_ascii=False,
            ),
        )
        return
    if args.max_ec is None:
        raise SystemExit(
            "--max-ec is required (budget ceiling); declare it before firing"
        )
    if not args.symbols:
        raise SystemExit(
            "accumulate-margin requires --symbols (the margin universe is a "
            "~4k-name subset; every added symbol bills every day - widen deliberately)"
        )
    anonymous = bool(getattr(args, "anonymous", False))
    results = []
    if args.catchup:
        for sym in args.symbols:
            results.append(
                fetch.fetch_margin_history(
                    sym, args.catchup, max_ec=args.max_ec, anonymous=anonymous
                )
            )
    else:
        results.append(
            fetch.fetch_margin_snapshot(
                args.symbols, max_ec=args.max_ec, anonymous=anonymous
            )
        )
    _free_trial_hint(results[0].metering.as_meta())
    frame = pd.concat([r.frame for r in results], ignore_index=True)
    paths = margin_store.save_margin_days(frame, args.out)
    _panel, meta = margin_store.load_margin_panel(args.out)
    margin_store.write_meta(args.out, meta)
    total_spent = sum(m.ec_cost or 0.0 for m in (r.metering for r in results))
    total_req = sum(m.requests for m in (r.metering for r in results))
    print("[accumulate-margin]", ", ".join(p.name for p in paths))
    print(
        "[metering]",
        json.dumps(
            {
                **results[0].metering.as_meta(),
                "requests": total_req,
            },
            ensure_ascii=False,
        ),
    )
    print("[margin-meta]", json.dumps(meta, ensure_ascii=False))
    for r in results:
        for w in r.warnings:
            print("[warn]", w)
    if total_spent > args.max_ec:
        raise SystemExit(
            f"margin accumulation budget ceiling {args.max_ec} exceeded; raise "
            "--max-ec or narrow --symbols"
        )


def cmd_write_universe(args: argparse.Namespace) -> None:
    """P1-1: write instruments/<name>.txt (Qlib convention) so
    ``D.instruments("<name>")`` works directly.

    Sources: ``--index <key>`` (live, real membership via indices.members) or
    ``--codes-file`` (one code per line; qlib-style SH600519 passes through,
    bare 600519 maps via stocks.list - never inferred from digit ranges).
    Optional ST/suspension hygiene pre-pass (live snapshot). The universe is
    INTERSECTED with the dumped panel's actual data coverage - a universe file
    can only make instruments backtestable that the dump contains.
    """
    qlib_dir = Path(args.qlib_dir)
    if not (qlib_dir / "instruments" / "all.txt").exists():
        raise SystemExit(
            f"no dumped panel at {qlib_dir} (missing instruments/all.txt); run dump-history first"
        )
    codes: list[str] = []
    history_sets: dict[str, set[str]] | None = None
    if not args.index and not args.codes_file:
        raise SystemExit("one of --index <key> or --codes-file <path> is required")
    if args.index and args.max_ec is None:
        raise SystemExit(
            "--index requires --max-ec (budget ceiling); declare it before firing"
        )
    # hygiene guards BEFORE any client creation: the CLI must fail fast on
    # argument mistakes without building a client or firing anything (guards
    # stay pre-network even though a missing-credentials client no longer
    # raises - it would silently select the public-pool free trial)
    if args.exclude_st or args.exclude_suspended:
        if args.max_ec is None:
            raise SystemExit("hygiene filtering requires --max-ec (budget ceiling)")

    if args.codes_file:
        codes = [
            ln.strip()
            for ln in Path(args.codes_file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    bare_codes = [c for c in codes if not _is_qlib_instrument(c)]
    # stocks.list is a live upstream read: only run the mapping when a bare
    # code actually needs it. An all-qlib-style codes file with no hygiene
    # pass is a zero-request run.
    needs_map = bool(args.index) or bool(bare_codes) or bool(
        args.exclude_st or args.exclude_suspended
    )
    if needs_map and args.max_ec is None:
        raise SystemExit(
            "this universe needs a stocks.list mapping (bare code -> exchange "
            "suffix). Pass --max-ec, or use SH600519-style codes with "
            "--codes-file for a zero-request run"
        )

    client = None
    instrument_map: dict[str, str] = {}
    spent = 0.0
    if needs_map:
        client = fetch._client(anonymous=bool(getattr(args, "anonymous", False)))
        instrument_map = normalize.build_instrument_map(client)
        map_ec = getattr(client, "last_ec_cost", None)
        if map_ec is not None:
            spent += float(map_ec)
        print(
            "[metering]",
            json.dumps(
                {
                    "stage": "universe mapping (stocks.list)",
                    "requests": 1,
                    "auth_mode": fetch._auth_mode(client),
                },
                ensure_ascii=False,
            ),
        )
        _free_trial_hint({"auth_mode": fetch._auth_mode(client)})

    if args.index:
        # PIT episodes preferred: the upstream materialized table accumulates one
        # membership snapshot per day (forward-PIT; earlier history does not
        # exist upstream). SDK lacking index.members_history -> fall back to the
        # current-members single-snapshot path below (behavior unchanged).
        hist_fn = None
        idx_api = getattr(client, "index", None)
        if idx_api is not None:
            hist_fn = getattr(idx_api, "members_history", None)
        envelope = None
        if hist_fn is not None:
            try:
                envelope = hist_fn(args.index)
            except Exception as exc:  # noqa: BLE001 - degrade loudly, keep working
                print(
                    "[warn]",
                    f"members_history failed ({type(exc).__name__}: {str(exc)[:80]}); "
                    "falling back to current-members snapshot (episode boundaries "
                    "degrade to this run's date)",
                )
                envelope = None
        if (
            isinstance(envelope, dict)
            and envelope.get("ok")
            and isinstance(envelope.get("data"), list)
            and envelope["data"]
        ):
            history_sets = {}
            for row in envelope["data"]:
                day = str(row.get("date") or "")[:10]
                sym = str(row.get("symbol") or "").strip()
                if day and sym:
                    history_sets.setdefault(day, set()).add(sym)
            if not history_sets:
                history_sets = None
            else:
                print(
                    "[universe-history]",
                    json.dumps(
                        {
                            "days": len(history_sets),
                            "as_of": str(envelope.get("as_of") or ""),
                            "note": "PIT episodes; earlier history does not exist "
                            "upstream (forward-PIT only)",
                        },
                        ensure_ascii=False,
                    ),
                )
        if history_sets is None:
            result = fetch.fetch_index_members(client, args.index)
            codes = result.frame["symbol"].astype(str).tolist()
            print("[metering]", json.dumps(result.metering.as_meta(), ensure_ascii=False))
            _free_trial_hint(result.metering.as_meta())
            for w in result.warnings[:5]:
                print("[warn]", w)
        # --max-ec is a CEILING, not an entry ticket: the index read (and the
        # mapping that just preceded it) must actually fit inside it.
        index_ec = getattr(client, "last_ec_cost", None)
        measured = spent + (float(index_ec) if index_ec is not None else 0.0)
        if args.max_ec is not None and measured > args.max_ec:
            raise SystemExit(
                f"universe budget ceiling {args.max_ec} exceeded: index pull + "
                "mapping together do not fit; raise --max-ec or narrow the source"
            )
    if not codes and history_sets is None:
        raise SystemExit("universe source produced no codes")

    # normalize codes -> qlib instruments (explicit mapping; no digit-range guesses)
    instruments: list[str] = []
    unmapped: list[str] = []
    for code in codes:
        c = code.upper()
        if _is_qlib_instrument(c):
            instruments.append(c)  # already qlib-style
        elif code in instrument_map:
            instruments.append(instrument_map[code])
        else:
            unmapped.append(code)
    if unmapped:
        print("[warn]", f"{len(unmapped)} code(s) unmapped (dropped): {unmapped[:5]}")

    if history_sets is not None:
        # PIT episode path: fold membership history into per-instrument episodes.
        # Hygiene flags are deliberately NOT applied here - filtering by TODAY's
        # ST/suspension state would rewrite history (survivorship bias).
        if args.exclude_st or args.exclude_suspended:
            print(
                "[warn]",
                "--exclude-st/--exclude-suspended ignored on the PIT history path: "
                "filtering by today's state would rewrite membership history",
            )
        hist_inst: dict[str, set[str]] = {}
        unmapped_hist: set[str] = set()
        for day, members in history_sets.items():
            kept: set[str] = set()
            for code in members:
                if _is_qlib_instrument(code):
                    kept.add(code.upper())
                elif code in instrument_map:
                    kept.add(instrument_map[code])
                else:
                    unmapped_hist.add(code)
            hist_inst[day] = kept
        if unmapped_hist:
            print(
                "[warn]",
                f"{len(unmapped_hist)} historical code(s) unmapped (dropped): "
                f"{sorted(unmapped_hist)[:5]}",
            )
        episodes = dump.universe_episodes_from_history(hist_inst)
        ranges = dump.instrument_date_ranges(qlib_dir, freq=args.freq)
        covered_eps = {inst: eps for inst, eps in episodes.items() if inst in ranges}
        missing_eps = sorted(set(episodes) - set(ranges))
        if missing_eps:
            print(
                "[warn]",
                f"{len(missing_eps)} episode instrument(s) not in the dumped panel "
                f"(dropped, e.g. {missing_eps[:5]})",
            )
        early = {
            inst: eps[0][0]
            for inst, eps in covered_eps.items()
            if eps[0][0] < ranges[inst][0]
        }
        if early:
            print(
                "[warn]",
                f"{len(early)} instrument(s) have membership episodes starting before "
                f"the dumped data (e.g. {list(early.items())[:3]}); Qlib reads NaN "
                "outside data coverage - clip by narrowing the dump window if needed",
            )
        if not covered_eps:
            raise SystemExit("no universe member has dumped data; nothing to write")
        out = dump.write_universe_episodes_file(qlib_dir, args.name, covered_eps)
        print(
            "[universe]",
            json.dumps(
                {
                    "path": str(out),
                    "name": args.name,
                    "written": len(covered_eps),
                    "episodes": sum(len(v) for v in covered_eps.values()),
                    "source": "index-history",
                    "hygiene": False,
                },
                ensure_ascii=False,
            ),
        )
        return

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
        _free_trial_hint(hyg.metering.as_meta())
        for w in hyg.warnings[:5]:
            print("[warn]", w)
        if hyg.metering.ec_cost is not None:
            measured = spent + float(hyg.metering.ec_cost)
            if args.max_ec is not None and measured > args.max_ec:
                raise SystemExit(
                    f"universe budget ceiling {args.max_ec} exceeded: mapping + "
                    "hygiene together do not fit; raise --max-ec"
                )

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
        window (windowed read) - no hand-fed files, the gate can run on a
        cadence instead of gathering dust.
    """
    import pandas as pd

    from . import config, fetch, reconcile, resume

    def _read(path: str) -> pd.DataFrame:
        p = Path(path)
        if p.suffix == ".parquet":
            return pd.read_parquet(p)
        return pd.read_csv(p)

    incomplete: str | None = None
    if getattr(args, "from_upstream", False):
        if not args.store:
            raise SystemExit("--from-upstream requires --store (resume store dir)")
        if getattr(args, "max_ec", None) is None:
            raise SystemExit(
                "--from-upstream requires --max-ec (budget ceiling for the "
                "official daily_basic reads)"
            )
        manifest = resume.load_manifest(args.store)
        symbols = args.symbols or sorted(manifest["symbols"].keys())
        full = resume.load_panel_with_tail(args.store, manifest)
        acc = full[full["field"].isin(config.PANEL_DAILY_BASIC_FIELDS)].copy()
        if acc.empty:
            raise SystemExit(
                "accumulated store has no daily_basic fields; run "
                "update-history --daily-basic first (Gate B audits that side)"
            )
        client = fetch._client(anonymous=bool(getattr(args, "anonymous", False)))
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
                    f"stopped at --max-ec {cap:g} after {n_pulled}/{len(symbols)} "
                    "symbols; rerun with a higher ceiling or a narrower "
                    "--symbols list"
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
                    "warnings": warns[:6],
                },
                ensure_ascii=False,
            ),
        )
        if n_pulled < len(symbols):
            incomplete = (
                f"only {n_pulled}/{len(symbols)} symbol(s) contributed an upstream "
                f"pull (budget cap {args.max_ec:g} or no accumulated rows)"
            )
    else:
        if not (args.accumulated and args.upstream):
            raise SystemExit("provide --accumulated/--upstream files, or --from-upstream --store")
        acc = _read(args.accumulated)
        ups = _read(args.upstream)
    report = reconcile.reconcile_daily_basic(acc, ups, tol=args.tol)
    print(json.dumps(report.as_meta(), ensure_ascii=False, indent=2))
    if report.n_compared == 0:
        # zero comparisons is absence of evidence, never agreement
        raise SystemExit(
            "Gate B FAILED: no overlapping (date, instrument, field) rows - an "
            "empty comparison certifies nothing "
            f"(missing accumulated={report.n_missing_accumulated}, "
            f"missing upstream={report.n_missing_upstream})"
        )
    if not report.ok:
        raise SystemExit(
            f"Gate B FAILED: {report.n_mismatch}/{report.n_compared} overlap "
            f"values disagree beyond tol={args.tol} - investigate before use"
        )
    if incomplete:
        raise SystemExit(
            f"Gate B INCOMPLETE: {incomplete}; a partial comparison cannot "
            "print PASS - raise --max-ec or narrow --symbols"
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
    p.add_argument("--version", action="version", version=f"wmshare-qlib {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("dump-history", help="fetch history and dump Qlib .bin")
    h.add_argument("--symbols", nargs="*", default=None)
    # 0.7.0 migration shim: live is the ONLY mode now; the flag still parses
    # (so old scripts do not die on unrecognized args) and main() prints a
    # one-time note instead of silently ignoring muscle memory.
    h.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    h.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    h.add_argument("--n", type=int, default=DEFAULT_DUMP_N)
    h.add_argument(
        "--years",
        type=float,
        default=None,
        help="deep history: bars = years x 250 (one call, raised "
        "served-bar cap; mutually exclusive with --n)",
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
        help="budget ceiling (REQUIRED): stop submitting symbols "
        "once reached (batch granularity); re-run with a higher "
        "ceiling to resume",
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
        help="price adjustment convention: auto = hfq_factor "
        "(factor = hfq/raw, volume/factor; two kline calls per "
        "window); explicit qfq_const keeps the v1 single-pull "
        "path (factor=1, re-anchors at every corporate action)",
    )
    h.add_argument(
        "--dry-run",
        action="store_true",
        help="print the run scope and exit (no store write, no request)",
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
    u.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    u.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    u.add_argument("--out", default="out/qlib_data")
    u.add_argument("--store", default=None, help="fragment cache dir (default: <out>/_resume)")
    u.add_argument("--workers", type=int, default=1)
    u.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="budget ceiling (REQUIRED unless --dump-only): stop "
        "submitting symbols once reached; re-run with a higher "
        "ceiling to resume",
    )
    u.add_argument(
        "--dump-only",
        action="store_true",
        help="re-dump the merged store (bulk + tails) to .bin without fetching "
        "- zero requests; the migration path after an on-disk "
        "layout change",
    )
    u.add_argument(
        "--adjust",
        default=None,
        choices=list(config.ADJUSTMENT_MODES),
        help="must resolve to hfq_factor (the default); "
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
        help="refresh the valuation series only when this many days elapsed",
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
        help="print the per-symbol tail plan and exit",
    )
    u.add_argument("--freq", default="day")
    u.set_defaults(func=cmd_update_history)

    c = sub.add_parser("dump-cross-section", help="emit today's score CSV")
    c.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    c.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    c.add_argument("--as-of", default=None)
    c.add_argument(
        "--via",
        default="screener",
        choices=["screener", "snapshot"],
        help="screener = full-market fundamentals composite (server "
        "scans everything; monthly cadence); snapshot = "
        "kline.snapshot technical composite over an explicit "
        "--symbols universe (the hs300-scale daily path)",
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
        help="budget ceiling, checked post-hoc (a single request "
        "cannot be pre-stopped); REQUIRED",
    )
    c.add_argument(
        "--idx",
        default=None,
        help="server index key to shrink the scanned universe "
        "(e.g. index.hs300 ~300 names); recommended for daily accumulation",
    )
    c.add_argument(
        "--dry-run", action="store_true", help="print the run scope and exit"
    )
    c.add_argument("--out", default="out/score")
    c.set_defaults(func=cmd_dump_cross_section)

    a = sub.add_parser("accumulate", help="append a daily snapshot")
    a.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    a.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    a.add_argument(
        "--as-of",
        default=None,
        help="snapshot date (ISO YYYY-MM-DD); default: today. This date is the "
        "PIT unit: one file per day, never a rolling overwrite",
    )
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
        help="budget ceiling, checked post-hoc; REQUIRED",
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
        "--dry-run", action="store_true", help="print the run scope and exit"
    )
    a.add_argument("--out", default="out/cs_accum")
    a.set_defaults(func=cmd_accumulate)

    am = sub.add_parser(
        "accumulate-margin",
        help="append a daily margin (两融) snapshot (Layer-3 forward-only PIT)",
    )
    am.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    am.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    am.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="margin-universe equity codes (keep tiny: every added "
        "symbol adds a daily read)",
    )
    am.add_argument(
        "--catchup",
        type=int,
        default=0,
        help="heal K missed days via margin.query(limit=K+2) - the only "
        "sanctioned backfill path; 1-2 missed days are better served "
        "by the daily snapshot runs",
    )
    am.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="budget ceiling, checked post-hoc; REQUIRED",
    )
    am.add_argument(
        "--dry-run", action="store_true", help="print the run scope and exit"
    )
    am.add_argument("--out", default="out/margin_accum")
    am.set_defaults(func=cmd_accumulate_margin)

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
        "metered spend reaches it",
    )
    r.add_argument(
        "--tol",
        type=float,
        default=1e-6,
        help="absolute tolerance on base-unit values (default 1e-6)",
    )
    r.add_argument(
        "--anonymous",
        action="store_true",
        help="with --from-upstream: force the public-pool free trial "
        "(ignore configured credentials)",
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
        help="live index key (e.g. index.hs300)",
    )
    w.add_argument(
        "--codes-file",
        default=None,
        help="one code per line (# comments ok): qlib-style "
        "SH600519 or bare 600519 (mapped via stocks.list)",
    )
    w.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    w.add_argument(
        "--anonymous",
        action="store_true",
        help="force the public-pool free trial (ignore configured credentials)",
    )
    w.add_argument(
        "--exclude-st", action="store_true", help="drop ST symbols via a live snapshot hygiene pass"
    )
    w.add_argument(
        "--exclude-suspended", action="store_true", help="drop suspended symbols via the same pass"
    )
    w.add_argument(
        "--max-ec",
        type=float,
        default=None,
        help="REQUIRED with hygiene filtering or bare-code mapping (budget ceiling)",
    )
    w.add_argument("--freq", default="day")
    w.set_defaults(func=cmd_write_universe)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if getattr(args, "live", False):
        # 0.7.0 migration shim: the flag parses so old scripts keep running,
        # but live is the only data mode - say so once instead of pretending.
        print(
            "[note] --live is a no-op since 0.7.0: live is the only data mode "
            "(without credentials the client is the public-pool free trial)"
        )
    args.func(args)


if __name__ == "__main__":
    main()
