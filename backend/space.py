"""Space environment: NOAA proton replays, orbits, radiation exposure, ground contact.

Measured: the GOES-18 proton series in data/noaa (see scripts/extract_noaa.py).
Simulated: everything else in this module. Orbits are circular. Exposure uses a
tilted geomagnetic dipole with a fixed cutoff band: solar protons reach low Earth
orbit over the polar caps and are shielded near the equator. This ignores
rigidity cutoffs, storm-time cutoff suppression, and the South Atlantic Anomaly.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "noaa"
STEP_MINUTES = 5

# ---------------------------------------------------------------------------
# Radiation scale

_ANCHORS = ((0.0, 0.0), (1.0, 0.22), (2.0, 0.55), (3.0, 0.72), (5.0, 1.0))


def pfu_to_risk(pfu: float) -> float:
    """Proton flux onto a 0-1 simulator scale: 10 pfu (S1) stays below cautious,
    100 pfu (S2) meets safe-hold, 1,000 pfu (S3) meets the hardware limit."""
    if pfu <= 1:
        return 0.0
    level = math.log10(pfu)
    if level >= _ANCHORS[-1][0]:
        return 1.0
    for (left, left_risk), (right, right_risk) in zip(_ANCHORS, _ANCHORS[1:]):
        if level <= right:
            return left_risk + (level - left) / (right - left) * (right_risk - left_risk)
    return 1.0


def risk_to_pfu(risk: float) -> float:
    if risk <= 0:
        return 1.0
    if risk >= 1:
        return 10 ** _ANCHORS[-1][0]
    for (left, left_risk), (right, right_risk) in zip(_ANCHORS, _ANCHORS[1:]):
        if risk <= right_risk:
            return 10 ** (left + (risk - left_risk) / (right_risk - left_risk) * (right - left))
    return 10 ** _ANCHORS[-1][0]


# ---------------------------------------------------------------------------
# Orbits and exposure

POLE_LAT = math.radians(80.7)
POLE_LON = math.radians(-72.7)
SIDEREAL_MIN = 1436.07
CUTOFF_LOW = 55.0
CUTOFF_HIGH = 63.0
LEAK = 0.004
SUBSAMPLES = 8
CONTACT_DEG = 18.0

GROUND_STATIONS = (
    {"id": "SVL", "name": "Svalbard", "lat": 78.23, "lon": 15.39},
    {"id": "FBK", "name": "Fairbanks", "lat": 64.86, "lon": -147.85},
    {"id": "ATL", "name": "Atlanta", "lat": 33.78, "lon": -84.40},
)


@dataclass(frozen=True)
class Orbit:
    label: str
    inclination: float
    period_min: float
    raan: float
    phase: float


def position(orbit: Orbit, minutes: float) -> tuple[float, float]:
    inc = math.radians(orbit.inclination)
    u = 2 * math.pi * (minutes / orbit.period_min + orbit.phase)
    lat = math.asin(math.sin(inc) * math.sin(u))
    lon = math.radians(orbit.raan) + math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
    lon -= 2 * math.pi * minutes / SIDEREAL_MIN
    lon = (lon + math.pi) % (2 * math.pi) - math.pi
    return math.degrees(lat), math.degrees(lon)


def magnetic_latitude(lat: float, lon: float) -> float:
    phi, lam = math.radians(lat), math.radians(lon)
    value = math.sin(phi) * math.sin(POLE_LAT) + math.cos(phi) * math.cos(POLE_LAT) * math.cos(lam - POLE_LON)
    return math.degrees(math.asin(max(-1.0, min(1.0, value))))


def exposure_at(lat: float, lon: float) -> float:
    mlat = abs(magnetic_latitude(lat, lon))
    if mlat <= CUTOFF_LOW:
        return LEAK
    if mlat >= CUTOFF_HIGH:
        return 1.0
    return LEAK + (mlat - CUTOFF_LOW) / (CUTOFF_HIGH - CUTOFF_LOW) * (1.0 - LEAK)


def _central_angle(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    value = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.acos(max(-1.0, min(1.0, value))))


def track(orbits: dict[str, Orbit], ticks: int, step_minutes: float = STEP_MINUTES) -> list[dict[str, dict]]:
    """Per step and satellite: start position, exposure averaged over the step,
    and the ground station in view (if any)."""
    rows = []
    for t in range(ticks):
        row = {}
        for sat, orbit in orbits.items():
            start = t * step_minutes
            total = 0.0
            contact = None
            for k in range(SUBSAMPLES):
                lat, lon = position(orbit, start + step_minutes * (k + 0.5) / SUBSAMPLES)
                total += exposure_at(lat, lon)
                if contact is None:
                    for station in GROUND_STATIONS:
                        if _central_angle(lat, lon, station["lat"], station["lon"]) <= CONTACT_DEG:
                            contact = station["id"]
                            break
            lat, lon = position(orbit, start)
            row[sat] = {
                "lat": round(lat, 1),
                "lon": round(lon, 1),
                "exposure": round(total / SUBSAMPLES, 3),
                "contact": contact,
            }
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# NOAA replays

ORDER = ["oct2024", "may2024", "mar2024", "jun2024", "jan2026", "quiet2024"]
SOURCE_URL = "https://www.ncei.noaa.gov/products/goes-r-space-environment-in-situ"
UNITS = "protons/(cm^2 sr s), GOES-18 westward differential-channel sum at and above 10 MeV"

WHAT_IF = (
    "What-if simulation. Real events set the dates and the measured radiation; the "
    "satellites, services, faults, and outcomes are simulated. Nothing here claims that "
    "real satellites failed or succeeded during these events."
)

MISSIONS = {
    "oct2024": {
        "title": "Hurricane Milton meets a solar storm",
        "label": "Oct 8–12, 2024 · Hurricane Milton",
        "context": (
            "Milton made landfall in Florida on the evening of 9 October 2024. That same "
            "morning an X1.8 solar flare launched a radiation storm. The satellites responders "
            "lean on for emergency comms, storm imagery, and flood maps fly into it during the "
            "first days of the recovery."
        ),
        "timeline": [
            ["2024-10-09T01:56:00Z", "X1.8 solar flare erupts"],
            ["2024-10-09T04:40:00Z", "Proton flux crosses the S1 radiation-storm threshold"],
            ["2024-10-10T00:30:00Z", "Milton makes landfall near Siesta Key, Florida (8:30 pm EDT, 9 Oct)"],
            ["2024-10-10T15:15:00Z", "Proton peak in the GOES-18 replay (S3 in the NOAA catalog)"],
        ],
        "sources": [
            ["NHC Milton report", "https://www.nhc.noaa.gov/data/tcr/AL142024_Milton.pdf"],
            ["NOAA SWPC", "https://www.swpc.noaa.gov/news/g4-severe-storm-watch-10-11-october"],
        ],
    },
    "may2024": {
        "title": "The Gannon storm",
        "label": "May 9–13, 2024 · Gannon storm",
        "context": "The strongest geomagnetic storm since 2003. Its radiation storm was moderate: two S2 peaks close together.",
    },
    "mar2024": {
        "title": "An S2 radiation storm",
        "label": "Mar 22–25, 2024 · S2",
        "context": "A radiation storm near the S3 threshold on an otherwise ordinary day.",
    },
    "jun2024": {
        "title": "An S3 radiation storm",
        "label": "Jun 7–10, 2024 · S3",
        "context": "A sharp rise into S3 that tests how quickly protection starts.",
    },
    "jan2026": {
        "title": "Strongest radiation storm in over 20 years",
        "label": "Jan 17–22, 2026 · S4 stress test",
        "context": "NOAA reported an S4 (severe) radiation storm on 19 January 2026. Even shielded orbits are exposed.",
        "sources": [["NOAA SWPC", "https://www.spaceweather.gov/news/s4-severe-solar-radiation-storm-progress-january-19th-2026"]],
    },
    "quiet2024": {
        "title": "Quiet control",
        "label": "Jul 18–22, 2024 · quiet control",
        "context": "No radiation storm. A good system should behave exactly like the others here.",
    },
}


def _load() -> dict[str, dict]:
    found = {}
    for path in DATA.glob("*.json"):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text())
        found[record["id"]] = record
    return found


WINDOWS = _load()


def window_ids() -> list[str]:
    return [name for name in ORDER if name in WINDOWS]


def mission(window_id: str) -> dict:
    return {"whatIf": WHAT_IF, "timeline": [], "sources": [], **MISSIONS.get(window_id, {})}


def load_series(window_id: str) -> dict:
    """Measured flux with gaps kept as None, a held copy for the environment, and quality."""
    record = WINDOWS[window_id]
    measured, held, quality, utc = [], [], [], []
    last = 0.0
    for sample in record["samples"]:
        utc.append(sample["t"])
        raw = sample.get("pfu")
        if raw is None:
            measured.append(None)
            held.append(last)
            quality.append("gap")
            continue
        last = float(raw)
        measured.append(last)
        held.append(last)
        degraded = sample.get("dtc") or sample.get("oob") or int(sample.get("valid") or 0) < 30
        quality.append("degraded" if degraded else "ok")
    hot = [i for i, value in enumerate(measured) if value is not None and value >= 10]
    peak = max(range(len(held)), key=lambda i: held[i])
    return {
        "id": window_id,
        "utc": utc,
        "measured": measured,
        "held": held,
        "quality": quality,
        "peak": peak,
        "stormStart": hot[0] if hot else len(held),
        "stormEnd": hot[-1] + 1 if hot else len(held),
        "record": record,
    }


def describe(window_id: str) -> dict:
    record = WINDOWS[window_id]
    return {
        "id": window_id,
        "label": MISSIONS.get(window_id, {}).get("label", record["label"]),
        "start": record["start"],
        "end": record["end"],
        "peakUtc": record["peakUtc"],
        "peakPfu": record["peakPfu"],
        "catalog": record["catalog"],
        "satellite": record["satellite"],
        "product": record["product"],
        "units": UNITS,
        "sourceUrl": SOURCE_URL,
        "mission": mission(window_id),
    }
