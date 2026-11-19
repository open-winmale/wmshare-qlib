"""wmshare-qlib: Qlib-native data layer powered by wmshare.

Three-layer data model (see README):
  1. history panel  - fields with real history -> Qlib .bin (backtestable)
  2. cross section  - today-only precomputed metrics -> score CSV (online scoring)
  3. accumulation   - daily snapshots accumulate into a backtestable panel over time

Explicit honesty contract: the 2,752 precomputed metric codes are CURRENT-SNAPSHOT
only (no history-series endpoint). We never fabricate history by pasting today's
value across dates.
"""

__version__ = "0.4.0"

# Imported so ``wmshare_qlib.qlib_io`` (etc.) resolves without an explicit
# submodule import, matching what __all__ promises. Import cost is small and
# qlib itself is optional inside qlib_io, so this stays side-effect free.
from . import (  # noqa: E402,F401  (re-export after __version__)
    config,
    cross_section,
    dump,
    estimate,
    fetch,
    format,
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
    "estimate",
    "reconcile",
]
