"""The simulated constellation: who can think, who carries which service.

Hubs have enough onboard compute to analyze the whole fleet. Simple satellites
have small processors that run their own payload service and follow built-in
rules or instructions; they cannot forecast exposure for themselves or others.
Every satellite has an inter-satellite (or relay) link to the others.
"""

from __future__ import annotations

from backend.space import Orbit

SATELLITES = {
    "H1": {
        "name": "Hub-1",
        "role": "hub",
        "orbit": Orbit("30°, low-inclination", 30.0, 96.1, 240.0, 0.55),
        "about": "Compute-capable. Primary in-flight analyst, shielded by Earth's magnetic field; a safe harbor for services.",
    },
    "H2": {
        "name": "Hub-2",
        "role": "hub",
        "orbit": Orbit("53°, mid-inclination", 53.0, 95.4, 120.0, 0.15),
        "about": "Compute-capable. Backup analyst; takes over when Hub-1 is paused or out of contact.",
    },
    "S1": {
        "name": "Relay-1",
        "role": "simple",
        "orbit": Orbit("86.4°, polar (Iridium-like)", 86.4, 100.4, 30.0, 0.0),
        "about": "Simple satellite. Carries the emergency comms relay.",
    },
    "S2": {
        "name": "Imager-1",
        "role": "simple",
        "orbit": Orbit("97.4°, sun-synchronous polar", 97.4, 94.6, 300.0, 0.4),
        "about": "Simple satellite. Carries hurricane imagery processing.",
    },
    "S3": {
        "name": "Mapper-1",
        "role": "simple",
        "orbit": Orbit("53°, mid-inclination", 53.0, 95.4, 0.0, 0.7),
        "about": "Simple satellite. Carries flood-mapping inference.",
    },
}

SAT_IDS = tuple(SATELLITES)
HUBS = tuple(sat for sat, spec in SATELLITES.items() if spec["role"] == "hub")
SIMPLE = tuple(sat for sat, spec in SATELLITES.items() if spec["role"] == "simple")
ORBITS = {sat: spec["orbit"] for sat, spec in SATELLITES.items()}

# Services released as periodic tasks: each task is due one period after release.
# Step = 5 minutes. The critical relay has half-size tasks twice as often.
SERVICES = (
    {"id": "comms", "name": "Emergency comms", "label": "Emergency comms relay", "tier": "critical", "home": "S1", "period": 12, "work": 5.0},
    {"id": "imagery", "name": "Imagery", "label": "Hurricane imagery processing", "tier": "essential", "home": "S2", "period": 24, "work": 10.0},
    {"id": "floodmap", "name": "Flood maps", "label": "Flood-mapping inference", "tier": "essential", "home": "S3", "period": 24, "work": 10.0},
)
TIER_ORDER = {"critical": 0, "essential": 1, "best-effort": 2}
TIER_WEIGHT = {"critical": 3.0, "essential": 1.0, "best-effort": 0.0}

# Thresholds on the 0-1 risk scale.
CAUTIOUS = 0.35
HOLD = 0.55
CLEAR = 0.28
SAFETY_LIMIT = 0.72

# Durations in steps (five minutes each).
CHECKPOINT_STEPS = 1
RECOVER_STEPS = 2
RECOVER_PLANNED = 1
RESUME_STREAK = 3
NORMAL_CHECKPOINT_EVERY = 12
CAUTIOUS_CHECKPOINT_EVERY = 4
TRANSFER_STEPS = 1
GROUND_ANALYSIS_STEPS = 6
HORIZON = 19
FALLBACK_AFTER = 12
ANALYST_LOAD = 0.15
SERVICE_CAPACITY = 0.95
HIT_SCALE = 0.03
