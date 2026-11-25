# Getting started

From zero to a backtestable Qlib `.bin` panel. The zero-credential path uses
wmshare's public-pool free trial (real data); production work adds credentials
for your own account.

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

## 3. First panel on the free trial (zero credentials)

With no credentials configured, every client is a **public-pool free-trial
client** (wmshare >=3.3): real data, no token, drawn from a quota **shared by
all unregistered callers of your network**. Start with a dry-run, then a tiny
panel:

```bash
# 3a. Preview the run scope (fires nothing)
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --dry-run

# 3b. History panel -> Qlib .bin
wmshare-qlib dump-history --symbols 600519 000858 --n 60 --max-ec 30000 --out ./out/qlib_data

# 3c. Read one value back (anchors are instrument:field:date:value; re-derive
#     the date from out/qlib_data/calendars/day.txt, last line)
wmshare-qlib verify --qlib-dir ./out/qlib_data --check SH600519:close:<date>:<close>
```

**How you know it was the free trial.** `out/qlib_data/fetch_meta.json` records
`metering.auth_mode == "public_pool"`, and the CLI prints a one-line
`[free-trial]` hint on stderr. Two knobs surround this mode:

- `--anonymous` (on every live command): force the free trial even when
  credentials exist.
- `WMSHARE_DISABLE_ANON=1` (env): forbid the fallback — missing credentials
  fail loud again instead of quietly using the shared pool.

**What you should see.** `out/qlib_data/` contains:

```
out/qlib_data/
├── calendars/day.txt              # one date per line, ascending
├── instruments/all.txt            # every dumped instrument (INST\tstart\tend)
├── features/
│   ├── sh600519/
│   │   ├── open.day.bin  high.day.bin  low.day.bin  close.day.bin
│   │   ├── volume.day.bin  amount.day.bin  factor.day.bin
│   │   ├── vwap.day.bin           # derived: amount / volume (dump-time)
│   │   ├── pe.day.bin  pb.day.bin  ps.day.bin  pr.day.bin
│   │   └── net_mf.day.bin elg_net.day.bin lg_net.day.bin md_net.day.bin sm_net.day.bin
│   └── sz000858/...
├── _resume/                        # fragment store: manifest + per-symbol parquet
└── fetch_meta.json                 # params, metering (+ auth_mode), warnings
```

`instruments/<name>.txt` for a named universe appears only after `write-universe`
(see step 6 below).

Qlib `.bin` layout: a little-endian `float32` array whose first element is the
start index into the global calendar, followed by that instrument's values
beginning at that position (`values[0]` is the value for
`calendar[start_index]`) and ending at the instrument's own last date. This is
Qlib's own format — its reader resolves calendar index `i` at byte offset
`4*(i-start_index)+4` — and the dumper is Qlib-free (`format.py` is the single
source of truth for the on-disk contract).

## 4. Free-trial boundaries (read before you rely on it)

The trial is real data with real limits — these are upstream terms, not bugs:

| Boundary | Behavior |
|---|---|
| Quota | **shared** across every unregistered caller of your network; not refilled like a private weekly balance |
| Rate refusals | `ip_rate_limited` (per-hour, per source network) and quota refusals are **terminal** — retrying does not help; the adapter never retries them |
| Pool suspended | `pool_unavailable` — wait, or configure credentials |
| IP banned | `ip_banned` — only real credentials bypass it |
| Who ran | always visible: `metering.auth_mode` + the `[free-trial]` stderr hint |

The trial is enough to prove the pipeline and pull a small panel. Anything
recurring wants real credentials.

## 5. Production: configure credentials

Credentials move you from the shared pool to your **own account**.

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
`wmshare.Client.from_env()`, so the SDK's rules apply verbatim. From then on
`fetch_meta.json` shows `metering.auth_mode == "credentials"`; use `--anonymous`
to step back onto the free trial for one run.

### 5b. First real panel

```bash
# Bulk load (builds the fragment store; qlib .bin dumped once all symbols land)
wmshare-qlib dump-history --symbols 600519 000858 \
  --n 60 --max-ec 30000 --out ./out/qlib_data

# Daily incremental after that
wmshare-qlib update-history --max-ec 20000 --out ./out/qlib_data
```

Two things that surprise people:

- **Stores dumped under an older convention are refused by `update-history`**
  (`store was not built under hfq_factor` — 0.6.x demo stores were `qfq_const`,
  and pre-`adjust` stores record no convention at all). Incremental appends need
  the immutable hfq basis; rebuild once with `dump-history`.
- **Both calls need `--max-ec`.** It is a budget ceiling: the run stops
  submitting work once the ceiling is reached. Without it the command exits
  before doing anything — that is intentional. If a run stops early with a
  ceiling message, raise the ceiling and re-run; `--resume` picks up where it
  left off.

Use `--dry-run` on any of `dump-history`, `update-history`, `dump-cross-section`
or `accumulate` to see the run scope without firing a request:

```bash
wmshare-qlib dump-history --symbols 600519 --n 60 --dry-run --max-ec 30000
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
SH600519    2026-11-17    2026-11-20
SZ000858    2026-11-17    2026-11-20
```

Codes may be bare (`600519`) or Qlib-style (`SH600519`); the exchange suffix is
mapped through `stocks.list`, never guessed from digit ranges. Symbols not present
in the dumped panel are dropped — if the file comes out shorter than your code
list, check that those symbols were dumped.

Two other sources exist: `--index index.hs300` (live only) and hygiene filters
`--exclude-st/--exclude-suspended` (live only, requires `--max-ec`).

## 7. Optional: verify with Qlib's own reader

```bash
pip install 'wmshare-qlib[qlib]'
wmshare-qlib verify --qlib-dir ./out/qlib_data \
  --check SH600519:close:2026-11-20:1485.75
```

Anchor values are whatever your dump fetched (re-derive the date from
`calendars/day.txt`); without `pyqlib` the command prints
`UNAVAILABLE (ok=None)` — explicitly **not** a pass. The dumper itself never imports Qlib; only this read-back path does, and it
runs in a child process so your `qlib.init()` state is untouched.

## 8. Optional: local automation (launchd, macOS)

Keep a small fragment store fresh without manual runs. The chain is
`update-history` (incremental tails) → `accumulate` (PIT snapshot) →
`accumulate-margin` → `tools/freshness.py` (offline verdict). Every live step
is soft-fail (warn + continue, each guarded by its own `--max-ec`);
freshness always runs last and its exit code is the chain's exit code
(`0` green / `1` corrupt / `2` stale).

```bash
# Manual run (output lands in out/logs/daily_<date>.log; VERBOSE=1 keeps stdout)
bash tools/daily_update.sh

# Install the launchd job (17:30 local time daily; replace __REPO__ yourself)
sed "s|__REPO__|/absolute/path/to/wmshare-qlib|g" \
  tools/com.wmshare.qlib-daily-update.plist \
  > ~/Library/LaunchAgents/com.wmshare.qlib-daily-update.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wmshare.qlib-daily-update.plist

# Trigger once by hand, inspect, then uninstall
launchctl kickstart -k gui/$(id -u)/com.wmshare.qlib-daily-update
launchctl print gui/$(id -u)/com.wmshare.qlib-daily-update | grep -E "state|exit"
launchctl bootout gui/$(id -u)/com.wmshare.qlib-daily-update
```

Notes:

- launchd does not load your shell profile; the plist and `daily_update.sh`
  resolve everything from absolute paths inside the repo (`PY=` selects the
  interpreter, `REPO=` the checkout). Credentials are unaffected: the SDK
  reads `~/.wmshare/credentials.env` directly, not shell env.
- The `MAX_EC_*` ceilings in `daily_update.sh` are sized for the default
  3-symbol store; widen `ACCUM_SYMBOLS` / `MARGIN_SYMBOLS` deliberately —
  every added symbol adds a daily read.
- Staleness is judged against weekdays only; CN holidays can trigger a
  spurious `stale` (exit 2) — by design a warn, never a corruption signal.
- Set `MARGIN_ACCUM=0` to skip margin accumulation (e.g. before it is
  provisioned).

## 9. Next

- [data-model.md](data-model.md) — the three availability tiers and why most of
  the metric catalog has no history.
- [faq.md](faq.md) — free-trial boundaries, hfq vs qfq, common errors.
- [../examples/](../examples/) — `dump_to_qlib.py`, `online_score.py`,
  `alpha158_lgbm.py`, `benchmark/`.
