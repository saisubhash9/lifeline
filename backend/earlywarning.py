"""Flare early warning at runtime: pure-Python inference from data/models/flare_sep.json.

The model (scripts/train_flare_model.py) predicts, at a flare's X-ray peak, the chance that
the flare will drive a >=10 pfu proton storm. X-rays reach Earth in about 8 minutes and are
not shielded by the geomagnetic field, so a hub's X-ray photometer sees the flare at its peak,
typically hours before the protons arrive.

Replay windows use cross-fitted probabilities (data/flares/window_warnings.json): each
window's flares were scored by a model trained without the 60 days around that window.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "data" / "models" / "flare_screen.json"
WINDOWS_PATH = ROOT / "data" / "flares" / "window_screen.json"
VERIFY_PATH = ROOT / "data" / "flares" / "verifications.json"
CASCADE_PATH = ROOT / "data" / "models" / "cascade_eval.json"
COMPARISON_PATH = ROOT / "data" / "models" / "comparison.json"
VERIFY_DELAY_STEPS = 18  # Grok verifies at flare peak + 90 min, once coronagraph images exist


@lru_cache(maxsize=1)
def model() -> dict:
    return json.loads(MODEL_PATH.read_text())


@lru_cache(maxsize=1)
def _windows() -> dict:
    return json.loads(WINDOWS_PATH.read_text()) if WINDOWS_PATH.exists() else {}


@lru_cache(maxsize=1)
def _verifications() -> dict:
    return json.loads(VERIFY_PATH.read_text()) if VERIFY_PATH.exists() else {}


def _optional(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def connected(lon: float) -> float:
    return math.exp(-(((lon - 60.0) / 40.0) ** 2))


def features(peak_flux: float, fluence_to_peak: float, rise_minutes: float, background: float, lon: float | None, lat: float | None) -> dict:
    return {
        "log_peak": math.log10(peak_flux),
        "log_fluence": math.log10(max(fluence_to_peak, 1e-6)),
        "log_rise_min": math.log10(max(1.0, rise_minutes)),
        "log_background": math.log10(max(background, 1e-9)),
        "connected": connected(lon) if lon is not None else 0.0,
        "has_location": 1.0 if lon is not None else 0.0,
        "abs_lat": abs(lat) / 90.0 if lat is not None else 0.0,
    }


def probability(values: dict) -> float:
    spec = model()
    z = spec["coef"][0]
    for name, mean, std, weight in zip(spec["features"], spec["mean"], spec["std"], spec["coef"][1:]):
        z += weight * (values[name] - mean) / std
    return 1.0 / (1.0 + math.exp(-z))


def summary() -> dict:
    spec = model()
    comparison = _optional(COMPARISON_PATH)
    return {
        "name": spec["name"],
        "model": spec["model"],
        "target": spec["target"],
        "features": spec["featureNotes"],
        "threshold": spec["threshold"],
        "split": spec["split"],
        "metrics": spec["metrics"],
        "onsetDelayHours": spec["onsetDelayHours"],
        "sources": spec["sources"],
        "cascade": _optional(CASCADE_PATH),
        "comparison": None
        if comparison is None
        else [
            {"model": r["model"], "rocAuc": r["testRocAuc"], "prAuc": r["testPrAuc"], "budgetCaught": r["budget"]["stormsCaught"], "warningsToCatchAll": r["warningsToCatchAll"]}
            for r in sorted(comparison["results"], key=lambda r: -r["testPrAuc"])
        ],
    }


def warnings_for(window: str, utc: list[str], step_minutes: int = 5) -> list[dict]:
    """Flares >= M1 inside a replay window, mapped to the first replay step that starts at or
    after the X-ray peak, so a warning can never precede the peak it is based on."""
    index = {stamp: i for i, stamp in enumerate(utc)}
    found = []
    for item in _windows().get(window, []):
        peak = item["peakUtc"]
        minute = int(peak[14:16])
        bucket = f"{peak[:14]}{minute - minute % step_minutes:02d}:00Z"
        if bucket not in index or index[bucket] + 1 >= len(utc):
            continue
        low, mid, high = item["onsetHours"]
        found.append(
            {
                **item,
                "verification": _verifications().get(str(item["flare_id"])),
                "t": index[bucket] + (0 if bucket == peak else 1),
                "onsetSteps": [int(low * 60 / step_minutes), int(mid * 60 / step_minutes), int(high * 60 / step_minutes)],
            }
        )
    return sorted(found, key=lambda entry: entry["t"])
