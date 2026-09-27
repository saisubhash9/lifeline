"""Physics, no-look-ahead, messaging, protection, and API behavior for Lifeline."""

from fastapi.testclient import TestClient

import csv
import json
from pathlib import Path

from backend import earlywarning, grok, sim
from backend.app import app
from backend.fleet import HUBS, ORBITS, SIMPLE
from backend.sim import Scenario, aggregate, simulate
from backend.space import LEAK, pfu_to_risk, risk_to_pfu, track

client = TestClient(app)


def test_risk_scale_round_trips():
    for pfu in (3.0, 10.0, 55.0, 100.0, 1000.0, 42000.0):
        assert abs(risk_to_pfu(pfu_to_risk(pfu)) - pfu) / pfu < 1e-6


def test_polar_satellites_cross_caps_and_primary_hub_is_shielded():
    rows = track(ORBITS, 288)
    for sat in ("S1", "S2"):
        exposed = sum(row[sat]["exposure"] > 0.5 for row in rows) / len(rows)
        assert 0.2 < exposed < 0.45
    assert max(row["H1"]["exposure"] for row in rows) <= LEAK + 1e-9
    assert any(row["S1"]["contact"] for row in rows)


def test_decisions_do_not_depend_on_future_flux():
    base = sim.build_env(Scenario("oct2024", 3))
    cut = 600
    original = sim.load_series

    def altered(window):
        series = original(window)
        series["held"] = series["held"][:cut] + [value * 50 for value in series["held"][cut:]]
        return series

    first = simulate(Scenario("oct2024", 3))["frames"]
    sim.load_series = altered
    try:
        second = simulate(Scenario("oct2024", 3))["frames"]
    finally:
        sim.load_series = original
    assert base.ticks == len(first)
    for t in range(cut):
        assert first[t]["fleets"] == second[t]["fleets"], t


def test_messages_arrive_after_latency_and_total_loss_means_fallback():
    result = simulate(Scenario("oct2024", 2, latency=3, loss=0.0))
    assert result["decisions"]
    silent = simulate(Scenario("oct2024", 2, latency=1, loss=1.0))
    assert silent["summary"]["lifeline"]["handoffs"] == 0
    assert silent["messages"]["lifeline"]["dropped"] == silent["messages"]["lifeline"]["sent"]


def test_planned_pauses_lose_no_unsaved_work():
    lost = []
    original = sim._enter_hold

    def spy(world, node, planned):
        if planned and world.name == "lifeline":
            lost.append(sum(job.unsaved for job in sim.hosted(world, node.id)))
        original(world, node, planned)

    sim._enter_hold = spy
    try:
        simulate(Scenario("oct2024", 2))
    finally:
        sim._enter_hold = original
    assert lost
    assert max(lost) < 1e-6


def test_lifeline_protects_emergency_comms_in_the_milton_week():
    scores = aggregate([simulate(Scenario("oct2024", seed), keep_frames=False) for seed in (1, 2, 3)])
    for base in ("ground", "threshold", "local"):
        assert scores["lifeline"]["criticalMissed"] < scores[base]["criticalMissed"]
        assert scores["lifeline"]["downtime"] < scores[base]["downtime"]
    assert scores["lifeline"]["interlocks"] == 0


def test_quiet_control_needs_no_handoffs():
    summary = simulate(Scenario("quiet2024", 1), keep_frames=False)["summary"]
    for name in summary:
        assert summary[name]["missed"] == 0
    assert summary["lifeline"]["handoffs"] == 0


def test_invalid_grok_choice_falls_back_to_the_analyst():
    calls = []

    def advisor(payload):
        calls.append(payload)
        return {"choice": "launch-the-satellite-into-the-sun", "reason": "nonsense"}

    result = simulate(Scenario("oct2024", 7), advisor=advisor)
    assert calls
    assert all("forecastServiceDowntimeMinutes" in option for option in calls[0]["options"])
    assert all(item.get("source", "analyst") == "analyst" for item in result["decisions"] if item["kind"] == "handoff")


def test_hubs_and_simple_roles():
    assert set(HUBS) == {"H1", "H2"}
    assert set(SIMPLE) == {"S1", "S2", "S3"}


def test_api_default_run_is_milton():
    events = client.get("/api/events").json()
    assert events["events"][0]["id"] == "oct2024"
    body = client.post("/api/run", json={}).json()
    assert "Milton" in body["mission"]["title"]
    assert set(body["summary"]) == {"ground", "threshold", "local", "lifeline", "lifeline_ml", "lifeline_ew"}
    assert body["primary"] == "lifeline_ew"
    assert len(body["xray"]) == body["ticks"]
    frame = body["frames"][100]
    assert set(frame["sats"]) == {"H1", "H2", "S1", "S2", "S3"}
    assert "fleets" in frame and "links" in frame


def test_voice_session_requires_a_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert client.post("/api/voice/session").status_code == 503


def test_debrief_facts_match_the_run(monkeypatch):
    captured = {}

    def fake(facts):
        captured.update(facts)
        return {"text": "ok", "latencyMs": 1.0, "model": "test"}

    monkeypatch.setenv("XAI_API_KEY", "test")
    monkeypatch.setattr(grok, "debrief", fake)
    body = client.post("/api/debrief", json={}).json()
    assert body["text"] == "ok"
    run = client.post("/api/run", json={}).json()
    for key, name in run["strategies"].items():
        assert captured["scores"][name]["missedEmergencyCommsDeadlines"] == run["summary"][key]["criticalMissed"]
    assert any("Flare warning" in event for event in captured["lifelineEvents"])


def test_screener_runtime_matches_training_export():
    spec = earlywarning.model()
    rows = {row["peak_utc"]: row for row in csv.DictReader(Path("data/flares/training.csv").open())}
    warnings = json.loads(Path("data/flares/test_warnings.json").read_text())
    assert len(warnings) >= 50
    for event in warnings[:40]:
        row = rows[event["peakUtc"]]
        has = int(row["has_location"])
        values = earlywarning.features(
            10 ** float(row["log_peak"]), 10 ** float(row["log_fluence"]), 10 ** float(row["log_rise_min"]),
            10 ** float(row["log_background"]), float(row["lon"]) if has else None, float(row["lat"]) if has else None,
        )
        assert abs(earlywarning.probability(values) - event["p"]) < 1e-3
        assert event["p"] >= spec["threshold"]
    assert spec["metrics"]["stage1"]["tp"] == spec["metrics"]["testStorms"]


def test_flare_warnings_never_precede_the_xray_peak():
    result = simulate(Scenario("oct2024", 7), keep_frames=False)
    utc = sim.load_series("oct2024")["utc"]
    warnings = [item for item in result["decisions"] if item["kind"] == "flare-warning"]
    assert warnings
    for item in warnings:
        flare = next(f for f in result["flares"] if f["t"] == item["t"])
        assert utc[item["t"]] >= flare["peakUtc"]


def test_quiet_control_has_no_flare_warnings():
    result = simulate(Scenario("quiet2024", 1), keep_frames=False)
    assert not [item for item in result["decisions"] if item["kind"] == "flare-warning"]


def _fake_flare(utc, verification=None):
    return [{"flare_id": 1, "peakUtc": utc[200], "class": "X2.0", "lon": 40.0, "lat": 10.0, "p": 0.3, "warn": True, "sep": 0,
             "onsetHours": [1.0, 3.0, 6.0], "t": 200, "onsetSteps": [12, 36, 72], "verification": verification}]


def test_unverified_false_alarm_stands_down_and_comms_returns_home(monkeypatch):
    utc = sim.load_series("quiet2024")["utc"]
    monkeypatch.setattr(earlywarning, "warnings_for", lambda window, utc_list: _fake_flare(utc))
    result = simulate(Scenario("quiet2024", 1), keep_frames=False)
    kinds = [item["kind"] for item in result["decisions"]]
    assert "flare-warning" in kinds and "stand-down" in kinds
    handoffs = [item for item in result["decisions"] if item["kind"] == "handoff"]
    assert handoffs and handoffs[0]["service"] == "Emergency comms"
    assert "return" in kinds
    assert result["summary"]["lifeline_ew"]["criticalMissed"] == 0


def test_grok_rejection_prevents_pre_positioning(monkeypatch):
    utc = sim.load_series("quiet2024")["utc"]
    reject = {"verdict": "reject", "probability": 3, "threshold": 20, "reason": "No coherent CME front.", "cmeVisible": "no", "cmeExtent": "none", "model": "test"}
    monkeypatch.setattr(earlywarning, "warnings_for", lambda window, utc_list: _fake_flare(utc, reject))
    result = simulate(Scenario("quiet2024", 1), keep_frames=False)
    kinds = [item["kind"] for item in result["decisions"]]
    assert "grok-verification" in kinds
    verification = next(item for item in result["decisions"] if item["kind"] == "grok-verification")
    assert verification["t"] == 200 + earlywarning.VERIFY_DELAY_STEPS and verification["verdict"] == "reject"
    assert "handoff" not in kinds
    assert result["summary"]["lifeline_ew"]["handoffs"] == 0
    assert result["summary"]["lifeline_ml"]["handoffs"] > 0


def test_sun_watch_is_served_from_cache_without_a_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    body = client.post("/api/sun", json={"window": "oct2024", "refresh": True}).json()
    assert body["images"]["watch"]["aia193"].endswith(".jpg")
    assert body["assessment"]["outlook"] in ("quiet", "watch", "warning")
    assert body["warning"]
