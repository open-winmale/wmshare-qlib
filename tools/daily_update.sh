#!/usr/bin/env bash
# Daily local update chain for the wmshare-qlib fragment store:
#   update-history -> accumulate -> accumulate-margin -> freshness
#
# Semantics:
# - Every live step fails SOFT (warn + continue); a transient upstream
#   outage must not take the whole chain down.
# - freshness runs last, unconditionally, and its exit code IS the script's
#   exit code (0 green / 1 corrupt / 2 stale) so launchd/cron ecosystems and
#   human operators see one honest number.
# - Output goes to out/logs/daily_<date>.log by default (automation mode);
#   run with VERBOSE=1 to keep stdout on the terminal (manual debugging).
#
# Overridable via environment (see defaults below). Every live step is
# guarded by an explicit --max-ec; the defaults are sized for the default
# 3-symbol sample store — widen ACCUM_SYMBOLS/MARGIN_SYMBOLS deliberately.

set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${PY:-/opt/anaconda3/envs/python3.11/bin/python}"
STORE="${STORE:-$REPO/out/_resume_live}"
OUT="${OUT:-$REPO/out/qlib_data_live}"
ACCUM_OUT="${ACCUM_OUT:-$REPO/out/cs_accum}"
MARGIN_OUT="${MARGIN_OUT:-$REPO/out/margin_accum}"
# PIT snapshot / margin accumulation sample (keep tiny; widen deliberately).
ACCUM_SYMBOLS="${ACCUM_SYMBOLS:-600519 000858 601318}"
MARGIN_SYMBOLS="${MARGIN_SYMBOLS:-600519 000858 601318}"
MARGIN_ACCUM="${MARGIN_ACCUM:-1}"   # set to 0 to skip margin accumulation
MAX_EC_UPDATE="${MAX_EC_UPDATE:-15000}"
MAX_EC_ACCUM="${MAX_EC_ACCUM:-2000}"

LOGDIR="$REPO/out/logs"
mkdir -p "$LOGDIR" "$MARGIN_OUT"
LOG="$LOGDIR/daily_$(date +%F).log"
if [ "${VERBOSE:-0}" != "1" ]; then
  exec >>"$LOG" 2>&1
fi
export PYTHONPATH="$REPO/src"

echo "[$(date '+%F %T')] === daily update start (store=$STORE out=$OUT) ==="

step() {
  local name="$1"; shift
  echo "[$(date '+%F %T')] --- $name ---"
  if "$@"; then
    echo "[$(date '+%F %T')] $name OK"
  else
    echo "[$(date '+%F %T')] WARN: $name FAILED (rc=$?) - continuing"
  fi
}

step update-history \
  "$PY" -m wmshare_qlib.cli update-history \
  --store "$STORE" --out "$OUT" \
  --valuation-days 7 --max-ec "$MAX_EC_UPDATE"

step accumulate \
  "$PY" -m wmshare_qlib.cli accumulate \
  --via snapshot --symbols $ACCUM_SYMBOLS \
  --as-of "$(date +%F)" \
  --max-ec "$MAX_EC_ACCUM" --out "$ACCUM_OUT"

if [ "$MARGIN_ACCUM" = "1" ]; then
  step accumulate-margin \
    "$PY" -m wmshare_qlib.cli accumulate-margin \
    --symbols $MARGIN_SYMBOLS \
    --max-ec "$MAX_EC_ACCUM" --out "$MARGIN_OUT"
fi

# Freshness: always runs, fail-loud, its rc is the chain's rc.
"$PY" "$REPO/tools/freshness.py" --store "$STORE"
rc=$?
echo "[$(date '+%F %T')] freshness rc=$rc"
echo "[$(date '+%F %T')] === daily update done (rc=$rc) ==="
exit $rc
