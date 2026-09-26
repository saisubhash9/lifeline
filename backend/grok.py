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
    "Plain text, no headings, at most 170 words, four short paragraphs: the situation, what "
    "Lifeline did and when, how it compared for emergency comms and other services, and one limitation."
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
