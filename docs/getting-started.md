# Getting started

From zero to a backtestable Qlib `.bin` panel. About five minutes on the demo
path (no credentials, no cost), a little longer on the live path.

## 1. Requirements

- **Python 3.11** — the only supported interpreter (3.12+ is refused at install
  time: pyqlib ships no wheels for it)
- Optional but recommended: `pyqlib` (only needed to read `.bin` back with Qlib's
  own reader and to run the Alpha158 example)

## 2. Install

```bash
pip install wmshare-qlib
```

With the optional Qlib verification backend:

```bash
pip install 'wmshare-qlib[qlib]'
```

From source (current `main`):

```bash
git clone https://github.com/open-winmale/wmshare-qlib.git
cd wmshare-qlib
pip install -e '.[qlib]'
```

> `pyqlib` 0.9.x is built against numpy 1.x. The `qlib` extra pins `numpy<2` for
> that reason; installing `pyqlib` by hand into a numpy-2 environment can resolve
> to a broken binary.

## 3. Run the demo path first (zero credentials, zero EC)

The adapter defaults to **demo mode**: a fixed offline snapshot served by
`wmshare.demo_client()`. Nothing is billed, nothing hits the network. This is the
fastest way to confirm the install works.

```bash
# 3a. History panel -> Qlib .bin
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --out ./out/qlib_data

# 3b. Read one value back (anchors are instrument:field:date:value)
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-11-16:1453.9

# 3c. Today's cross section -> score CSV (ScoreFileModel input)
wmshare-qlib dump-cross-section --as-of 2026-11-16 --out ./out/score

# 3d. Accumulate one daily PIT snapshot
wmshare-qlib accumulate --as-of 2026-11-16 --out ./out/cs_accum
```

**About the anchors in 3b.** `2026-11-16` and `1453.9` are values from the SDK's
bundled demo snapshot, not market facts. They move when the SDK re-captures the
snapshot. If 3b reports `FAIL`, re-derive the date from
`out/qlib_data/calendars/day.txt` (last line) instead of assuming the install is
broken.

**What you should see.** `out/qlib_data/` contains (verified on a real demo run):

```
out/qlib_data/
├── calendars/day.txt              # one date per line, ascending
├── instruments/all.txt            # every dumped instrument (INST\tstart\tend)
├── features/
│   ├── sh600519/
│   │   ├── open.day.bin  high.day.bin  low.day.bin  close.day.bin
│   │   ├── volume.day.bin  amount.day.bin  factor.day.bin
│   │   ├── vwap.day.bin           # derived: amount / volume (dump-time)
│   │   ├── pe.day.bin             # demo has pe only; pb/ps/pr are skipped
│   │   └── net_mf.day.bin elg_net.day.bin lg_net.day.bin md_net.day.bin sm_net.day.bin
│   └── sz000858/...
├── _resume/                        # fragment store: manifest + per-symbol parquet
└── fetch_meta.json                 # params, EC metering, warnings
```

`instruments/<name>.txt` for a named universe appears only after `write-universe`
(see step 6 below).

Qlib `.bin` layout: a little-endian `float32` array whose first element is the
start index into the global calendar, followed by that instrument's values. This
is Qlib's own format — the dumper is Qlib-free (`format.py` is the single source
of truth for the on-disk contract).

## 4. Demo boundaries (read before you point it at your own universe)

The demo snapshot is deliberately small and will fail loudly outside its bounds —
that is by design, not a bug:

| Boundary | Limit | What happens past it |
|---|---|---|
| Universe | 10 A-share codes | `symbols outside universe` |
| K-line depth | 120 bars | shorter panels |
| Moneyflow depth | 30 bars | shorter panels |
| Valuation fields | `pe` only | `pb/ps/pr` skipped with a warning |
| Adjustment mode | `qfq_const` (factor = 1.0) | `hfq_factor requires --live` |
| `update-history` | live-only | `use dump-history for the full demo path` |
| Concurrency | forced to 1 worker | demo client is not thread-safe |

Demo coverage is enough for a pipeline proof, not for research conclusions.

## 5. Switch to live

Live mode reaches the real wmshare service. It needs **credentials** and it
**costs EC** (wmshare's compute credit).

### 5a. Get credentials

1. Open <https://open.winmale.com> and sign in.
2. Go to 我的 (My account) → 开放平台 (Open platform) → 我的应用 → 查看应用信息.
3. Copy **both** `client_id` and `api-key`.

Put them in the environment (the names are exact — `AppID`/`API Key` in prose
maps to these two variables):

```bash
export WMSHARE_CLIENT_ID=your_client_id
export WMSHARE_CLIENT_SECRET=your_api_key
```

Or persist them in a file the SDK reads automatically:

```bash
mkdir -p ~/.wmshare
cat > ~/.wmshare/credentials.env <<'EOF'
WMSHARE_CLIENT_ID=your_client_id
WMSHARE_CLIENT_SECRET=your_api_key
EOF
chmod 600 ~/.wmshare/credentials.env
```

`wmshare-qlib` never reads these files itself — it calls
`wmshare.Client.from_env()`, so the SDK's rules apply verbatim.

### 5b. Understand EC before you fire a live command

EC is wmshare's billing unit. A free tier exists and is replenished weekly, but
**every live command in this adapter is required to declare a budget** so you
cannot burn it accidentally. See [data-model.md](data-model.md#ec-costs) for the
cost model and [faq.md](faq.md) for "why is this so expensive".

### 5c. First live panel

```bash
# Bulk load (builds the fragment store; qlib .bin dumped once all symbols land)
wmshare-qlib dump-history --live --symbols 600519 000858 \
  --n 60 --max-ec 20000 --out ./out/qlib_data

# Daily incremental after that
wmshare-qlib update-history --live --max-ec 20000 --out ./out/qlib_data
```

Two things that surprise people:

- **You cannot go from a demo store to a live one.** Demo builds the store under
  `qfq_const`; incremental appends need an immutable basis, so `update-history`
  refuses (`store was not built under hfq_factor`). Rebuild with
  `dump-history --live` first.
- **Both calls need `--max-ec`.** Without it the command exits before spending
  anything. That is intentional.

Use `--dry-run` on any of `dump-history`, `update-history`, `dump-cross-section`
or `accumulate` to see the estimate without firing a request:

```bash
wmshare-qlib dump-history --live --symbols 600519 --n 60 --dry-run --max-ec 20000
```

## 6. Optional: named instrument universe

`dump-history` writes `instruments/all.txt`. To get a named universe that
`D.instruments("<name>")` can address:

```bash
printf '600519\n000858\n' > codes.txt
wmshare-qlib write-universe --qlib-dir ./out/qlib_data \
  --name csi300 --codes-file codes.txt
```

Output (verified):

```
[universe] {"path": "out/qlib_data/instruments/csi300.txt", "name": "csi300",
            "written": 2, "source": "codes-file", "hygiene": false}
```

```
SH600519    2023-11-20    2026-11-16
SZ000858    2023-11-20    2026-11-16
```

Codes may be bare (`600519`) or Qlib-style (`SH600519`); the exchange suffix is
mapped through `stocks.list`, never guessed from digit ranges. Symbols not present
in the dumped panel are dropped — if the file comes out shorter than your code
list, check that those symbols were dumped.

Two other sources exist: `--index index.hs300` (live only; its EC cost is not yet
probed) and hygiene filters `--exclude-st/--exclude-suspended` (live only, requires
`--max-ec`, ≈100 EC/票).

## 7. Optional: verify with Qlib's own reader

```bash
pip install 'wmshare-qlib[qlib]'
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-11-16:1453.9
```

Without `pyqlib` the command prints `UNAVAILABLE (ok=None)` — explicitly **not** a
pass. The dumper itself never imports Qlib; only this read-back path does, and it
runs in a child process so your `qlib.init()` state is untouched.

## 8. Next

- [data-model.md](data-model.md) — the three availability tiers, why most of the
  metric catalog has no history, and where the EC goes.
- [faq.md](faq.md) — demo boundaries, hfq vs qfq, common errors.
- [../examples/](../examples/) — `dump_to_qlib.py`, `online_score.py`,
  `alpha158_lgbm.py`, `benchmark/`.
