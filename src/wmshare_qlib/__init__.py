"""wmshare-qlib: Qlib-native data layer powered by wmshare.

Three-layer data model (see README):
  1. history panel  - fields with real history -> Qlib .bin (backtestable)
  2. cross section  - today-only precomputed metrics -> score CSV (online scoring)
  3. accumulation   - daily snapshots accumulate into a backtestable panel over time

Explicit honesty contract: the 2,752 precomputed metric codes are CURRENT-SNAPSHOT
only (no history-series endpoint). We never fabricate history by pasting today's
value across dates.
"""

__version__ = "0.3.3"

__all__ = [
    "config",
    "normalize",
    "fetch",
    "panel",
    "cross_section",
    "dump",
]
