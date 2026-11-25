"""The ONLY module allowed to import Qlib (optional dependency).

Design contract (see follow-up roadmap M0):

  * all ``import qlib`` statements in this package live here and nowhere else;
  * NO ``qlib.init()`` is ever called in the caller's process — read-back
    verification runs in a short-lived CHILD process, so a host application
    that already initialized Qlib against another provider is never polluted;
  * the public read API (``qlib.data.D.features``) is preferred; the private
    ``FileFeatureStorage`` class is only a fallback, and its constructor kwargs
    are discovered via ``inspect.signature`` instead of being hard-coded;
  * when pyqlib is absent the module still imports fine and reports
    ``status="unavailable"`` — it never raises into the core pipeline.

Everything Qlib-version-specific is confined to this file, so upgrading Qlib
only ever touches this boundary.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


def qlib_version() -> str | None:
    """Return the installed pyqlib version, or None if it is not importable."""
    try:
        import qlib  # noqa: F401  (the single sanctioned import site)

        return getattr(qlib, "__version__", "unknown")
    except Exception:
        return None


# Child-process verifier. It is executed with:
#   python -c <CHILD_VERIFY_CODE> <payload.json> <result.json>
# Running in a child guarantees qlib.init() cannot affect the parent process.
_CHILD_VERIFY_CODE = r'''
import inspect, json, sys
payload_path, result_path = sys.argv[1], sys.argv[2]
with open(payload_path, encoding="utf-8") as fh:
    p = json.load(fh)
base, checks, freq = p["base"], p["checks"], p.get("freq", "day")
details, backend, errors = [], None, []

def close(a, b):
    try:
        return a is not None and abs(float(a) - float(b)) <= max(1e-6, abs(float(b)) * 1e-5)
    except (TypeError, ValueError):
        return False

# 1) Public API first: qlib.data.D.features
try:
    import qlib
    qlib.init(provider_uri=base, region="cn")
    from qlib.data import D
    backend = "D.features"
    for inst, field, date, expected in checks:
        try:
            frame = D.features([inst], ["$" + field], start_time=date, end_time=date, freq=freq)
            if frame is None or len(frame) == 0:
                details.append(f"{inst}/${field}@{date}: no rows from D.features")
                continue
            actual = float(frame.iloc[0, 0])
            details.append(
                f"{inst}/${field}@{date}: expected={expected:.6f} actual={actual:.6f} "
                f"{'OK' if close(actual, expected) else 'MISMATCH'}"
            )
        except Exception as exc:  # field/instrument may be unsupported by D.features
            errors.append(f"D.features {inst}/${field}@{date}: {type(exc).__name__}: {exc}")
except Exception as exc:
    errors.append(f"D.features backend unavailable: {type(exc).__name__}: {exc}")
    backend = None

# 2) Private-class fallback with signature probing (never hard-code kwargs).
if errors and any("no rows" not in e for e in errors):
    try:
        from qlib.data.storage.file_storage import FileFeatureStorage
        sig = inspect.signature(FileFeatureStorage.__init__)
        params = set(sig.parameters)
        backend = "FileFeatureStorage"
        with open(base + "/calendars/day.txt", encoding="utf-8") as fh:
            cal = fh.read().split()
        provider = {"day": base}
        for inst, field, date, expected in checks:
            try:
                kwargs = {"instrument": inst, "field": field, "freq": freq}
                # Different Qlib versions name this arg differently.
                if "provider_uri" in params:
                    kwargs["provider_uri"] = provider
                elif "provider" in params:
                    kwargs["provider"] = provider
                storage = FileFeatureStorage(**kwargs)
                idx = cal.index(date) if date in cal else None
                if idx is None:
                    details.append(f"{inst}/${field}@{date}: date not in calendar")
                    continue
                _, actual = storage[idx]
                details.append(
                    f"{inst}/${field}@{date}: expected={expected:.6f} actual={actual} "
                    f"{'OK' if close(actual, expected) else 'MISMATCH'}"
                )
            except Exception as exc:
                details.append(
                    f"{inst}/${field}@{date}: fallback failed: "
                    f"{type(exc).__name__}: {exc}"
                )
    except Exception as exc:
        details.append(f"FileFeatureStorage backend unavailable: {type(exc).__name__}: {exc}")
        backend = None

ok = bool(details) and all("OK" in d for d in details) and not any(
    "MISMATCH" in d or "failed" in d or "not in calendar" in d or "no rows" in d
    for d in details
)
status = "passed" if ok else ("unsupported" if backend is None else "mismatch")
with open(result_path, "w", encoding="utf-8") as fh:
    json.dump({"ok": ok, "status": status, "backend": backend, "details": details,
               "notes": errors[:5]}, fh, ensure_ascii=False)
'''


def verify_bin(
    qlib_dir: str | Path,
    checks: list[tuple[str, str, str, float]],
    *,
    freq: str = "day",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Verify dumped .bin files with Qlib's OWN reader, in a CHILD process.

    ``checks`` is a list of ``(instrument, field, date, expected_value)``.

    Returns a dict with stable keys, safe for CLI/JSON serialization::

        {"ok": bool|None, "status": "passed|mismatch|unsupported|unavailable|timeout",
         "backend": str|None, "details": [...], "notes": [...]}

    ``ok=None`` means the check could not run (Qlib absent / timeout) — it is
    explicitly NOT treated as a pass, and never raises into the caller.
    """
    if qlib_version() is None:
        return {
            "ok": None,
            "status": "unavailable",
            "backend": None,
            "details": ["pyqlib not importable (optional dependency not installed)"],
            "notes": [],
        }
    if not checks:
        return {
            "ok": None,
            "status": "unsupported",
            "backend": None,
            "details": ["no checks supplied"],
            "notes": [],
        }

    payload = {
        "base": str(Path(qlib_dir).resolve()),
        "checks": checks,
        "freq": freq,
    }
    with tempfile.TemporaryDirectory(prefix="wmshare_qlib_verify_") as td:
        payload_path = Path(td) / "payload.json"
        result_path = Path(td) / "result.json"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _CHILD_VERIFY_CODE, str(payload_path), str(result_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": None,
                "status": "timeout",
                "backend": None,
                "details": [f"Qlib read-back exceeded {timeout}s"],
                "notes": [],
            }
        if not result_path.exists():
            tail = (proc.stderr or proc.stdout or "")[-400:]
            return {
                "ok": None,
                "status": "unsupported",
                "backend": None,
                "details": ["Qlib child produced no result"],
                "notes": [tail],
            }
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "ok": None,
                "status": "unsupported",
                "backend": None,
                "details": [f"malformed Qlib child result: {exc}"],
                "notes": [],
            }


@dataclass
class ScoreRead:
    """Result of reading a score CSV back, with an explicit backend/reason."""

    series: pd.Series | None
    backend: str  # "ScoreFileModel" | "local-equivalent" | "empty"
    note: str = ""


def read_score(csv_path: str | Path, date: str) -> ScoreRead:
    """Read one day's scores via Qlib's ScoreFileModel, with an equivalent fallback.

    ScoreFileModel reads ``pd.read_csv(score_path, index_col=[0,1], parse_dates=True)``
    and ``get_data_with_date(date)``. Version matrix (2026-11-24, clean venvs):
    pyqlib 0.9.6/0.9.7 AND git main (0.9.8) all ship without
    ``qlib/contrib/model/base.py`` — the PyPI wheel physically omits the file,
    so every ScoreFileModel import path (evaluate.score and online.online_model)
    breaks on ModuleNotFoundError. Until upstream fixes the packaging, the
    fallback below is the working path; it runs the exact same logic and SAYS
    SO in ``note`` (no silent degradation). See roadmap P2-1 for the upstream
    issue material.
    """
    try:
        from qlib.contrib.online.online_model import ScoreFileModel  # type: ignore

        model = ScoreFileModel(str(csv_path))
        series = model.get_data_with_date(date)
        return ScoreRead(series=series, backend="ScoreFileModel")
    except Exception as exc:
        try:
            frame = pd.read_csv(csv_path, index_col=[0, 1], parse_dates=True, date_format="mixed")
            try:
                series = frame.loc(axis=0)[:, date]["score"]
                backend = "local-equivalent"
            except KeyError:
                series = pd.Series(dtype=float)
                backend = "empty"
            return ScoreRead(
                series=series,
                backend=backend,
                note=(
                    f"ScoreFileModel unavailable ({type(exc).__name__}: {str(exc)[:80]}); "
                    "used local reader with identical logic"
                ),
            )
        except Exception as read_exc:
            return ScoreRead(
                series=None,
                backend="empty",
                note=f"score CSV unreadable: {type(read_exc).__name__}: {read_exc}",
            )
