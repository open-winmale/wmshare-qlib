"""wmshare-qlib: Qlib-native data layer powered by wmshare.

Three-layer data model (see README):
  1. history panel  - fields with real history -> Qlib .bin (backtestable)
  2. cross section  - today-only precomputed metrics -> score CSV (online scoring)
  3. accumulation   - daily snapshots accumulate into a backtestable panel over time

Explicit honesty contract: the ~2,750 precomputed screener/indicator metric
codes are CURRENT-SNAPSHOT only - their querying surface accepts no date
parameter, so requesting their past values is inexpressible upstream. This is
a different data domain from the valuation fields of layer 1 (which DO carry
dated series). We never fabricate history by pasting today's value across
dates.
"""

__version__ = "1.0.0"

# Imported so ``wmshare_qlib.qlib_io`` (etc.) resolves without an explicit
# submodule import, matching what __all__ promises. Import cost is small and
# qlib itself is optional inside qlib_io, so this stays side-effect free.
from . import (  # noqa: E402,F401  (re-export after __version__)
    config,
    cross_section,
    dump,
    fetch,
    format,
    margin_store,
    normalize,
    panel,
    qlib_io,
    reconcile,
    resume,
)

__all__ = [
    "config",
    "normalize",
    "fetch",
    "panel",
    "cross_section",
    "dump",
    "format",
    "qlib_io",
    "resume",
    "reconcile",
    "margin_store",
]
