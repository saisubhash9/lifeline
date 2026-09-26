"""Physics, no-look-ahead, messaging, protection, and API behavior for Lifeline."""

from fastapi.testclient import TestClient

from backend import grok, sim
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
    assert set(body["summary"]) == {"ground", "threshold", "local", "lifeline"}
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
    assert captured["scores"]["Lifeline"]["missedEmergencyCommsDeadlines"] == run["summary"]["lifeline"]["criticalMissed"]
