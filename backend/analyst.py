"""In-flight analyst, run on a compute-capable hub.

Inputs are only what the hub knows at step t: telemetry reports delivered over
the crosslinks, its memory of earlier reports, and the planned orbits of every
satellite (ephemeris is known in advance). It never reads true radiation or any
later sample.

It estimates the unshielded (ambient) flux from satellites currently over a
polar cap, forecasts each satellite's exposure windows for the next orbit, and
compares candidate plans for every service at risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.fleet import (
    CAUTIOUS,
    CHECKPOINT_STEPS,
    HOLD,
    HORIZON,
    RECOVER_PLANNED,
    RESUME_STREAK,
    RECOVER_STEPS,
    SATELLITES,
    SERVICE_CAPACITY,
    ANALYST_LOAD,
    TIER_WEIGHT,
    TRANSFER_STEPS,
    SAFETY_LIMIT,
)
from backend.space import pfu_to_risk, risk_to_pfu

AMBIENT_EXPOSURE = 0.6
AMBIENT_MEMORY = 24
STORM_LEVEL = 0.45
MOVE_MARGIN = 2.0


@dataclass
class Ambient:
    value: float
    source: str
    obs_t: int


@dataclass
class HubMemory:
    ambient: Ambient | None = None
    reports: dict[str, dict] = field(default_factory=dict)
    storm_announced: bool = False
    plans: dict[str, str] = field(default_factory=dict)


def update_ambient(memory: HubMemory, t: int) -> Ambient | None:
    for report in memory.reports.values():
        if report["exposure"] < AMBIENT_EXPOSURE or report["confidence"] <= 0:
            continue
        value = pfu_to_risk(risk_to_pfu(report["risk"]) / report["exposure"])
        if memory.ambient is None or report["obsT"] > memory.ambient.obs_t:
            memory.ambient = Ambient(round(value, 3), report["sat"], report["obsT"])
    if memory.ambient is not None and t - memory.ambient.obs_t > AMBIENT_MEMORY:
        memory.ambient = None
    return memory.ambient


def forecast(ambient: Ambient | None, ephemeris: list[dict], sat: str, step: int) -> float:
    if ambient is None:
        return 0.0
    step = min(step, len(ephemeris) - 1)
    return pfu_to_risk(risk_to_pfu(ambient.value) * ephemeris[step][sat]["exposure"])


def windows(ambient: Ambient | None, ephemeris: list[dict], sat: str, t: int, horizon: int = HORIZON) -> list[list[int]]:
    """Contiguous future steps forecast at or above the hold level."""
    found: list[list[int]] = []
    for step in range(t + 1, min(t + horizon, len(ephemeris) - 1) + 1):
        if forecast(ambient, ephemeris, sat, step) >= HOLD:
            if found and found[-1][1] == step - 1:
                found[-1][1] = step
            else:
                found.append([step, step])
    return found


def peak(ambient: Ambient | None, ephemeris: list[dict], sat: str, t: int, horizon: int = HORIZON) -> float:
    values = [forecast(ambient, ephemeris, sat, step) for step in range(t, min(t + horizon, len(ephemeris) - 1) + 1)]
    return max(values) if values else 0.0


def _held_steps(spans: list[list[int]], recover: int) -> int:
    return sum(end - start + 1 + recover for start, end in spans)


def candidates(
    service: dict,
    host: str,
    t: int,
    ambient: Ambient | None,
    ephemeris: list[dict],
    loads: dict[str, float],
    modes: dict[str, str],
) -> list[dict]:
    """Forecast outcomes of the options for one service. Steps are five minutes."""
    weight = TIER_WEIGHT[service["tier"]]
    spans = windows(ambient, ephemeris, host, t)
    options = []
    hot = peak(ambient, ephemeris, host, t)
    stay_down = _held_steps(spans, RECOVER_PLANNED)
    options.append(
        {
            "id": "stay",
            "label": f"Keep {service['name']} on {host}; checkpoint and pause through each forecast exposure",
            "target": host,
            "downtime": stay_down,
            "lostRisk": 0.0,
            "interlockRisk": False,
        }
    )
    reactive_down = _held_steps(spans, RESUME_STREAK + RECOVER_STEPS)
    options.append(
        {
            "id": "no-action",
            "label": f"Keep running on {host} with only built-in rules",
            "target": host,
            "downtime": reactive_down,
            "lostRisk": round(len(spans) * 3.0, 1),
            "interlockRisk": hot >= SAFETY_LIMIT,
        }
    )
    for peer, spec in SATELLITES.items():
        if peer == host or modes.get(peer) in ("protected", "recovering"):
            continue
        capacity = SERVICE_CAPACITY - (ANALYST_LOAD if spec["role"] == "hub" else 0.0)
        if loads.get(peer, 0.0) + service["period_load"] > capacity + 1e-9:
            continue
        peer_peak = peak(ambient, ephemeris, peer, t)
        if peer_peak >= CAUTIOUS:
            continue
        options.append(
            {
                "id": f"move:{peer}",
                "label": f"Hand {service['name']} to {peer} ({SATELLITES[peer]['name']}), forecast peak {peer_peak:.2f}",
                "target": peer,
                "downtime": CHECKPOINT_STEPS + TRANSFER_STEPS,
                "lostRisk": 0.0,
                "interlockRisk": False,
                "peerPeak": round(peer_peak, 2),
            }
        )
    for option in options:
        option["score"] = round(
            weight * option["downtime"] + option["lostRisk"] + (10.0 if option["interlockRisk"] else 0.0), 2
        )
    return options


def choose(options: list[dict], current: str) -> dict:
    """Lowest score among options the analyst may execute; a move must clearly beat staying."""
    allowed = [option for option in options if option["id"] != "no-action"]
    best = min(allowed, key=lambda option: (option["score"], option["id"] != "stay"))
    stay = next(option for option in allowed if option["id"] == "stay")
    if best["id"].startswith("move:") and stay["score"] - best["score"] < MOVE_MARGIN:
        return stay
    return best


def explain(service: dict, host: str, chosen: dict, options: list[dict], ambient: Ambient | None) -> str:
    stay = next(option for option in options if option["id"] == "stay")
    idle = next(option for option in options if option["id"] == "no-action")
    if ambient is None:
        source = ""
    elif "forecast" in ambient.source:
        source = f" No protons measured yet; planning assumes polar-cap radiation {ambient.value:.2f} from the {ambient.source}."
    else:
        source = f" Ambient radiation {ambient.value:.2f}, measured by {ambient.source} over the pole."
    if chosen["id"].startswith("move:"):
        return (
            f"{host} is forecast to be exposed for {stay['downtime'] * 5} minutes in the next orbit.{source} "
            f"Handing {service['name']} to {chosen['target']} costs about {chosen['downtime'] * 5} minutes of service. "
            f"Doing nothing would cost about {idle['downtime'] * 5} minutes and risks losing unsaved work."
        )
    if stay["downtime"] == 0:
        return f"{host} is not forecast to be exposed in the next orbit.{source}"
    return (
        f"No shielded peer has room for {service['name']}, or moving would not clearly help. "
        f"{host} checkpoints and pauses through {stay['downtime'] * 5} forecast minutes instead.{source}"
    )
