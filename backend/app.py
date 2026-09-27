"""Lifeline API and dashboard."""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from backend import grok  # noqa: E402
from backend.fleet import SATELLITES, SERVICES  # noqa: E402
from backend.sim import STRATEGY_NAMES, Scenario, evaluate, headline, simulate  # noqa: E402
from backend.space import WINDOWS, describe, window_ids  # noqa: E402

WEB = ROOT / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Precompute the default replay locally so the first page load is fast (skipped on Vercel,
    where background threads do not outlive a request)."""
    if not os.environ.get("VERCEL"):
        threading.Thread(target=lambda: (_cached_run("oct2024", 7, 1, 0.0), _cached_eval(1, 0.0)), daemon=True).start()
    yield


app = FastAPI(title="Lifeline", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.mount("/assets", StaticFiles(directory=WEB), name="assets")
SUN = ROOT / "data" / "sun"
if SUN.exists():
    app.mount("/sun", StaticFiles(directory=SUN), name="sun")
VERIFY = ROOT / "data" / "verify"
if VERIFY.exists():
    app.mount("/verify", StaticFiles(directory=VERIFY), name="verify")
_sun_live: dict[str, dict] = {}


class RunRequest(BaseModel):
    window: str = "oct2024"
    seed: int = Field(7, ge=1, le=99)
    latency: int = Field(1, ge=1, le=6)
    loss: float = Field(0.0, ge=0.0, le=0.6)
    grok: bool = False


def _scenario(body: RunRequest) -> Scenario:
    if body.window not in WINDOWS:
        raise HTTPException(404, "unknown storm window")
    return Scenario(body.window, body.seed, body.latency, body.loss)


@lru_cache(maxsize=24)
def _cached_run(window: str, seed: int, latency: int, loss: float) -> dict:
    return simulate(Scenario(window, seed, latency, loss))


DEFAULT_EVAL = ROOT / "data" / "models" / "evaluation_default.json"


@lru_cache(maxsize=8)
def _cached_eval(latency: int, loss: float) -> dict:
    # The default settings are precomputed (scripts/precompute_eval.py) so a cold serverless
    # start answers instantly; other settings are computed on demand (about 15 s).
    if (latency, loss) == (1, 0.0) and DEFAULT_EVAL.exists():
        return json.loads(DEFAULT_EVAL.read_text())
    rows = evaluate(seeds=5, latency=latency, loss=loss)
    return {"rows": rows, "headline": headline(rows), "seeds": 5, "strategies": STRATEGY_NAMES}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/events")
def events() -> dict:
    return {"events": [describe(name) for name in window_ids()], "grok": grok.available()}


def _gzip_json(payload: dict) -> Response:
    """A replay is 8-10 MB of JSON (about 0.3 MB gzipped). Always compress it so the response
    stays far below serverless body limits (4.5 MB on Vercel) whatever the request headers say."""
    import gzip

    body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), compresslevel=6)
    return Response(body, media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})


@app.post("/api/run")
def run(body: RunRequest) -> Response:
    scenario = _scenario(body)
    if body.grok and grok.available():
        result = simulate(scenario, advisor=grok.analyst_choice)
        result["grok"] = True
        return _gzip_json(result)
    result = dict(_cached_run(scenario.window, scenario.seed, scenario.latency, scenario.loss))
    result["grok"] = False
    if body.grok:
        result["warning"] = "No XAI_API_KEY is set, so the deterministic analyst decided."
    return _gzip_json(result)


@app.post("/api/evaluate")
def evaluate_runs(body: RunRequest) -> dict:
    return _cached_eval(body.latency, round(body.loss, 2))


class SunRequest(BaseModel):
    window: str = "oct2024"
    refresh: bool = False


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


@app.post("/api/sun")
def sun(body: SunRequest) -> dict:
    """Grok's Sun watch for a storm window: real SDO images plus a cached (or live) briefing."""
    images = _read_json(SUN / "images.json").get(body.window)
    if images is None:
        raise HTTPException(404, "no solar images for this window")
    assessment = _sun_live.get(body.window) or _read_json(SUN / "assessments.json").get(body.window)
    warning = None
    if body.refresh:
        if not grok.available():
            warning = "No XAI_API_KEY is set; showing the cached briefing."
        else:
            fresh = grok.sun_watch(
                [("SDO AIA 193 corona", SUN / images["watch"]["aia193"]), ("SDO HMI magnetogram", SUN / images["watch"]["hmi"])],
                images["watchUtc"],
            )
            if fresh:
                assessment = _sun_live[body.window] = {**fresh, "utc": images["watchUtc"], "live": True}
            else:
                warning = "Grok did not return a briefing; showing the cached one."
    return {"images": images, "assessment": assessment, "evaluation": _read_json(SUN / "eval.json").get("summary"), "warning": warning}


@app.post("/api/voice/session")
def voice_session() -> dict:
    session = grok.voice_session()
    if session is None:
        raise HTTPException(503, "Grok Voice is unavailable: set XAI_API_KEY in .env.")
    return session


def debrief_facts(result: dict) -> dict:
    minutes = result["stepMinutes"]
    names = result["strategies"]
    frames = result["frames"]
    services = {item["name"]: item["label"] for item in result["services"]}
    moves = [
        f"{item['utc']}: {item['title']}"
        for item in result["decisions"]
        if item["kind"] in ("flare-warning", "grok-verification", "stand-down", "handoff", "return", "storm", "takeover", "clear")
    ][:12]
    scores = {}
    for name, value in result["summary"].items():
        scores[names[name]] = {
            "missedEmergencyCommsDeadlines": value["criticalMissed"],
            "missedDeadlinesByService": {services.get(key, key): count for key, count in value["missedByService"].items()},
            "serviceDowntimeHours": round(value["downtime"] * minutes / 60, 1),
            "meanMinutesToRestore": round(value["recovery"] * minutes),
            "hardwareInterlocks": value["interlocks"],
            "lostWorkUnits": value["lost"],
            "serviceHandoffs": value["handoffs"],
            "emergencyCommsAvailableDuringStormPct": round(value["commsAvailability"] * 100),
            "emergencyCommsOutageHoursDuringStorm": value["commsOutageHours"],
            "imageryDeliveredOnTime": f"{value['imageryOnTime']} of {value['imageryDue']}",
            "floodMapsDeliveredOnTime": f"{value['floodMapsOnTime']} of {value['floodMapsDue']}",
        }
    return {
        "event": result["mission"],
        "window": f"{frames[0]['utc']} to {frames[-1]['utc']}",
        "fleet": {sat: f"{spec['name']}: {spec['about']}" for sat, spec in SATELLITES.items()},
        "services": [f"{spec['label']} ({spec['tier']}), home {spec['home']}" for spec in SERVICES],
        "lifelineEvents": moves,
        "earlyWarningCascade": {
            "stage1": result["earlyWarning"]["model"],
            "stage1HeldOutAuc": result["earlyWarning"]["metrics"]["testAuc"],
            "stage2": "Grok vision checks SDO and LASCO coronagraph images for a wide CME",
            "cascadeHeldOut": (result["earlyWarning"].get("cascade") or {}).get("cascade"),
        },
        "scores": scores,
        "note": "All outcomes are simulator outputs; faults, workloads, and satellites are simulated.",
    }


@app.post("/api/debrief")
def run_debrief(body: RunRequest) -> dict:
    if not grok.available():
        return {"text": None, "warning": "No XAI_API_KEY is set."}
    scenario = _scenario(body)
    result = _cached_run(scenario.window, scenario.seed, scenario.latency, scenario.loss)
    facts = debrief_facts(result)
    report = grok.debrief(facts)
    if report is None:
        return {"text": None, "warning": "Grok did not return a debrief.", "facts": facts}
    return {**report, "facts": facts}
