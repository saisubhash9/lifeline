"""Four fleets fly through one replayed storm.

Every fleet sees the same measured flux, the same orbits, the same sensor noise,
the same hardware-error telemetry, the same link-loss draws, and the same fault
draws. Only the protection strategy differs:

  ground     Satellites only have a hardware safe mode. The ground reads telemetry
             during station passes and uplinks commands on a later pass.
  threshold  Every satellite follows fixed onboard rules on its own sensor.
  local      Hubs forecast and protect themselves; simple satellites follow fixed
             rules. Nothing is shared. This isolates the value of sharing.
  lifeline   Hubs collect fleet telemetry over crosslinks, forecast every
             satellite's exposure, send each one its protective schedule, and
             hand critical services to shielded peers. Simple satellites fall back
             to fixed rules if they stop hearing from a hub.

Faults are an assumption: while a payload is active, each five-minute step has hit
probability risk^2 * HIT_SCALE, which loses unsaved work. Safe mode powers the
payload down, so unsaved work is lost unless it was checkpointed first. A hardware
interlock forces safe mode at true risk 0.72.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from backend import analyst, earlywarning
from backend.fleet import (
    ANALYST_LOAD,
    CAUTIOUS,
    CAUTIOUS_CHECKPOINT_EVERY,
    READY_CHECKPOINT_EVERY,
    CHECKPOINT_STEPS,
    CLEAR,
    FALLBACK_AFTER,
    GROUND_ANALYSIS_STEPS,
    HIT_SCALE,
    HOLD,
    HUBS,
    NORMAL_CHECKPOINT_EVERY,
    RECOVER_PLANNED,
    RECOVER_STEPS,
    RESUME_STREAK,
    SAFETY_LIMIT,
    SATELLITES,
    SAT_IDS,
    SERVICE_CAPACITY,
    SERVICES,
    SIMPLE,
    ORBITS,
    TIER_ORDER,
    TRANSFER_STEPS,
)
from backend.space import MISSIONS, STEP_MINUTES, load_series, mission, pfu_to_risk, track, window_ids

STRATEGIES = ("ground", "threshold", "local", "lifeline", "lifeline_ml", "lifeline_ew")
STRATEGY_NAMES = {
    "ground": "Ground-dependent",
    "threshold": "Fixed-threshold",
    "local": "Hubs only, no sharing",
    "lifeline": "Lifeline, protons only",
    "lifeline_ml": "Lifeline + ML warning only",
    "lifeline_ew": "Lifeline + ML → Grok verified",
}
PROVISIONAL_RISK = 0.62  # expected polar-cap level assumed after a warning, until protons are measured
ACTIVE = ("normal", "cautious", "checkpointing")
COMPUTING = ("normal", "cautious")
HELD = ("protected", "recovering")
SENSOR_NOISE = 0.04
TELEMETRY_RATE = 12.0
TELEMETRY_CONFIDENCE = 0.5


@dataclass
class Scenario:
    window: str = "oct2024"
    seed: int = 7
    latency: int = 1
    loss: float = 0.0


# ---------------------------------------------------------------------------
# Shared environment


@dataclass
class Env:
    ticks: int
    series: dict
    ephemeris: list[dict]
    true: list[dict[str, float]]
    observed: list[dict[str, float]]
    confidence: list[dict[str, float]]
    errors: list[dict[str, int]]
    fused: list[dict[str, float]]
    shock: list[dict[str, float]]
    link: list[dict[tuple[str, str], float]]
    observed_low: list[dict[str, int]]
    fused_low: list[dict[str, int]]
    flares: list[dict] = field(default_factory=list)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, count, product = math.exp(-lam), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def _streaks(rows: list[dict[str, float]]) -> list[dict[str, int]]:
    out, run = [], {sat: 0 for sat in SAT_IDS}
    for row in rows:
        run = {sat: run[sat] + 1 if row[sat] < CLEAR else 0 for sat in SAT_IDS}
        out.append(dict(run))
    return out


def build_env(scenario: Scenario) -> Env:
    series = load_series(scenario.window)
    ticks = len(series["held"])
    ephemeris = track(ORBITS, ticks)
    rng = random.Random(scenario.seed)
    tele = random.Random(scenario.seed + 7919)
    links = random.Random(scenario.seed + 104729)
    pairs = [(a, b) for a in SAT_IDS for b in SAT_IDS if a != b]
    true, observed, confidence, errors, fused, shock, link = [], [], [], [], [], [], []
    last = {sat: 0.0 for sat in SAT_IDS}
    ewma = {sat: 0.0 for sat in SAT_IDS}
    for t in range(ticks):
        quality = series["quality"][t]
        row_true, row_obs, row_conf, row_err, row_fused, row_shock = {}, {}, {}, {}, {}, {}
        for sat in SAT_IDS:
            risk = pfu_to_risk(series["held"][t] * ephemeris[t][sat]["exposure"])
            row_true[sat] = risk
            row_shock[sat] = rng.random()
            if quality == "gap":
                row_obs[sat], row_conf[sat] = last[sat], 0.0
            else:
                row_obs[sat] = min(1.0, max(0.0, risk + rng.gauss(0, SENSOR_NOISE)))
                row_conf[sat] = 0.55 if quality == "degraded" else 0.92
                last[sat] = row_obs[sat]
            row_err[sat] = _poisson(tele, TELEMETRY_RATE * risk * risk)
            ewma[sat] = 0.5 * ewma[sat] + 0.5 * row_err[sat]
            hardware = min(1.0, math.sqrt(ewma[sat] / TELEMETRY_RATE))
            weight = row_conf[sat] + TELEMETRY_CONFIDENCE
            row_fused[sat] = (row_conf[sat] * row_obs[sat] + TELEMETRY_CONFIDENCE * hardware) / weight
        true.append(row_true)
        observed.append(row_obs)
        confidence.append(row_conf)
        errors.append(row_err)
        fused.append(row_fused)
        shock.append(row_shock)
        link.append({pair: links.random() for pair in pairs})
    flares = earlywarning.warnings_for(scenario.window, series["utc"])
    return Env(ticks, series, ephemeris, true, observed, confidence, errors, fused, shock, link, _streaks(observed), _streaks(fused), flares)


# ---------------------------------------------------------------------------
# Fleet state


@dataclass
class Task:
    release: int
    due: int
    left: float


@dataclass
class Job:
    id: str
    name: str
    tier: str
    home: str
    node: str
    period: int = 0
    work: float = 0.0
    offset: int = 0
    tasks: list[Task] = field(default_factory=list)
    met: int = 0
    missed: int = 0
    work_done: float = 0.0
    unsaved: float = 0.0
    status: str = "running"
    migrate_to: str | None = None
    migrate_left: int = 0
    down_since: int | None = None
    storm_steps: int = 0
    storm_up: int = 0

    @property
    def essential(self) -> bool:
        return self.tier != "best-effort"

    @property
    def load(self) -> float:
        return self.work / self.period if self.period else 0.0


@dataclass
class Node:
    id: str
    role: str
    mode: str = "normal"
    resume_mode: str = "normal"
    checkpoint_left: int = 0
    hold_after: bool = False
    planned: bool = False
    recover_left: int = 0
    since_checkpoint: int = 0
    uncorrectable: int = 0
    locked: bool = False
    heard_t: int = -10_000
    ready_until: int = -1
    windows: list[list[int]] = field(default_factory=list)
    pending: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Metrics:
    lost: float = 0.0
    interlocks: int = 0
    exposed: int = 0
    up: int = 0
    up_den: int = 0
    overhead: int = 0
    interruptions: list[int] = field(default_factory=list)
    handoffs: int = 0
    messages: int = 0
    dropped: int = 0


@dataclass
class World:
    name: str
    nodes: dict[str, Node]
    jobs: list[Job]
    metrics: Metrics = field(default_factory=Metrics)
    memory: dict[str, analyst.HubMemory] = field(default_factory=dict)
    queue: list[dict] = field(default_factory=list)
    telemetry: dict[str, list[tuple[int, float, int]]] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    analysts: set[str] = field(default_factory=set)
    warning_mode: str | None = None
    warning: dict | None = None


def new_world(name: str) -> World:
    nodes = {
        sat: Node(sat, SATELLITES[sat]["role"], since_checkpoint=i * NORMAL_CHECKPOINT_EVERY // len(SAT_IDS))
        for i, sat in enumerate(SAT_IDS)
    }
    jobs = [
        Job(spec["id"], spec["name"], spec["tier"], spec["home"], spec["home"], spec["period"], spec["work"], offset=i * 4)
        for i, spec in enumerate(SERVICES)
    ]
    jobs += [Job(f"archive-{sat}", f"Archive-{sat}", "best-effort", sat, sat) for sat in SAT_IDS]
    return World(
        name,
        nodes,
        jobs,
        warning_mode={"lifeline_ml": "ml", "lifeline_ew": "cascade"}.get(name),
        memory={hub: analyst.HubMemory() for hub in HUBS},
        telemetry={sat: [] for sat in SAT_IDS},
    )


def hosted(world: World, sat: str) -> list[Job]:
    return [job for job in world.jobs if job.node == sat and job.status in ("running", "paused")]


def capacity(sat: str) -> float:
    return SERVICE_CAPACITY - (ANALYST_LOAD if SATELLITES[sat]["role"] == "hub" else 0.0)


def service_load(world: World, sat: str) -> float:
    total = 0.0
    for job in world.jobs:
        if not job.essential:
            continue
        here = job.node == sat and job.status in ("running", "paused", "migrating")
        arriving = job.status == "migrating" and job.migrate_to == sat
        if here or arriving:
            total += job.load
    return total


def fits(world: World, sat: str, job: Job) -> bool:
    return service_load(world, sat) + job.load <= capacity(sat) + 1e-9


def unsaved(world: World, sat: str) -> float:
    return sum(job.unsaved for job in hosted(world, sat))


def _lose(world: World, job: Job) -> None:
    if job.unsaved <= 0:
        return
    world.metrics.lost += job.unsaved
    if job.essential and job.tasks:
        job.tasks[0].left += job.unsaved
    elif not job.essential:
        job.work_done -= job.unsaved
    job.unsaved = 0.0


def _note(world: World, t: int, sat: str, text: str) -> None:
    world.log.append({"t": t, "sat": sat, "text": text})


# ---------------------------------------------------------------------------
# Messages over crosslinks


def send(world: World, env: Env, scenario: Scenario, t: int, msg: dict) -> None:
    world.metrics.messages += 1
    if env.link[t][(msg["sender"], msg["receiver"])] < scenario.loss:
        world.metrics.dropped += 1
        return
    world.queue.append({**msg, "sendT": t, "deliverT": t + max(1, scenario.latency)})


def deliver(world: World, t: int) -> list[dict]:
    arrived = [msg for msg in world.queue if msg["deliverT"] <= t]
    world.queue = [msg for msg in world.queue if msg["deliverT"] > t]
    for msg in arrived:
        node = world.nodes[msg["receiver"]]
        kind = msg["kind"]
        if kind == "report" and node.role == "hub":
            world.memory[node.id].reports[msg["sender"]] = msg
            continue
        node.heard_t = t
        if kind == "schedule":
            node.windows = [list(span) for span in msg["windows"]]
        elif kind == "readiness":
            node.ready_until = msg["until"]
        elif kind in ("handoff", "return"):
            node.pending = [item for item in node.pending if item[0] != msg["job"]] + [(msg["job"], msg["target"])]
    return arrived


def report(world: World, env: Env, t: int, sat: str) -> dict:
    return {
        "kind": "report",
        "sender": sat,
        "sat": sat,
        "obsT": t,
        "risk": round(env.fused[t][sat], 3),
        "confidence": env.confidence[t][sat],
        "exposure": env.ephemeris[t][sat]["exposure"],
        "errors": env.errors[t][sat],
        "mode": world.nodes[sat].mode,
        "services": [job.id for job in hosted(world, sat) if job.essential],
        "load": round(service_load(world, sat), 3),
    }


# ---------------------------------------------------------------------------
# Controllers. Each returns {sat: action}, a set of planned holds, and migrations.


def fixed_rule(node: Node, reading: float, streak: int) -> str | None:
    if node.mode in ACTIVE and reading >= HOLD and not node.hold_after:
        return "safe-hold"
    if node.mode == "normal" and reading >= CAUTIOUS:
        return "cautious"
    if node.mode == "cautious" and reading < CLEAR:
        return "relax"
    if node.mode == "protected" and not node.locked and reading < CLEAR and streak >= RESUME_STREAK:
        return "resume"
    return None


def _in(windows: list[list[int]], step: int) -> bool:
    return any(start <= step <= end for start, end in windows)


def follow(node: Node, reading: float, windows: list[list[int]], t: int) -> tuple[str | None, bool]:
    """Protect on a forecast schedule. Returns the action and whether it is planned."""
    if node.mode == "protected":
        if not node.locked and reading < CAUTIOUS and not _in(windows, t) and not _in(windows, t + 1):
            return "resume", False
        return None, False
    if node.mode == "recovering":
        return None, False
    if node.mode in ACTIVE and not node.hold_after:
        if reading >= HOLD:
            return "safe-hold", False
        if _in(windows, t + 1):
            return "safe-hold", True
    if node.mode == "normal" and reading >= CAUTIOUS:
        return "cautious", False
    if node.mode == "cautious" and reading < CLEAR:
        return "relax", False
    return None, False


def ground_control(world: World, env: Env, t: int) -> dict:
    actions = {}
    for sat in SAT_IDS:
        contact = env.ephemeris[t][sat]["contact"]
        if not contact:
            continue
        history = world.telemetry[sat]
        ready = [item for item in history if item[0] <= t - GROUND_ANALYSIS_STEPS]
        history.append((t, env.observed[t][sat], env.observed_low[t][sat]))
        if not ready:
            continue
        _, reading, streak = ready[-1]
        node = world.nodes[sat]
        action = fixed_rule(node, reading, streak)
        if node.mode == "protected" and node.locked and reading < CLEAR:
            action = "resume"
        if action:
            actions[sat] = action
    return {"actions": actions, "planned": set(), "migrations": []}


def threshold_control(world: World, env: Env, t: int, sats=SAT_IDS) -> dict:
    actions = {}
    for sat in sats:
        action = fixed_rule(world.nodes[sat], env.observed[t][sat], env.observed_low[t][sat])
        if action:
            actions[sat] = action
    return {"actions": actions, "planned": set(), "migrations": []}


def smart_control(world: World, env: Env, scenario: Scenario, t: int, share: bool, advisor, state: dict) -> dict:
    actions: dict[str, str] = {}
    planned: set[str] = set()
    migrations: list[tuple[str, str]] = []
    decisions: list[dict] = []
    world.analysts = set()

    for hub in HUBS:
        memory = world.memory[hub]
        memory.reports[hub] = report(world, env, t, hub)
        analyst.update_ambient(memory, t)

    # Hubs protect themselves from their own forecast.
    for hub in HUBS:
        node = world.nodes[hub]
        spans = analyst.windows(world.memory[hub].ambient, env.ephemeris, hub, t)
        action, is_planned = follow(node, env.fused[t][hub], spans, t)
        if action:
            actions[hub] = action
            if is_planned:
                planned.add(hub)

    if share and world.warning_mode:
        decisions += _flare_watch(world, env, scenario, t)

    if share:
        for hub in HUBS:
            if _is_analyst(world, hub, t):
                world.analysts.add(hub)
                if state.get("analyst") not in (None, hub):
                    decisions.append(
                        {
                            "t": t,
                            "kind": "takeover",
                            "hub": hub,
                            "title": f"{hub} takes over as analyst",
                            "text": f"{state['analyst']} is paused or out of contact, so {hub} ({SATELLITES[hub]['name']}) runs the fleet analysis.",
                        }
                    )
                state["analyst"] = hub
                decisions += _analyze(world, env, scenario, t, hub, advisor, state)
                break

    for sat in SIMPLE:
        node = world.nodes[sat]
        reading = env.fused[t][sat] if share else env.observed[t][sat]
        stale = t - node.heard_t > FALLBACK_AFTER and not any(end >= t for _, end in node.windows)
        if not share or stale:
            action = fixed_rule(node, reading, env.fused_low[t][sat] if share else env.observed_low[t][sat])
            if action:
                actions[sat] = action
            continue
        action, is_planned = follow(node, reading, node.windows, t)
        if action in ("safe-hold", "resume"):
            actions[sat] = action
            if is_planned:
                planned.add(sat)
            continue
        if _handoff(world, node, actions, migrations):
            continue
        if action:
            actions[sat] = action

    for hub in HUBS:
        if hub in actions:
            continue
        _handoff(world, world.nodes[hub], actions, migrations)

    if share:
        for sat in SAT_IDS:
            for hub in HUBS:
                if hub != sat:
                    send(world, env, scenario, t, {**report(world, env, t, sat), "receiver": hub})
    return {"actions": actions, "planned": planned, "migrations": migrations, "decisions": decisions}


def _readiness(world: World, env: Env, scenario: Scenario, t: int, hub: str, until: int) -> None:
    """Readiness (more frequent checkpoints) only for satellites whose orbits would be exposed
    if the storm arrives; shielded satellites keep their normal rhythm."""
    expected = analyst.Ambient(PROVISIONAL_RISK, "X-ray flare forecast", t)
    for sat in SAT_IDS:
        exposed = analyst.peak(expected, env.ephemeris, sat, t) >= HOLD
        value = until if exposed or until <= t else -1
        if sat in HUBS:
            world.nodes[sat].ready_until = value
        else:
            send(world, env, scenario, t, {"kind": "readiness", "sender": hub, "receiver": sat, "until": value})


def _flare_watch(world: World, env: Env, scenario: Scenario, t: int) -> list[dict]:
    """Hubs carry an X-ray photometer: a flare is seen at its peak, before any protons.

    "ml" mode acts on the stage-1 screener alone. "cascade" mode goes to readiness on the
    screener, then waits for Grok's verification (peak + 90 min) before pre-positioning; a
    rejection stands the fleet down early. A missing verification falls back to the screener.
    """
    decisions = []
    watching = [hub for hub in HUBS if world.nodes[hub].mode not in HELD]
    if not watching:
        return decisions
    hub = watching[0]
    storm = any(
        world.memory[h].ambient is not None and world.memory[h].ambient.value >= analyst.STORM_LEVEL for h in HUBS
    )
    cascade = world.warning_mode == "cascade"
    for flare in env.flares:
        if not flare["warn"]:
            continue
        verification = flare.get("verification") if cascade else None
        side = "" if flare["lon"] is None else (f"W{abs(flare['lon']):.0f}" if flare["lon"] > 0 else f"E{abs(flare['lon']):.0f}")
        where = f" at {side}" if side else ""
        if flare["t"] == t:
            until = t + flare["onsetSteps"][2]
            previous = world.warning if world.warning and world.warning["until"] >= t else None
            confirmed = not verification or bool(previous and previous.get("confirmed"))
            world.warning = {**flare, "until": max(until, (previous or {}).get("until", -1)), "hub": hub, "confirmed": confirmed}
            if previous and "lead" in previous:
                world.warning["lead"] = previous["lead"]
            _readiness(world, env, scenario, t, hub, world.warning["until"])
            onset = [env.series["utc"][min(env.ticks - 1, t + step)] for step in flare["onsetSteps"]]
            if storm:
                action = "A storm is already under way; readiness extended."
            elif verification:
                action = "The fleet goes to readiness. Grok will verify the flare with coronagraph images 90 minutes after the peak before critical services move."
            else:
                action = "The fleet goes to readiness and critical services are pre-positioned on shielded peers."
            decisions.append(
                {
                    "t": t,
                    "kind": "flare-warning",
                    "hub": hub,
                    "title": f"Flare warning: {flare['class']} flare{where}",
                    "text": (
                        f"{hub}'s X-ray photometer saw a {flare['class']} flare peak. The screening model gives a "
                        f"{flare['p'] * 100:.1f}% chance of a proton storm, above its high-recall threshold of "
                        f"{earlywarning.model()['threshold'] * 100:.1f}%. Protons typically arrive "
                        f"{flare['onsetHours'][0]:.0f}-{flare['onsetHours'][2]:.0f} h after the flare. {action}"
                    ),
                    "probability": flare["p"],
                    "flareClass": flare["class"],
                    "lon": flare["lon"],
                    "onsetUtc": onset,
                    "sep": flare["sep"],
                }
            )
        if verification and flare["t"] + earlywarning.VERIFY_DELAY_STEPS == t:
            confirm = verification.get("verdict", "confirm") == "confirm"
            active = world.warning if world.warning and world.warning["t"] == flare["t"] else None
            if active is not None:
                if confirm:
                    active["confirmed"] = True
                elif "lead" not in active and not storm:
                    world.warning = None
                    _readiness(world, env, scenario, t, hub, t)
            if confirm:
                effect = "Critical services move to shielded peers now." if active is not None and not storm else "Readiness continues."
            else:
                effect = "No sign of an eruption, so the fleet stands down early." if active is not None and not storm else "A storm is already being tracked from measured protons."
            decisions.append(
                {
                    "t": t,
                    "kind": "grok-verification",
                    "hub": hub,
                    "title": f"Grok {'confirms' if confirm else 'rejects'} the {flare['class']} warning",
                    "text": f"{verification['reason']} {effect}",
                    "verdict": verification.get("verdict", "confirm"),
                    "probability": verification.get("probability"),
                    "threshold": verification.get("threshold"),
                    "cmeVisible": verification.get("cmeVisible"),
                    "cmeExtent": verification.get("cmeExtent"),
                    "eruptionSigns": verification.get("eruptionSigns", []),
                    "panel": verification.get("panel"),
                    "flareClass": flare["class"],
                    "flareUtc": flare["peakUtc"],
                    "sep": flare["sep"],
                    "source": verification.get("model", "grok"),
                }
            )
    warning = world.warning
    if warning and t == warning["until"] + 1:
        if "lead" not in warning:
            decisions.append(
                {
                    "t": t,
                    "kind": "stand-down",
                    "hub": hub,
                    "title": "Stand down: no protons arrived",
                    "text": f"No proton storm within {warning['onsetHours'][2]:.0f} h of the {warning['class']} flare. Readiness ends and pre-positioned services return home.",
                }
            )
        world.warning = None
    return decisions


def _handoff(world: World, node: Node, actions: dict, migrations: list) -> bool:
    """Carry out a pending handoff: checkpoint first, then transfer."""
    while node.pending:
        job_id, target = node.pending[0]
        job = next(item for item in world.jobs if item.id == job_id)
        if job.node != node.id or job.status == "migrating" or target == node.id:
            node.pending.pop(0)
            continue
        if node.mode not in COMPUTING:
            return node.mode == "checkpointing"
        if job.unsaved > 1e-6:
            actions[node.id] = "checkpoint"
        else:
            migrations.append((job.id, target))
            node.pending.pop(0)
        return True
    return False


def _is_analyst(world: World, hub: str, t: int) -> bool:
    if world.nodes[hub].mode in HELD:
        return False
    if hub == HUBS[0]:
        return True
    primary = world.memory[hub].reports.get(HUBS[0])
    return primary is None or primary["mode"] in HELD or t - primary["obsT"] > 3


def _analyze(world: World, env: Env, scenario: Scenario, t: int, hub: str, advisor, state: dict) -> list[dict]:
    memory = world.memory[hub]
    ambient = memory.ambient
    reports = memory.reports
    decisions = []
    real_storm = ambient is not None and ambient.value >= analyst.STORM_LEVEL
    warning = world.warning if world.warning and t <= world.warning["until"] else None
    confirmed = warning is not None and warning.get("confirmed", True)
    if warning and real_storm and "lead" not in warning:
        # Protons are now measured: schedule-based protection takes over, readiness ends.
        warning["lead"] = t - warning["t"]
        for sat in SAT_IDS:
            if sat in HUBS:
                world.nodes[sat].ready_until = t
            else:
                send(world, env, scenario, t, {"kind": "readiness", "sender": hub, "receiver": sat, "until": t})
    planning = ambient
    if warning and confirmed and not real_storm:
        planning = analyst.Ambient(PROVISIONAL_RISK, "X-ray flare forecast", t)
    modes = {sat: report_["mode"] for sat, report_ in reports.items()}
    loads = {sat: report_["load"] for sat, report_ in reports.items()}
    placement = {service: sat for sat, report_ in reports.items() for service in report_["services"]}

    if ambient is not None and ambient.value >= analyst.STORM_LEVEL and not memory.storm_announced:
        memory.storm_announced = True
        exposed = [sat for sat in SAT_IDS if analyst.windows(ambient, env.ephemeris, sat, t)]
        decisions.append(
            {
                "t": t,
                "kind": "storm",
                "hub": hub,
                "title": "Radiation storm detected",
                "text": (
                    f"{ambient.source} measured ambient radiation {ambient.value:.2f} over the pole. "
                    f"Forecast exposure in the next orbit for: {', '.join(exposed) or 'none'}. "
                    "Sending protective schedules to the fleet."
                ),
                "ambient": ambient.value,
            }
        )
    if ambient is not None and ambient.value < CLEAR and memory.storm_announced:
        memory.storm_announced = False
        decisions.append({"t": t, "kind": "clear", "hub": hub, "title": "All clear", "text": "Ambient radiation is back below the clear level. Services return home.", "ambient": ambient.value})

    for sat in SAT_IDS:
        spans = analyst.windows(ambient, env.ephemeris, sat, t)
        if sat == hub:
            continue
        if sat in HUBS and sat != hub:
            continue
        key = f"windows:{sat}"
        if memory.plans.get(key) != str(spans) or t % 3 == 0:
            memory.plans[key] = str(spans)
            send(world, env, scenario, t, {"kind": "schedule", "sender": hub, "receiver": sat, "windows": spans})

    for spec in SERVICES:
        host = placement.get(spec["id"])
        if host is None:
            continue
        job = next(item for item in world.jobs if item.id == spec["id"])
        service = {**spec, "period_load": job.load}
        pending = memory.plans.get(spec["id"])
        if pending and pending == host:
            memory.plans.pop(spec["id"], None)
            pending = None
        if pending:
            continue
        storm = ambient is not None and ambient.value >= analyst.STORM_LEVEL
        provisional = warning is not None and confirmed and not storm
        if host != spec["home"] and not provisional:
            home = spec["home"]
            home_ok = (
                ambient is not None
                and ambient.value < CLEAR
                and modes.get(home) in COMPUTING
                and loads.get(home, 0.0) + job.load <= capacity(home) + 1e-9
                and analyst.peak(ambient, env.ephemeris, home, t) < CAUTIOUS
            )
            if home_ok:
                memory.plans[spec["id"]] = home
                loads[home] = loads.get(home, 0.0) + job.load
                _instruct(world, env, scenario, t, hub, host, spec["id"], home, "return")
                decisions.append(
                    {
                        "t": t,
                        "kind": "return",
                        "hub": hub,
                        "service": spec["name"],
                        "tier": spec["tier"],
                        "host": host,
                        "target": home,
                        "title": f"Return {spec['name']} to {home}",
                        "text": f"All clear. {spec['name']} goes back from {host} to its home satellite {home}.",
                    }
                )
                continue
        if provisional and (spec["tier"] != "critical" or host != spec["home"]):
            continue
        if not (storm or provisional) or not analyst.windows(planning, env.ephemeris, host, t):
            continue
        options = analyst.candidates(service, host, t, planning, env.ephemeris, loads, modes)
        chosen = analyst.choose(options, host)
        source = "analyst"
        reason = analyst.explain(service, host, chosen, options, planning)
        if provisional:
            reason = (
                f"Pre-positioning before the protons arrive: the flare warning (p = {warning['p']:.2f}) "
                f"expects a storm within {warning['onsetHours'][2]:.0f} h, and {host} crosses the polar caps every orbit. "
                + reason
            )
        moves = [option for option in options if option["id"].startswith("move:")]
        if advisor and moves and state["advisor_calls"] < 3 and chosen["id"].startswith("move:"):
            state["advisor_calls"] += 1
            advice = advisor(
                {
                    "t": t,
                    "utc": env.series["utc"][t],
                    "service": spec["label"],
                    "tier": spec["tier"],
                    "host": host,
                    "ambient": ambient.value if ambient else None,
                    "ambientSource": ambient.source if ambient else None,
                    "options": [
                        {
                            "id": option["id"],
                            "description": option["label"],
                            "forecastServiceDowntimeMinutes": option["downtime"] * STEP_MINUTES,
                            "lostWorkRisk": option["lostRisk"],
                            "hardwareInterlockRisk": option["interlockRisk"],
                        }
                        for option in options
                    ],
                    "analystChoice": chosen["id"],
                }
            )
            if advice and any(option["id"] == advice.get("choice") and option["id"] != "no-action" for option in options):
                chosen = next(option for option in options if option["id"] == advice["choice"])
                reason = advice.get("reason") or reason
                source = advice.get("provider", "grok")
                state["advisor_latency"] = advice.get("latencyMs")
        if not chosen["id"].startswith("move:"):
            continue
        memory.plans[spec["id"]] = chosen["target"]
        loads[chosen["target"]] = loads.get(chosen["target"], 0.0) + job.load
        loads[host] = max(0.0, loads.get(host, 0.0) - job.load)
        _instruct(world, env, scenario, t, hub, host, spec["id"], chosen["target"], "handoff")
        decisions.append(
            {
                "t": t,
                "kind": "handoff",
                "hub": hub,
                "service": spec["name"],
                "tier": spec["tier"],
                "host": host,
                "target": chosen["target"],
                "title": f"Hand {spec['name']} from {host} to {chosen['target']}",
                "text": reason,
                "options": options,
                "chosen": chosen["id"],
                "source": source,
                "ambient": ambient.value if ambient else None,
            }
        )
    return decisions


def _instruct(world: World, env: Env, scenario: Scenario, t: int, hub: str, host: str, job_id: str, target: str, kind: str) -> None:
    if host == hub:
        node = world.nodes[hub]
        node.pending = [item for item in node.pending if item[0] != job_id] + [(job_id, target)]
        return
    send(world, env, scenario, t, {"kind": kind, "sender": hub, "receiver": host, "job": job_id, "target": target})


# ---------------------------------------------------------------------------
# Applying decisions and stepping the world


def _enter_hold(world: World, node: Node, planned: bool) -> None:
    for job in hosted(world, node.id):
        _lose(world, job)
        if job.status == "running":
            job.status = "paused"
    for job in world.jobs:
        if job.status == "migrating" and (job.node == node.id or job.migrate_to == node.id):
            job.status = "paused" if job.node == node.id or world.nodes[job.node].mode in HELD else "running"
            job.migrate_to, job.migrate_left = None, 0
    node.mode = "protected"
    node.planned = planned
    node.hold_after = False
    node.checkpoint_left = 0


def apply(world: World, t: int, intent: dict) -> None:
    for sat, action in intent["actions"].items():
        node = world.nodes[sat]
        if action == "checkpoint" and node.mode in COMPUTING:
            node.resume_mode, node.mode = node.mode, "checkpointing"
            node.checkpoint_left, node.hold_after = CHECKPOINT_STEPS, False
        elif action == "safe-hold" and node.mode in ACTIVE:
            node.planned = sat in intent["planned"]
            if node.mode == "checkpointing":
                node.hold_after = True
            elif unsaved(world, sat) > 1e-6:
                node.resume_mode, node.mode = node.mode, "checkpointing"
                node.checkpoint_left, node.hold_after = CHECKPOINT_STEPS, True
            else:
                _enter_hold(world, node, node.planned)
            _note(world, t, sat, "planned hold before forecast exposure" if node.planned else "safe-hold")
        elif action == "cautious" and node.mode == "normal":
            node.mode = "cautious"
        elif action == "relax" and node.mode == "cautious":
            node.mode = "normal"
        elif action == "resume" and node.mode == "protected":
            if node.locked and world.name != "ground":
                continue
            node.locked = False
            node.mode = "recovering"
            node.recover_left = RECOVER_PLANNED if node.planned else RECOVER_STEPS
            _note(world, t, sat, "resume")
    for job_id, target in intent["migrations"]:
        job = next(item for item in world.jobs if item.id == job_id)
        src = world.nodes[job.node]
        if job.status != "running" or src.mode not in COMPUTING or job.unsaved > 1e-6:
            continue
        if target == job.node or world.nodes[target].mode not in ACTIVE or not fits(world, target, job):
            continue
        job.status, job.migrate_to, job.migrate_left = "migrating", target, TRANSFER_STEPS
        world.metrics.handoffs += 1
        _note(world, t, job.node, f"hand {job.name} to {target}")


def interlock(world: World, env: Env, t: int) -> None:
    for sat, node in world.nodes.items():
        if env.true[t][sat] < SAFETY_LIMIT or node.mode in HELD:
            continue
        world.metrics.interlocks += 1
        _enter_hold(world, node, planned=False)
        node.locked = world.name == "ground"
        _note(world, t, sat, "hardware interlock")


def _hit(env: Env, t: int, sat: str) -> bool:
    risk = env.true[t][sat]
    return env.shock[t][sat] < risk * risk * HIT_SCALE


def _priority(job: Job) -> tuple:
    return (TIER_ORDER[job.tier], job.tasks[0].due if job.tasks else math.inf)


def _work(job: Job, budget: float, t: int) -> float:
    if not job.essential:
        job.work_done += budget
        job.unsaved += budget
        return budget
    used = 0.0
    while budget - used > 1e-9 and job.tasks:
        task = job.tasks[0]
        step = min(budget - used, task.left)
        task.left -= step
        job.unsaved += step
        used += step
        if task.left <= 1e-9:
            job.tasks.pop(0)
            job.unsaved = 0.0
            if t < task.due:
                job.met += 1
                job.work_done += job.work
            else:
                job.missed += 1
    return used


def execute(world: World, env: Env, t: int) -> None:
    metrics = world.metrics
    settled: set[str] = set()
    for job in world.jobs:
        if not job.essential:
            continue
        while job.tasks and job.tasks[0].due <= t:
            job.tasks.pop(0)
            job.missed += 1
            job.unsaved = 0.0
        if t >= job.offset and (t - job.offset) % job.period == 0:
            job.tasks.append(Task(t, t + job.period, job.work))

    for sat, node in world.nodes.items():
        if node.mode in ACTIVE and env.true[t][sat] >= 0.45 and hosted(world, sat):
            metrics.exposed += 1
        if node.mode == "checkpointing":
            metrics.overhead += 1
            if _hit(env, t, sat):
                node.uncorrectable += 1
                for job in hosted(world, sat):
                    _lose(world, job)
                if node.hold_after:
                    _enter_hold(world, node, node.planned)
                else:
                    node.mode = node.resume_mode
                    node.since_checkpoint = 0
                continue
            node.checkpoint_left -= 1
            if node.checkpoint_left <= 0:
                for job in hosted(world, sat):
                    job.unsaved = 0.0
                node.since_checkpoint = 0
                settled.add(sat)
                if node.hold_after:
                    _enter_hold(world, node, node.planned)
                else:
                    node.mode = node.resume_mode
        elif node.mode == "recovering":
            metrics.overhead += 1
            node.recover_left -= 1
            if node.recover_left <= 0:
                node.mode = "normal"
                node.since_checkpoint = 0
                for job in hosted(world, sat):
                    if job.status == "paused":
                        job.status = "running"

    for job in world.jobs:
        if job.status != "migrating":
            continue
        metrics.overhead += 1
        job.migrate_left -= 1
        if job.migrate_left > 0:
            continue
        job.node, job.migrate_to = job.migrate_to or job.node, None
        job.status = "paused" if world.nodes[job.node].mode in HELD else "running"

    for sat, node in world.nodes.items():
        if node.mode not in COMPUTING or sat in settled:
            continue
        active = [job for job in hosted(world, sat) if job.status == "running"]
        if not active:
            continue
        if _hit(env, t, sat):
            node.uncorrectable += 1
            for job in active:
                _lose(world, job)
            node.since_checkpoint = 0
            continue
        budget = (0.85 if node.mode == "cautious" else 1.0) - (ANALYST_LOAD if sat in world.analysts else 0.0)
        for job in sorted(active, key=_priority):
            budget -= _work(job, budget, t)
        node.since_checkpoint += 1
        if node.mode == "cautious":
            every = CAUTIOUS_CHECKPOINT_EVERY
        elif node.ready_until >= t:
            every = READY_CHECKPOINT_EVERY
        else:
            every = NORMAL_CHECKPOINT_EVERY
        if node.since_checkpoint >= every and unsaved(world, sat) > 1e-6:
            node.resume_mode, node.mode = node.mode, "checkpointing"
            node.checkpoint_left, node.hold_after = CHECKPOINT_STEPS, False

    for job in world.jobs:
        if not job.essential:
            continue
        up = job.status == "running" and world.nodes[job.node].mode in ACTIVE
        if env.series["stormStart"] <= t < env.series["stormEnd"]:
            job.storm_steps += 1
            job.storm_up += int(up)
        metrics.up_den += 1
        if up:
            metrics.up += 1
            if job.down_since is not None:
                metrics.interruptions.append(t - job.down_since)
                job.down_since = None
        elif job.down_since is None:
            job.down_since = t


# ---------------------------------------------------------------------------
# Scores and frames


def scores(world: World, t: int) -> dict:
    metrics = world.metrics
    services = [job for job in world.jobs if job.essential]
    open_gaps = [t + 1 - job.down_since for job in services if job.down_since is not None]
    gaps = metrics.interruptions + open_gaps
    return {
        "criticalMissed": sum(job.missed for job in services if job.tier == "critical"),
        "missed": sum(job.missed for job in services),
        "missedByService": {job.name: job.missed for job in services},
        "delivered": sum(job.met for job in services),
        "lost": round(metrics.lost, 1),
        "downtime": sum(gaps),
        "recovery": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
        "availability": round(metrics.up / metrics.up_den, 4) if metrics.up_den else 1.0,
        "interlocks": metrics.interlocks,
        "exposed": metrics.exposed,
        "overhead": metrics.overhead,
        "handoffs": metrics.handoffs,
        "archive": round(sum(job.work_done for job in world.jobs if not job.essential), 1),
        **people(world),
    }


def people(world: World) -> dict:
    """The same results in terms of what responders get: hours of emergency comms during the
    storm (from the first to the last >= 10 pfu sample) and deliveries made on time."""
    by_id = {job.id: job for job in world.jobs}
    comms, imagery, flood = by_id["comms"], by_id["imagery"], by_id["floodmap"]
    hours = STEP_MINUTES / 60
    return {
        "commsStormHours": round(comms.storm_steps * hours, 1),
        "commsOutageHours": round((comms.storm_steps - comms.storm_up) * hours, 1),
        "commsAvailability": round(comms.storm_up / comms.storm_steps, 4) if comms.storm_steps else 1.0,
        "commsRelaysOnTime": comms.met,
        "commsRelaysDue": comms.met + comms.missed,
        "imageryOnTime": imagery.met,
        "imageryDue": imagery.met + imagery.missed,
        "floodMapsOnTime": flood.met,
        "floodMapsDue": flood.met + flood.missed,
    }


def _mode_label(world: World, node: Node) -> tuple[str, str]:
    if any(job.status == "migrating" and job.migrate_to == node.id for job in world.jobs):
        return "Receiving service", "warn"
    if any(job.status == "migrating" and job.node == node.id for job in world.jobs):
        return "Handing off", "warn"
    if node.mode == "checkpointing":
        return ("Checkpoint, then pause" if node.hold_after else "Checkpointing"), "warn"
    if node.mode == "protected":
        if node.locked:
            return "Safe mode, awaiting ground", "bad"
        return ("Planned pause" if node.planned else "Safe mode"), "warn" if node.planned else "bad"
    if node.mode == "recovering":
        return "Recovering", "warn"
    if node.mode == "cautious":
        return "Cautious", "warn"
    return "Running", "ok"


def _frame(worlds: dict[str, World], env: Env, t: int, arrivals: list[dict]) -> dict:
    series = env.series
    measured = series["measured"][t]
    sats = {}
    for sat in SAT_IDS:
        eph = env.ephemeris[t][sat]
        sats[sat] = {
            "risk": round(env.fused[t][sat], 3),
            "truth": round(env.true[t][sat], 3),
            "exposure": eph["exposure"],
            "lat": eph["lat"],
            "lon": eph["lon"],
            "errors": env.errors[t][sat],
            "contact": eph["contact"],
        }
    fleets = {}
    for name, world in worlds.items():
        nodes = {}
        for sat, node in world.nodes.items():
            label, tone = _mode_label(world, node)
            services = [job.name for job in world.jobs if job.essential and (
                (job.node == sat and job.status != "migrating") or (job.status == "migrating" and job.migrate_to == sat)
            )]
            nodes[sat] = {"label": label, "tone": tone, "services": services, "mode": node.mode}
        fleets[name] = {"nodes": nodes, "scores": scores(world, t), "analysts": sorted(world.analysts)}
    return {
        "t": t,
        "utc": series["utc"][t],
        "pfu": None if measured is None else round(measured, 3),
        "gap": measured is None,
        "sats": sats,
        "fleets": fleets,
        "links": [
            {"from": msg["sender"], "to": msg["receiver"], "kind": msg["kind"]}
            for msg in arrivals
            if msg["kind"] != "report"
        ],
    }


PRIMARY = "lifeline_ew"


def simulate(scenario: Scenario, advisor=None, keep_frames: bool = True) -> dict:
    env = build_env(scenario)
    worlds = {name: new_world(name) for name in STRATEGIES}
    states = {name: {"advisor_calls": 0, "advisor_latency": None} for name in ("local", "lifeline", "lifeline_ml", "lifeline_ew")}
    frames = []
    decisions = {"lifeline": [], "lifeline_ew": []}
    for t in range(env.ticks):
        arrivals = {name: deliver(world, t) for name, world in worlds.items()}
        intents = {
            "ground": ground_control(worlds["ground"], env, t),
            "threshold": threshold_control(worlds["threshold"], env, t),
            "local": smart_control(worlds["local"], env, scenario, t, False, None, states["local"]),
            "lifeline": smart_control(worlds["lifeline"], env, scenario, t, True, advisor, states["lifeline"]),
            "lifeline_ml": smart_control(worlds["lifeline_ml"], env, scenario, t, True, None, states["lifeline_ml"]),
            "lifeline_ew": smart_control(worlds["lifeline_ew"], env, scenario, t, True, advisor, states["lifeline_ew"]),
        }
        for name in decisions:
            for item in intents[name].get("decisions", []):
                item["utc"] = env.series["utc"][t]
                decisions[name].append(item)
        for name, world in worlds.items():
            apply(world, t, intents[name])
            interlock(world, env, t)
            execute(world, env, t)
        if keep_frames or t == env.ticks - 1:
            frames.append(_frame(worlds, env, t, arrivals[PRIMARY]))
    series = env.series
    primary = states[PRIMARY]
    return {
        "window": scenario.window,
        "mission": mission(scenario.window),
        "ticks": env.ticks,
        "stepMinutes": STEP_MINUTES,
        "stormStart": series["stormStart"],
        "stormEnd": series["stormEnd"],
        "peak": series["peak"],
        "frames": frames,
        "primary": PRIMARY,
        "decisions": decisions[PRIMARY],
        "decisionsProtonsOnly": decisions["lifeline"],
        "xray": series["xray"],
        "flares": env.flares,
        "earlyWarning": earlywarning.summary(),
        "logs": {name: world.log[-400:] for name, world in worlds.items()},
        "summary": {name: frames[-1]["fleets"][name]["scores"] for name in STRATEGIES},
        "strategies": STRATEGY_NAMES,
        "satellites": {
            sat: {"name": spec["name"], "role": spec["role"], "orbit": spec["orbit"].label, "about": spec["about"]}
            for sat, spec in SATELLITES.items()
        },
        "services": [{key: spec[key] for key in ("id", "name", "label", "tier", "home")} for spec in SERVICES],
        "advisorCalls": primary["advisor_calls"],
        "advisorLatencyMs": primary["advisor_latency"],
        "messages": {name: {"sent": world.metrics.messages, "dropped": world.metrics.dropped} for name, world in worlds.items()},
    }


# ---------------------------------------------------------------------------
# Evaluation across storms and seeds

SCORE_KEYS = (
    "criticalMissed", "missed", "lost", "downtime", "recovery", "availability", "interlocks", "exposed", "handoffs", "archive",
    "commsStormHours", "commsOutageHours", "commsAvailability", "imageryOnTime", "imageryDue", "floodMapsOnTime", "floodMapsDue",
)
HEADLINE_KEYS = ("criticalMissed", "missed", "lost", "downtime", "interlocks", "commsOutageHours", "commsStormHours")


def aggregate(results: list[dict]) -> dict:
    out = {}
    for name in STRATEGIES:
        samples = [result["summary"][name] for result in results]
        out[name] = {key: round(sum(sample[key] for sample in samples) / len(samples), 3) for key in SCORE_KEYS}
    return out


def headline(rows: list[dict], skip: tuple[str, ...] = ("quiet2024",)) -> dict:
    totals = {name: {key: 0.0 for key in HEADLINE_KEYS} for name in STRATEGIES}
    for row in rows:
        if row["window"] in skip:
            continue
        for name in STRATEGIES:
            for key in HEADLINE_KEYS:
                totals[name][key] += row["scores"][name][key]
    def pct(target: str, base: str, key: str):
        if totals[base][key] == 0:
            return None
        return round(100 * (totals[target][key] - totals[base][key]) / totals[base][key], 1)

    change = {
        target: {base: {key: pct(target, base, key) for key in HEADLINE_KEYS} for base in STRATEGIES if base != target}
        for target in ("lifeline", "lifeline_ml", "lifeline_ew")
    }
    return {"totals": {name: {k: round(v, 1) for k, v in row.items()} for name, row in totals.items()}, "change": change}


def evaluate(seeds: int = 5, latency: int = 1, loss: float = 0.0) -> list[dict]:
    rows = []
    for window in window_ids():
        results = [
            simulate(Scenario(window, seed, latency, loss), keep_frames=False) for seed in range(1, seeds + 1)
        ]
        rows.append(
            {
                "window": window,
                "label": MISSIONS.get(window, {}).get("label", window),
                "scores": aggregate(results),
            }
        )
    return rows
