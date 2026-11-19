# Contributing

Thanks for looking. This is a small adapter with a deliberately narrow job, so the
rules are mostly about not breaking the honesty contract.

## Quick start for contributors

```bash
git clone https://github.com/open-winmale/wmshare-qlib.git
cd wmshare-qlib
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
```

## Running tests

```bash
# Tier 1 — mandatory, offline, no credentials, no EC. This is what CI runs.
pytest tests -q -m "not live"

# Tier 2 — adds the Qlib read-back cross-check (needs pyqlib)
pip install -e '.[qlib,test]'
pytest tests -q -m qlib
```

Tier 1 uses `wmshare.demo_client()` — a fixed offline snapshot. It needs **no
credentials and spends no EC**, but it does need the `wmshare` package installed
(it is a hard dependency).

### Live tests (opt-in, spends real EC)

`tests/test_live.py` is marked `live` and is excluded from CI. It requires both a
real credential *and* an explicit opt-in:

```bash
export WMSHARE_CLIENT_ID=...
export WMSHARE_CLIENT_SECRET=...
export WMSHARE_QLIB_LIVE=1
pytest tests/test_live.py -q
```

Without `WMSHARE_QLIB_LIVE=1` it skips loudly rather than passing with no coverage.
Do not lower that gate, and do not let a live test run in CI — it would bill the
project owner's account.

## Lint

```bash
ruff check .
```

CI runs ruff 0.14.4 on every push. Configuration lives in `pyproject.toml`
(`line-length = 100`, `select = ["E","F","W","I"]`).

## What we will not merge

These are the project's red lines, not stylistic preferences:

1. **Fabricated history.** Never paste a current snapshot value across past dates to
   manufacture a series. If upstream has no history endpoint, the answer is "we do
   not have that series", not a plausible-looking series.
2. **Client-side recomputation of upstream metrics.** Re-deriving a precomputed
   metric from raw primitives and presenting it as a wmshare value cannot be
   verified against the server's formula.
3. **Unbudgeted billed paths.** Any new live path that can spend EC must take an
   explicit `--max-ec` and enforce it before submission.
4. **`qlib.init()` in the caller's process.** Qlib configuration is global state;
   the read-back runs in a child process precisely so importing this package cannot
   reconfigure someone's session.
5. **Silent degradation.** Missing data must be reported (a warning, a `null`, an
   explicit skip), never zero-filled.

## Pull requests

- One logical change per PR; keep the diff reviewable.
- Add or update tests. New behaviour with no test will be asked for one.
- If you touch the on-disk format, update `format.py` (the single source of truth)
  and the self-check it exposes.
- If your change affects cost, update `config.EC_*` anchors **with the measured
  number and the date**, and note it in the changelog.
- Update `CHANGELOG.md` under `[Unreleased]`.

## Reporting bugs

Include:

- `wmshare-qlib --version` (or `pip show wmshare-qlib`) and `pip show wmshare`
- the exact command, with credentials redacted
- whether you were in demo or live mode
- for live issues, the `metering` block from `fetch_meta.json` (it contains no
  credential)

## Upstream data requests

"Please add a history endpoint for precomputed metric codes" is an upstream data
question, not an adapter bug — those belong in the
[wmshare](https://github.com/open-winmale/wmshare) repository. This package cannot
invent a series the service does not serve.

## License

Contributions are accepted under BSD-3-Clause, the project's existing license.
