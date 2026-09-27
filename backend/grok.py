"""Grok API calls: the in-flight analyst's choice, the mission debrief, and voice tokens.

Scores always come from the simulator. A model answer is used only after local
validation, and every call has a deterministic fallback.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

import certifi

API = "https://api.x.ai/v1"
MODEL = os.environ.get("XAI_MODEL", "grok-4")
VOICE_MODEL = "grok-voice-latest"
_SSL = ssl.create_default_context(cafile=certifi.where())


def available() -> bool:
    return bool(os.environ.get("XAI_API_KEY"))


def _post(path: str, body: dict, timeout: float = 45) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_SSL) as response:
        return json.loads(response.read().decode())


def _chat(system: str, user: dict, timeout: float = 45) -> tuple[str, float]:
    started = time.perf_counter()
    payload = _post(
        "/chat/completions",
        {
            "model": os.environ.get("XAI_MODEL", MODEL),
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
        },
        timeout,
    )
    text = (payload["choices"][0]["message"].get("content") or "").strip()
    return text, round((time.perf_counter() - started) * 1000, 1)


def _json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


ANALYST_PROMPT = (
    "You are the in-flight analyst running on a compute-capable satellite (a remote API call "
    "emulates it). A solar radiation storm is under way. You get one service at risk, the "
    "satellite hosting it, the measured ambient radiation, and candidate options with forecast "
    "outcomes computed from planned orbits: downtime in five-minute steps, lost-work risk, and "
    "interlock risk. Critical services outrank essential ones. Pick exactly one option id that "
    "is not 'no-action'. Reply with JSON only: "
    '{"choice": "<option id>", "reason": "<one or two plain sentences for an operator, in minutes, naming satellites; no field names>"}'
)


def analyst_choice(payload: dict) -> dict | None:
    if not available():
        return None
    try:
        text, latency = _chat(ANALYST_PROMPT, payload)
        data = _json(text)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    return {"choice": str(data.get("choice", "")), "reason": str(data.get("reason", "")), "latencyMs": latency, "provider": "grok"}


DEBRIEF_PROMPT = (
    "You write a short morning-after debrief for satellite operators and the emergency-response "
    "coordinators who rely on their services. Use only the JSON facts; quote numbers exactly and "
    "invent none. It is a what-if simulation: real events set the dates and radiation, but the "
    "satellites and outcomes are simulated, so never claim real satellites failed or succeeded. "
    "Plain text, no headings, at most 170 words, four short paragraphs: the situation for the people on the "
    "ground, what Lifeline did and when, what it meant for responders (hours of emergency comms kept, imagery "
    "and flood maps delivered on time) compared with the other approaches, and one limitation."
)


def debrief(facts: dict) -> dict | None:
    if not available():
        return None
    try:
        text, latency = _chat(DEBRIEF_PROMPT, facts)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    return {"text": text, "latencyMs": latency, "model": os.environ.get("XAI_MODEL", MODEL)} if text else None


def voice_session(seconds: int = 600) -> dict | None:
    """Mint a short-lived client secret so the browser can open the realtime socket
    without ever seeing the API key."""
    if not available():
        return None
    try:
        data = _post("/realtime/client_secrets", {"expires_after": {"seconds": seconds}}, 20)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    return {
        "token": data["value"],
        "expiresAt": data.get("expires_at"),
        "url": f"wss://api.x.ai/v1/realtime?model={VOICE_MODEL}",
        "model": VOICE_MODEL,
    }


SUN_PROMPT = (
    "You are the space-weather forecaster for a satellite fleet. You are given real full-disk images of "
    "the Sun from NASA SDO: an AIA 193 Å coronal image and an HMI line-of-sight magnetogram (white and "
    "black are opposite magnetic polarities). Solar north is up and solar east is on the LEFT, so regions "
    "on the right (western) half are magnetically better connected to Earth for proton storms. Use only "
    "what you can see. Identify up to three notable active regions and give your 48-hour outlook. Reply "
    "with JSON only: {\"regions\": [{\"position\": \"e.g. N15 near disk center / south-east limb\", "
    "\"hemisphere\": \"east|west|center\", \"complexity\": \"simple|moderate|complex\", \"flare_risk\": "
    "\"low|moderate|high\"}], \"outlook_48h\": \"quiet|watch|warning\", \"earth_connected_risk\": "
    "\"low|moderate|high\", \"flare_probability_48h\": <integer 0-100: chance of an M5-or-stronger flare "
    "anywhere on the visible disk in the next 48 hours>, \"briefing\": \"two or three sentences for satellite operators\"}"
)


def _image_uri(path) -> str:
    import base64
    from pathlib import Path

    data = Path(path).read_bytes()
    kind = "jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "png"
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


def _percent(value) -> int | None:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def sun_watch(images: list[tuple[str, str]], when_utc: str) -> dict | None:
    """Grok vision reads SDO images [(label, path)] and returns a structured flare-risk briefing."""
    if not available():
        return None
    content = [{"type": "input_image", "image_url": _image_uri(path), "detail": "high"} for _label, path in images]
    labels = ", ".join(label for label, _path in images)
    content.append({"type": "input_text", "text": f"Images ({labels}) taken at {when_utc}. {SUN_PROMPT}"})
    model_name = os.environ.get("XAI_VISION_MODEL", "grok-4.7")
    started = time.perf_counter()
    try:
        payload = _post("/responses", {"model": model_name, "input": [{"role": "user", "content": content}]}, timeout=180)
        text = ""
        for item in payload.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text += part.get("text", "")
        data = _json(text)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    outlook = str(data.get("outlook_48h", "watch")).lower()
    return {
        "regions": data.get("regions", [])[:3],
        "outlook": outlook if outlook in ("quiet", "watch", "warning") else "watch",
        "earthConnectedRisk": str(data.get("earth_connected_risk", "moderate")).lower(),
        "flareProbability": _percent(data.get("flare_probability_48h")),
        "briefing": str(data.get("briefing", "")),
        "model": model_name,
        "latencyMs": round((time.perf_counter() - started) * 1000, 1),
    }


VERIFY_PROMPT = (
    "You are the second stage of a proton-storm early-warning system for a satellite fleet. A statistical "
    "screener flagged this solar flare as a possible source of a >=10 pfu solar energetic particle (proton) "
    "storm. The screener is tuned to miss almost nothing, so only about 1 in 20 flagged flares actually causes "
    "a storm. Estimate how likely THIS flare is to cause one.\n"
    "The image has four labeled panels of real data (solar north up, east left):\n"
    "  A: SDO AIA 131 A at the flare peak.\n"
    "  B: SDO AIA 193 A difference, 30 min after peak minus before the flare. The flare's own brightening "
    "always shows up; what matters is large dark areas of coronal dimming or a bright wave front sweeping "
    "across the disk.\n"
    "  C: SOHO LASCO C2 coronagraph about 75 min after the peak.\n"
    "  D: LASCO C2 difference, after minus before. Streamers drift and noise leaves streaks in every "
    "difference image. Only a coherent, bright, expanding front spanning a wide angle (especially a halo or "
    "partial halo ring around the occulter) indicates a fast, wide coronal mass ejection, the usual driver of "
    "proton storms. Narrow jets and streamer changes rarely cause storms. Panels may say 'no data'.\n"
    "Be calibrated: without clear wide-CME evidence your probability should stay near the base rate. "
    "Reply with JSON only: {\"eruption_signs\": [\"...\"], \"cme_visible\": \"yes|no|unclear\", "
    "\"cme_extent\": \"halo|partial halo|narrow|none|unknown\", \"storm_probability\": <integer 0-100>, "
    "\"reason\": \"one or two sentences\"}"
)


def verify_warning(image_path, facts: dict) -> dict | None:
    """Stage 2: Grok vision checks a screened flare for eruption and CME signatures."""
    if not available():
        return None
    model_name = os.environ.get("XAI_VISION_MODEL", "grok-4.7")
    content = [
        {"type": "input_image", "image_url": _image_uri(image_path), "detail": "high"},
        {"type": "input_text", "text": f"Triggering data: {json.dumps(facts)}\n{VERIFY_PROMPT}"},
    ]
    started = time.perf_counter()
    try:
        payload = _post("/responses", {"model": model_name, "input": [{"role": "user", "content": content}]}, timeout=240)
        text = "".join(
            part.get("text", "")
            for item in payload.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        data = _json(text)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None
    return {
        "probability": _percent(data.get("storm_probability")),
        "cmeVisible": str(data.get("cme_visible", "unclear")).lower(),
        "cmeExtent": str(data.get("cme_extent", "unknown")).lower(),
        "eruptionSigns": [str(sign) for sign in data.get("eruption_signs", [])][:5],
        "reason": str(data.get("reason", "")),
        "model": model_name,
        "latencyMs": round((time.perf_counter() - started) * 1000, 1),
    }
