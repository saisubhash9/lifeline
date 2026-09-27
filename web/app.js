// Lifeline dashboard: replay, map, four fleets, copilot, evaluation.

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

const STRATEGY_ORDER = ["ground", "threshold", "local", "lifeline", "lifeline_ew"];
const EVAL_ORDER = ["ground", "threshold", "local", "lifeline", "lifeline_ml", "lifeline_ew"];
const STRATEGY_ABOUT = {
  ground: "Onboard safe mode only. The ground reads telemetry during station passes and uplinks commands on a later pass.",
  threshold: "Every satellite follows fixed onboard rules on its own sensor. No sharing.",
  local: "Hubs forecast and protect themselves; simple satellites keep fixed rules. Nothing is shared.",
  lifeline: "Hubs forecast the whole fleet from measured protons, send protective schedules, and hand services to shielded peers.",
  lifeline_ml: "Lifeline plus the ML screener alone: every flagged flare moves critical services.",
  lifeline_ew: "Lifeline plus the early-warning cascade: the ML screener flags flares, Grok checks the images for a wide CME, and only confirmed warnings move critical services before protons arrive.",
};
const SAT_COLORS = { H1: "#b99cff", H2: "#d7c6ff", S1: "#e2b15a", S2: "#8eb4ff", S3: "#7fe0b8" };
const ANNOUNCE_KINDS = new Set(["flare-warning", "grok-verification", "stand-down", "storm", "handoff", "takeover", "clear", "return"]);
const primary = () => (state.run && state.run.primary) || "lifeline";

const state = {
  events: [],
  run: null,
  t: 0,
  playing: false,
  timer: null,
  speed: 12,
  token: 0,
  voice: null,
  announced: new Set(),
  sun: null,
  sunLayer: "aia193",
  chartBase: "",
  capCells: null,
};

// ---------------------------------------------------------------------------
// Helpers

function payload() {
  return {
    window: $("window").value,
    seed: Number($("seed").value) || 7,
    latency: Number($("latency").value),
    loss: Number($("loss").value),
    grok: $("grok-analyst").checked,
  };
}

function utcLabel(utc) {
  return utc ? utc.replace("T", " ").replace(":00Z", " UTC").replace("Z", " UTC") : "—";
}

function minutes(steps) {
  const total = Math.round(steps * (state.run ? state.run.stepMinutes : 5));
  if (total < 60) return `${total} min`;
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

function sScale(pfu) {
  if (pfu == null) return "no measurement";
  if (pfu >= 100000) return "S5";
  if (pfu >= 10000) return "S4";
  if (pfu >= 1000) return "S3";
  if (pfu >= 100) return "S2";
  if (pfu >= 10) return "S1";
  return "below S1";
}

function fluxRisk(pfu) {
  if (pfu == null || pfu <= 1) return 0;
  const level = Math.log10(pfu);
  const anchors = [[0, 0], [1, 0.22], [2, 0.55], [3, 0.72], [5, 1]];
  if (level >= 5) return 1;
  for (let i = 0; i < anchors.length - 1; i += 1) {
    const [a, ra] = anchors[i];
    const [b, rb] = anchors[i + 1];
    if (level <= b) return ra + ((level - a) / (b - a)) * (rb - ra);
  }
  return 1;
}

function frame() {
  return state.run ? state.run.frames[Math.min(state.t, state.run.frames.length - 1)] : null;
}

const TIER_RANK = { critical: 0, essential: 1 };

function latestDecision(kinds) {
  if (!state.run) return null;
  const past = state.run.decisions.filter((item) => item.t <= state.t && (!kinds || kinds.has(item.kind)));
  if (!past.length) return null;
  const last = past[past.length - 1].t;
  // Several decisions can share a step; show the most critical service first.
  const rank = (item) => (item.kind === "grok-verification" || item.kind === "flare-warning" ? -1 : TIER_RANK[item.tier] ?? 2);
  return past.filter((item) => item.t === last).sort((a, b) => rank(a) - rank(b))[0];
}

function satName(id) {
  const sat = state.run && state.run.satellites[id];
  return sat ? `${id} ${sat.name}` : id;
}

// ---------------------------------------------------------------------------
// Loading

async function init() {
  const response = await fetch("/api/events");
  const data = await response.json();
  state.events = data.events;
  $("window").innerHTML = data.events.map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
  $("window").value = "oct2024";
  $("status").textContent = data.grok ? "Grok connected · NOAA GOES-18 replay" : "No XAI_API_KEY · deterministic analyst only";
  if (!data.grok) {
    $("grok-analyst").disabled = true;
    $("voice-connect").disabled = true;
    $("debrief-button").disabled = true;
  }
  renderFooter();
  await loadRun();
}

async function loadRun() {
  const token = ++state.token;
  stop();
  $("status").textContent = $("grok-analyst").checked ? "Running the replay with Grok as the in-flight analyst…" : "Running the replay…";
  const response = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  if (!response.ok || token !== state.token) return;
  state.run = await response.json();
  state.announced = new Set();
  const first = state.run.decisions.find((item) => item.kind === "handoff") || state.run.decisions[0];
  const linked = /t=(\d+)/.exec(location.hash);
  state.t = linked ? Number(linked[1]) : first ? Math.max(0, first.t - 12) : state.run.stormStart;
  $("status").textContent = state.run.grok
    ? `Grok analyst decided ${state.run.advisorCalls} handoff${state.run.advisorCalls === 1 ? "" : "s"}${state.run.advisorLatencyMs ? ` · ${Math.round(state.run.advisorLatencyMs)} ms` : ""}`
    : state.run.warning || "Deterministic in-flight analyst · NOAA GOES-18 replay";
  $("debrief-text").innerHTML = "";
  renderMission();
  renderPeople();
  renderModel();
  loadSun(false);
  buildChart();
  render();
  loadEval(token);
}

async function loadEval(token) {
  $("eval-table").textContent = "Running every storm with five seeds…";
  $("headline").innerHTML = "";
  const response = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  if (!response.ok || token !== state.token) return;
  renderEval(await response.json());
}

// ---------------------------------------------------------------------------
// Rendering

function render() {
  const current = frame();
  if (!current) return;
  $("clock").textContent = `${utcLabel(current.utc)} · ${current.gap ? "measurement gap" : `${Math.round(current.pfu).toLocaleString("en-US")} pfu (${sScale(current.pfu)})`}`;
  $("play").textContent = state.playing ? "Pause" : "Play";
  renderPlayhead();
  renderMap(current);
  renderDecision();
  renderFleets(current);
}

function renderPeople() {
  const s = state.run.summary;
  const best = s[primary()];
  const fixed = s.threshold;
  const ground = s.ground;
  const pct = (v) => `${Math.round(v * 100)}%`;
  const storm = best.commsStormHours;
  $("people").innerHTML = `<div class="card-head"><h2>What this means for people · this storm, whole replay</h2><span class="muted small">Simulated outcomes on real radiation data · Lifeline + early warning vs fixed-threshold protection and ground control</span></div>
    <div class="people-grid">
      <div class="people-item"><div class="what">Emergency comms for responders, during the ${storm.toFixed(0)}-hour radiation storm</div>
        <div class="big">${pct(best.commsAvailability)} available</div>
        <div class="vs">Fixed thresholds: <b>${fixed.commsOutageHours.toFixed(0)} h outage</b> (${pct(fixed.commsAvailability)} up) · ground control: <b>${ground.commsOutageHours.toFixed(0)} h outage</b></div></div>
      <div class="people-item"><div class="what">Hurricane damage imagery delivered on time</div>
        <div class="big">${best.imageryOnTime} of ${best.imageryDue}</div>
        <div class="vs">Fixed thresholds: <b>${fixed.imageryOnTime} of ${fixed.imageryDue}</b> · ground control: <b>${ground.imageryOnTime} of ${ground.imageryDue}</b></div></div>
      <div class="people-item"><div class="what">Flood maps delivered on time</div>
        <div class="big">${best.floodMapsOnTime} of ${best.floodMapsDue}</div>
        <div class="vs">Fixed thresholds: <b>${fixed.floodMapsOnTime} of ${fixed.floodMapsDue}</b> · ground control: <b>${ground.floodMapsOnTime} of ${ground.floodMapsDue}</b></div></div>
    </div>`;
}

function renderMission() {
  const mission = state.run.mission;
  const timeline = (mission.timeline || []).map(([when, what]) => `<li><time>${esc(utcLabel(when))}</time>${esc(what)}</li>`).join("");
  const sources = (mission.sources || []).map(([label, url]) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>`).join(" · ");
  $("mission").innerHTML = `<h2>${esc(mission.title)}</h2><p>${esc(mission.context)}</p>${timeline ? `<ol>${timeline}</ol>` : ""}<p class="muted small">${esc(mission.whatIf)}${sources ? ` Sources: ${sources}` : ""}</p>`;
}

const CHART = { width: 1200, height: 150, left: 40, right: 10, top: 10, bottom: 22 };

function chartX(t) {
  const w = CHART.width - CHART.left - CHART.right;
  return CHART.left + (t / Math.max(1, state.run.frames.length - 1)) * w;
}

function chartY(value) {
  const h = CHART.height - CHART.top - CHART.bottom;
  return CHART.top + (1 - value) * h;
}

function buildChart() {
  const frames = state.run.frames;
  let flux = "";
  let drawing = false;
  frames.forEach((item, i) => {
    if (item.gap) { drawing = false; return; }
    flux += `${drawing ? "L" : "M"}${chartX(i).toFixed(1)},${chartY(fluxRisk(item.pfu)).toFixed(1)} `;
    drawing = true;
  });
  const polar = frames.map((item, i) => `${i ? "L" : "M"}${chartX(i).toFixed(1)},${chartY(item.sats.S1.risk).toFixed(1)}`).join(" ");
  const levels = [[0.35, "cautious"], [0.55, "hold"], [0.72, "interlock"]].map(([value, label]) =>
    `<line x1="${CHART.left}" x2="${CHART.width - CHART.right}" y1="${chartY(value)}" y2="${chartY(value)}" stroke="rgba(186,206,232,0.14)" stroke-dasharray="3 4"/>
     <text x="4" y="${chartY(value) + 4}" fill="#6f7f95" font-size="10">${label}</text>`
  ).join("");
  const markColor = (kind) => (kind === "handoff" || kind === "return" ? "#3dce86" : ["flare-warning", "grok-verification", "stand-down"].includes(kind) ? "#c9a8ff" : "#e2b15a");
  const marks = state.run.decisions.filter((item) => ANNOUNCE_KINDS.has(item.kind)).map((item) =>
    `<line x1="${chartX(item.t).toFixed(1)}" x2="${chartX(item.t).toFixed(1)}" y1="${CHART.height - CHART.bottom}" y2="${CHART.height - CHART.bottom + 8}" stroke="${markColor(item.kind)}" stroke-width="2"/>`
  ).join("");
  const xrayY = (flux) => chartY(Math.max(0, Math.min(1, (Math.log10(flux) + 8) / 5)));
  let xray = "";
  let xDrawing = false;
  (state.run.xray || []).forEach((value, i) => {
    if (value == null || value <= 0) { xDrawing = false; return; }
    xray += `${xDrawing ? "L" : "M"}${chartX(i).toFixed(1)},${xrayY(value).toFixed(1)} `;
    xDrawing = true;
  });
  const flares = (state.run.flares || []).filter((flare) => flare.warn || flare.sep).map((flare) => {
    const x = chartX(flare.t).toFixed(1);
    const color = flare.warn ? "#c9a8ff" : "#6f7f95";
    const v = flare.verification;
    const verdict = v && v.verdict ? (v.verdict === "confirm" ? " ✓" : " ✗") : "";
    return `<path d="M${x},${CHART.top + 1} l5,-8 h-10z" transform="translate(0,9)" fill="${color}"/>
      <text x="${x}" y="${CHART.top + 22}" fill="${color}" font-size="10" text-anchor="middle">${esc(flare.class)}${verdict}</text>`;
  }).join("");
  const days = frames.map((item, i) => [item.utc, i]).filter(([utc, i]) => utc.endsWith("T00:00:00Z") && i > 0).map(([utc, i]) =>
    `<text x="${chartX(i).toFixed(1)}" y="${CHART.height - 2}" fill="#6f7f95" font-size="10" text-anchor="middle">${utc.slice(5, 10)}</text>`
  ).join("");
  state.chartBase = `${levels}
    <path d="${xray}" fill="none" stroke="#b99cff" stroke-opacity="0.55" stroke-width="1.1"/>
    <path d="${polar}" fill="none" stroke="#e2b15a" stroke-opacity="0.35" stroke-width="1"/>
    <path d="${flux}" fill="none" stroke="#f4c56d" stroke-width="2.2"/>
    ${flares}${marks}${days}`;
  $("legend").innerHTML = `<span><i class="swatch" style="background:#f4c56d"></i>Measured GOES-18 proton flux on the risk scale</span>
    <span><i class="swatch" style="background:rgba(185,156,255,0.7)"></i>GOES-18 X-ray flux (log scale): flares arrive here first</span>
    <span><i class="swatch" style="background:rgba(226,177,90,0.4)"></i>Relay-1 (polar) local reading</span>
    <span><i class="swatch" style="background:#c9a8ff"></i>▼ Flare flagged by the ML screener · ✓ / ✗ Grok verification</span>
    <span><i class="swatch" style="background:#3dce86"></i>Handoff</span>
    <span><i class="swatch" style="background:#e2b15a"></i>Storm alert, takeover, or all clear</span>`;
}

function renderPlayhead() {
  const x = chartX(state.t).toFixed(1);
  $("chart").innerHTML = `<svg viewBox="0 0 ${CHART.width} ${CHART.height}" preserveAspectRatio="none">${state.chartBase}
    <line x1="${x}" x2="${x}" y1="${CHART.top}" y2="${CHART.height - CHART.bottom}" stroke="#f4f7fb" stroke-width="1.5"/></svg>`;
}

function magneticLatitude(lat, lon) {
  const r = Math.PI / 180;
  const pLat = 80.7 * r;
  const pLon = -72.7 * r;
  const v = Math.sin(lat * r) * Math.sin(pLat) + Math.cos(lat * r) * Math.cos(pLat) * Math.cos(lon * r - pLon);
  return Math.asin(Math.max(-1, Math.min(1, v))) / r;
}

function capCells() {
  if (state.capCells) return state.capCells;
  const cells = [];
  for (let lat = -88; lat <= 88; lat += 4) {
    for (let lon = -178; lon <= 178; lon += 4) {
      const m = Math.abs(magneticLatitude(lat, lon));
      if (m >= 55) cells.push([lon, lat, m >= 63 ? 1 : 0.45]);
    }
  }
  state.capCells = cells;
  return cells;
}

const MAP = { w: 720, h: 360 };
const mx = (lon) => ((lon + 180) / 360) * MAP.w;
const my = (lat) => ((90 - lat) / 180) * MAP.h;
const STATIONS = [["SVL", 78.23, 15.39], ["FBK", 64.86, -147.85], ["ATL", 33.78, -84.4]];

function renderMap(current) {
  const intensity = current.gap ? 0.2 : Math.max(0.12, fluxRisk(current.pfu));
  const caps = capCells().map(([lon, lat, a]) =>
    `<rect x="${mx(lon - 2).toFixed(1)}" y="${my(lat + 2).toFixed(1)}" width="8.2" height="8.2" fill="#e2b15a" opacity="${(a * intensity * 0.55).toFixed(3)}"/>`
  ).join("");
  const grid = [-60, -30, 0, 30, 60].map((lat) => `<line x1="0" x2="${MAP.w}" y1="${my(lat)}" y2="${my(lat)}" stroke="rgba(186,206,232,0.07)"/>`).join("");
  const stations = STATIONS.map(([id, lat, lon]) =>
    `<path d="M${mx(lon)},${my(lat) - 5} l5,9 h-10z" fill="#93a3b8"/><text x="${mx(lon) + 7}" y="${my(lat) + 4}" fill="#93a3b8" font-size="10">${id}</text>`
  ).join("");
  const lifeline = current.fleets[primary()];
  const links = (current.links || []).map((link) => {
    const a = current.sats[link.from];
    const b = current.sats[link.to];
    if (!a || !b || Math.abs(a.lon - b.lon) > 180) return "";
    return `<line x1="${mx(a.lon)}" y1="${my(a.lat)}" x2="${mx(b.lon)}" y2="${my(b.lat)}" stroke="${link.kind === "handoff" || link.kind === "return" ? "#3dce86" : "#b99cff"}" stroke-width="${link.kind === "schedule" ? 1 : 2.4}" stroke-dasharray="${link.kind === "schedule" ? "3 4" : ""}" opacity="0.8"/>`;
  }).join("");
  const start = Math.max(0, state.t - 14);
  const sats = Object.entries(current.sats).map(([id, sat]) => {
    const trail = state.run.frames.slice(start, state.t).map((item) =>
      `<circle cx="${mx(item.sats[id].lon).toFixed(1)}" cy="${my(item.sats[id].lat).toFixed(1)}" r="1.2" fill="${SAT_COLORS[id]}" opacity="0.45"/>`
    ).join("");
    const node = lifeline.nodes[id];
    const hot = sat.exposure > 0.5;
    const analyst = lifeline.analysts.includes(id);
    const ring = analyst ? `<circle cx="${mx(sat.lon)}" cy="${my(sat.lat)}" r="12" fill="none" stroke="#b99cff" stroke-width="1.5" stroke-dasharray="3 3"/>` : "";
    return `${trail}${ring}<circle cx="${mx(sat.lon)}" cy="${my(sat.lat)}" r="${hot ? 7 : 6}" fill="${SAT_COLORS[id]}" stroke="${hot ? "#e0645a" : "#0b111b"}" stroke-width="${hot ? 3 : 2}"/>
      <text x="${mx(sat.lon) + 10}" y="${my(sat.lat) + 4}" fill="#e8eef7" font-size="11" font-weight="700">${id}</text>
      <text x="${mx(sat.lon) + 10}" y="${my(sat.lat) + 16}" fill="#93a3b8" font-size="9.5">${esc(node.services.join(", ") || node.label)}</text>`;
  }).join("");
  $("map").innerHTML = `<rect width="${MAP.w}" height="${MAP.h}" fill="#0b1422"/>${grid}${caps}${stations}${links}${sats}`;
  $("map").setAttribute("viewBox", `0 0 ${MAP.w} ${MAP.h}`);
  $("map-note").textContent = `Analyst: ${lifeline.analysts.map(satName).join(", ") || "none (hubs paused)"}`;
  $("sat-legend").innerHTML = Object.entries(state.run.satellites).map(([id, sat]) =>
    `<span><i style="background:${SAT_COLORS[id]}"></i>${id} ${esc(sat.name)} · ${esc(sat.role === "hub" ? "hub" : "simple")} · ${esc(sat.orbit)}</span>`
  ).join("") + `<span>Shaded: polar caps where solar protons reach low orbit · red outline: exposed now · dashed ring: analyst hub · lines: Lifeline crosslink messages</span>`;
}

function renderDecision() {
  const item = latestDecision();
  if (!item) {
    $("decision").innerHTML = `<h3>No Lifeline decision yet</h3><p class="muted small">The hubs are watching fleet telemetry. Play the replay or jump to the next decision.</p>`;
    return;
  }
  if (item.kind === "grok-verification") {
    const pct = item.probability == null ? 0 : item.probability;
    const confirm = item.verdict === "confirm";
    const signs = (item.eruptionSigns || []).map((sign) => `<li>${esc(sign)}</li>`).join("");
    $("decision").innerHTML = `<div class="meta">${esc(utcLabel(item.utc))} · Grok vision (${esc(item.source)}) · stage 2 of the early-warning cascade</div>
      <h3>${esc(item.title)}</h3>
      <div class="prob"><div class="prob-bar"><i style="width:${pct}%"></i><b style="left:${item.threshold || 0}%"></b></div><span>Grok: ${pct}% chance this flare drives a proton storm · confirm threshold ${item.threshold}% · CME: ${esc(item.cmeVisible)} (${esc(item.cmeExtent)})</span></div>
      <p><b class="${confirm ? "ok" : "bad"}">${confirm ? "Confirmed" : "Rejected"}.</b> ${esc(item.text)}</p>
      ${item.panel ? `<a href="/verify/${esc(item.panel)}" target="_blank" rel="noopener"><img class="verify-panel" src="/verify/${esc(item.panel)}" alt="Images Grok checked: flare, EUV difference, coronagraph, coronagraph difference"/></a>` : ""}
      ${signs ? `<ul class="regions">${signs}</ul>` : ""}`;
    return;
  }
  if (item.kind === "flare-warning") {
    const pct = Math.min(100, item.probability * 100);
    const threshold = state.run.earlyWarning.threshold * 100;
    const lead = state.run.decisions.find((other) => other.kind === "storm" && other.t > item.t);
    $("decision").innerHTML = `<div class="meta">${esc(utcLabel(item.utc))} · ${esc(item.hub)} X-ray photometer · ML early-warning model</div>
      <h3>${esc(item.title)}</h3>
      <div class="prob"><div class="prob-bar"><i style="width:${Math.min(100, pct * 3)}%"></i><b style="left:${Math.min(100, threshold * 3)}%"></b></div><span>Screener: ${pct.toFixed(1)}% chance of a proton storm · high-recall threshold ${threshold.toFixed(1)}% (bar scaled ×3)</span></div>
      <p>${esc(item.text)}</p>
      <p class="muted small">Expected proton onset: ${esc(utcLabel(item.onsetUtc[0]))} to ${esc(utcLabel(item.onsetUtc[2]))}.${lead ? ` Protons were detected ${minutes(lead.t - item.t)} after this warning.` : ""}</p>`;
    return;
  }
  const options = (item.options || []).map((option) =>
    `<tr class="${option.id === item.chosen ? "chosen" : ""}"><td>${esc(option.label)}</td><td>${minutes(option.downtime)}</td><td>${option.lostRisk ? option.lostRisk : "—"}${option.interlockRisk ? " · interlock risk" : ""}</td></tr>`
  ).join("");
  const source = item.source && item.source !== "analyst" ? "Grok in-flight analyst" : "In-flight analyst";
  $("decision").innerHTML = `<div class="meta">${esc(utcLabel(item.utc))} · ${esc(item.hub)} ${esc(item.kind === "handoff" ? source : "")}</div>
    <h3>${esc(item.title)}</h3><p>${esc(item.text)}</p>
    ${options ? `<table><thead><tr><th>Option</th><th>Forecast downtime</th><th>Lost-work risk</th></tr></thead><tbody>${options}</tbody></table>` : ""}`;
}

function renderFleets(current) {
  const scores = STRATEGY_ORDER.map((name) => current.fleets[name].scores);
  const rows = [
    ["Emergency comms outage (storm)", (s) => s.commsOutageHours, (v) => `${v.toFixed(1)} h`],
    ["Imagery deliveries late", (s) => s.imageryDue - s.imageryOnTime, (v) => v],
    ["Flood maps late", (s) => s.floodMapsDue - s.floodMapsOnTime, (v) => v],
    ["All service downtime", (s) => s.downtime, (v) => minutes(v)],
    ["Hardware interlocks", (s) => s.interlocks, (v) => v],
  ];
  $("fleets").innerHTML = STRATEGY_ORDER.map((name, index) => {
    const fleet = current.fleets[name];
    const nodes = Object.entries(fleet.nodes).map(([id, node]) =>
      `<div class="node-row"><b>${id}</b><span class="mode ${node.tone}">${esc(node.label)}</span><span class="svc">${esc(node.services.join(", ") || "archive work only")}</span></div>`
    ).join("");
    const board = rows.map(([label, get, fmt]) => {
      const values = scores.map(get);
      const best = new Set(values).size > 1 && get(scores[index]) === Math.min(...values);
      return `<span>${label}</span><span class="${best ? "best" : ""}">${fmt(get(scores[index]))}</span>`;
    }).join("");
    return `<article class="fleet ${name}"><h3>${esc(state.run.strategies[name])}</h3><p class="sub">${esc(STRATEGY_ABOUT[name])}</p>${nodes}<div class="scoreboard">${board}</div></article>`;
  }).join("");
}

function renderEval(data) {
  const change = data.headline.change.lifeline_ew.threshold;
  const versusMl = data.headline.change.lifeline_ew.lifeline_ml;
  const versusProtons = data.headline.change.lifeline_ew.lifeline;
  const totals = data.headline.totals;
  const pct = (value) => (value == null ? "—" : `${value > 0 ? "+" : ""}${Math.round(value)}%`);
  const tile = (label, key, fmt) =>
    `<div class="tile"><div class="label">${label}</div><div class="value">${pct(change[key])}</div><div class="sub">${fmt(totals.lifeline_ew[key])} vs ${fmt(totals.threshold[key])} fixed-threshold<br>${pct(versusProtons[key])} vs protons-only · ${pct(versusMl[key])} vs ML-only</div></div>`;
  const hours = (v) => `${(v * 5 / 60).toFixed(0)} h`;
  const count = (v) => v.toFixed(0);
  $("headline").innerHTML = [
    tile("Emergency comms outage during storms", "commsOutageHours", (v) => `${v.toFixed(0)} h`),
    tile("Emergency comms deadlines missed", "criticalMissed", count),
    tile("All service deadlines missed", "missed", count),
    tile("Service downtime", "downtime", hours),
    tile("Lost work", "lost", (v) => v.toFixed(0)),
    tile("Hardware interlocks", "interlocks", count),
  ].join("");
  const head = EVAL_ORDER.map((name) => `<th>${esc(data.strategies[name])}</th>`).join("");
  const body = data.rows.map((row) => {
    const cells = EVAL_ORDER.map((name) => {
      const s = row.scores[name];
      const best = EVAL_ORDER.every((other) => row.scores[other].missed >= s.missed) && EVAL_ORDER.some((other) => row.scores[other].missed > s.missed);
      return `<td><span class="${best ? "best" : ""}">${Math.round(s.commsAvailability * 100)}% comms up · ${s.missed.toFixed(1)} late</span><br><span class="muted">${s.commsOutageHours.toFixed(1)} h comms outage · ${(s.downtime * 5 / 60).toFixed(1)} h all down${s.interlocks ? ` · ${s.interlocks.toFixed(1)} interlocks` : ""}</span></td>`;
    }).join("");
    return `<tr><td>${esc(row.label)}</td>${cells}</tr>`;
  }).join("");
  $("eval-table").innerHTML = `<table class="grid"><thead><tr><th>Storm (per run: comms availability during the storm, late deliveries)</th>${head}</tr></thead><tbody>${body}</tbody></table>
    <p class="muted small">Tiles: Lifeline + early warning against the fixed threshold, totals over the five storms (quiet control excluded). In the quiet control every strategy delivers every task; hub analysis costs about 4% of background archive throughput, and early-warning readiness costs more when flares are false alarms.</p>`;
}

async function loadSun(refresh) {
  const token = state.token;
  if (refresh) $("sun-refresh").disabled = true;
  try {
    const response = await fetch("/api/sun", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ window: $("window").value, refresh }) });
    if (!response.ok || token !== state.token) return;
    state.sun = await response.json();
    renderSun();
  } finally {
    $("sun-refresh").disabled = false;
  }
}

function renderSun() {
  const data = state.sun;
  if (!data) return;
  const images = data.images;
  const layers = [["aia193", "Corona · AIA 193 Å", images.watch.aia193], ["hmi", "Magnetogram · HMI", images.watch.hmi]];
  if (images.flare) layers.push(["flare", `Flare · AIA 131 Å`, images.flare.image]);
  const current = layers.find(([id]) => id === state.sunLayer) || layers[0];
  const tabs = layers.map(([id, label]) => `<button type="button" data-layer="${id}" class="${id === current[0] ? "on" : ""}">${esc(label)}</button>`).join("");
  const a = data.assessment;
  const outlook = a ? a.outlook : "unknown";
  const regions = a && a.regions ? a.regions.map((r) => `<li><b>${esc(r.position || "")}</b> · ${esc(r.hemisphere || "")} · ${esc(r.complexity || "")} · flare risk <span class="risk-${esc(r.flare_risk)}">${esc(r.flare_risk || "")}</span></li>`).join("") : "";
  const ev = data.evaluation;
  const evText = ev
    ? `Measured skill on ${ev.days} sampled days (2023-2025): ROC-AUC ${ev.auc} for Grok's flare probability (0.5 = no skill); mean ${ev.meanProbabilityXDays}% before X-class flares vs ${ev.meanProbabilityQuietDays}% before quiet days.`
    : "Skill check pending.";
  const caption = current[0] === "flare"
    ? `${images.flare.class} flare at its X-ray peak, ${utcLabel(images.flare.utc)}${images.flare.lon != null ? ` · ${images.flare.lon > 0 ? "W" : "E"}${Math.abs(Math.round(images.flare.lon))}` : ""} (display only; not shown to Grok)`
    : `SDO image at ${utcLabel(images.watchUtc)}, 6 h into the window`;
  $("sunwatch").innerHTML = `<div class="sun-img"><img src="/sun/${esc(current[2])}" alt="${esc(current[1])}"/><div class="sun-tabs">${tabs}</div><p class="muted small">${esc(caption)}</p></div>
    <div class="sun-text">
      <div class="card-head"><h2>Sun watch · Grok vision</h2><button type="button" id="sun-refresh-inline">Re-run with Grok</button></div>
      <p class="outlook outlook-${esc(outlook)}">48-hour outlook: <b>${esc(outlook)}</b>${a ? ` · Earth-connected risk: <b>${esc(a.earthConnectedRisk)}</b>` : ""}${a && a.flareProbability != null ? ` · M5+ flare within 48 h: <b>${a.flareProbability}%</b>` : ""}</p>
      <p>${esc(a ? a.briefing : "No briefing cached for this window.")}</p>
      ${regions ? `<ul class="regions">${regions}</ul>` : ""}
      <p class="muted small">${a ? `${esc(a.model)} read the corona and magnetogram images${a.live ? " just now" : " (cached)"}.` : ""} ${esc(evText)} Informational: Sun watch does not change the simulation.${data.warning ? ` ${esc(data.warning)}` : ""}</p>
    </div>`;
  $("sunwatch").querySelectorAll(".sun-tabs button").forEach((button) => button.addEventListener("click", () => { state.sunLayer = button.dataset.layer; renderSun(); }));
  const inline = $("sun-refresh-inline");
  if (inline) inline.addEventListener("click", () => { inline.disabled = true; inline.textContent = "Grok is looking…"; loadSun(true); });
}

function renderModel() {
  const m = state.run.earlyWarning;
  const x = m.metrics;
  const s1 = x.stage1;
  const c = m.cascade;
  const cascadeRow = c
    ? `<tr><td><b>Cascade: screener → Grok verification</b></td><td>${c.cascade.stormsConfirmed} of ${c.cascade.storms}</td><td>${c.cascade.falseAlarmsKept}</td><td>${c.cascade.warnings}</td><td>${(c.cascade.precision * 100).toFixed(0)}%</td></tr>`
    : `<tr><td>Cascade: screener → Grok verification</td><td colspan="4" class="muted">verification pending</td></tr>`;
  const comparison = (m.comparison || []).slice(0, 6).map((r) =>
    `<tr><td>${esc(r.model)}</td><td>${r.rocAuc.toFixed(3)}</td><td>${r.prAuc.toFixed(2)}</td><td>${r.budgetCaught} of ${x.testStorms}</td><td>${r.warningsToCatchAll}</td></tr>`
  ).join("");
  const replay = (state.run.flares || []).filter((flare) => flare.warn || flare.sep).map((flare) => {
    const v = flare.verification;
    const grok = v && v.verdict ? ` → Grok ${v.probability}% ${v.verdict === "confirm" ? "✓ confirmed" : "✗ rejected"}` : "";
    return `<li>${esc(flare.class)} at ${esc(utcLabel(flare.peakUtc))}: screener ${(flare.p * 100).toFixed(1)}%${flare.warn ? " (flagged)" : ""}${grok}${flare.sep ? " · <b>caused a proton storm</b>" : " · no storm of its own"}</li>`;
  }).join("") || "<li>No flare was flagged in this window.</li>";
  $("model-card").innerHTML = `<div class="card-head"><h2>Early-warning cascade</h2><span class="muted small">Trained ${esc(m.split.train)} · tested on unseen data ${esc(m.split.test)}</span></div>
    <div class="model-grid">
      <div>
        <p><b>Stage 1, ML screener.</b> ${esc(m.model)}, chosen from ${(m.comparison || []).length || "several"} models. At each flare's X-ray peak it scores the chance of a ≥10 pfu proton storm from peak flux, fluence, rise time, background, and location. Its threshold is set on training data to miss almost nothing.</p>
        <p><b>Stage 2, Grok vision.</b> 90 minutes after the peak, Grok checks SDO and SOHO LASCO coronagraph images (flare, EUV difference, coronagraph, coronagraph difference) for a fast, wide coronal mass ejection, the usual driver of proton storms. Its confirm threshold (${c ? c.threshold : "—"}%) is calibrated on training-period warnings only.</p>
        <table class="mini"><thead><tr><th>Held-out test: ${x.testFlares} flares, ${x.testStorms} storms</th><th>Storms caught</th><th>False alarms</th><th>Warnings</th><th>Precision</th></tr></thead>
        <tbody><tr><td>Stage 1 alone (screener)</td><td>${s1.tp} of ${x.testStorms}</td><td>${s1.fp}</td><td>${s1.warnings}</td><td>${((s1.tp / Math.max(1, s1.warnings)) * 100).toFixed(0)}%</td></tr>${cascadeRow}</tbody></table>
        <p class="muted small">${c ? `Grok returned a verdict for ${c.test.verified} of ${c.test.warnings} held-out warnings; the cascade row counts those. ` : ""}Screener ROC-AUC ${x.testAuc}. ${c && c.grokAucAmongWarnings != null ? `Grok's probability separates storms from false alarms among flagged flares with AUC ${c.grokAucAmongWarnings} (mean ${c.meanProbabilityStorms}% vs ${c.meanProbabilityFalseAlarms}%).` : ""} Median lead from flare peak to proton onset: ${x.medianLeadHours} h.</p>
      </div>
      <div>
        <p class="muted small">Model comparison on the same held-out test (top 6 by PR-AUC)</p>
        <table class="mini"><thead><tr><th>Model</th><th>ROC-AUC</th><th>PR-AUC</th><th>Top 3/mo</th><th>To catch all</th></tr></thead><tbody>${comparison}</tbody></table>
        <p class="muted small">Flares in this replay</p><ul class="flare-list">${replay}</ul>
      </div>
    </div>`;
}

function renderFooter() {
  $("footer").innerHTML = `<p><b>Measured:</b> NOAA GOES-18 five-minute proton flux (NCEI sgps-l2-avg5m, westward differential channels summed at and above 10 MeV), timestamps, and quality flags.</p>
    <p><b>Simulated:</b> the five satellites, their orbits, a tilted-dipole shielding model (full exposure over the polar caps), sensor noise, memory-error telemetry, faults, services, crosslinks, and ground passes. The flux-to-risk scale is a simulator scale: 100 pfu meets the hold level and 1,000 pfu the hardware interlock.</p>
    <p><b>Early warning:</b> GOES-18 X-ray flare summary and flare locations (NCEI xrsf-l2-flsum, xrsf-l2-flloc), X-ray flux (xrsf-l2-avg1m), and the NOAA solar proton event list. Solar images: NASA SDO AIA and HMI via Helioviewer.</p>
    <p><b>Grok:</b> the in-flight analyst option and the voice copilot call the xAI API remotely to emulate onboard reasoning. Every analyst choice is validated against the simulator's candidate plans before it runs.</p>`;
}

// ---------------------------------------------------------------------------
// Playback

function stop() {
  state.playing = false;
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

function play() {
  if (!state.run) return;
  if (state.playing) {
    stop();
    render();
    return;
  }
  state.playing = true;
  state.timer = setInterval(() => {
    if (state.t >= state.run.frames.length - 1) {
      stop();
      render();
      return;
    }
    state.t += 1;
    announce();
    render();
  }, 1000 / state.speed);
  render();
}

function seek(t) {
  state.t = Math.max(0, Math.min(state.run.frames.length - 1, t));
  render();
}

function announce() {
  const fresh = state.run.decisions.filter((item) => item.t === state.t && ANNOUNCE_KINDS.has(item.kind) && !state.announced.has(`${item.t}:${item.title}`));
  if (!fresh.length) return;
  fresh.forEach((item) => {
    state.announced.add(`${item.t}:${item.title}`);
    addBubble("system", `${utcLabel(item.utc)} · ${item.title}. ${item.text}`);
  });
  if (state.voice && state.voice.connected && $("auto-announce").checked) {
    const text = fresh.map((item) => `${item.title}. ${item.text}`).join(" ");
    state.voice.say(
      `[Fleet event at ${fresh[0].utc}] ${text}`,
      "Announce this fleet event to the operator in one or two short sentences. Name the service and the reason. Do not call tools."
    );
  }
}

// ---------------------------------------------------------------------------
// Copilot

function addBubble(kind, text) {
  const box = $("transcript");
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  bubble.textContent = text;
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

let liveBubble = null;

function onTranscript(role, text, final) {
  if (role === "grok") {
    if (!liveBubble) liveBubble = addBubble("grok", text);
    liveBubble.textContent = text;
    $("transcript").scrollTop = $("transcript").scrollHeight;
    if (final) liveBubble = null;
  } else if (final) {
    addBubble("you", text);
  }
}

const VOICE_INSTRUCTIONS = `You are the Lifeline copilot, speaking with a satellite operations operator.
Lifeline is a five-satellite fleet: two compute-capable hubs (H1, H2) and three simple satellites that carry services:
S1 emergency comms relay (critical), S2 hurricane imagery, S3 flood maps. During solar radiation storms the hubs forecast
which satellites will be exposed over the polar caps, send protective schedules ahead of exposure, and hand critical
services to shielded peers. The replay uses real NOAA GOES-18 radiation data; the satellites and outcomes are simulated,
so say "in this simulation" when you describe outcomes. Always use your tools for facts and never invent numbers.
Times are UTC; one step is five minutes. Keep answers to two or three short sentences unless asked for more.
Speak in terms of the people served: hours of emergency comms kept for responders, imagery and flood maps delivered on time.
When asked about a specific service, pass its name to get_latest_decision.
Lifeline also has an early-warning chain: Grok reads real solar images for a day-ahead Sun watch, and an ML model trained on
the GOES-18 flare record estimates, at each flare's X-ray peak, the chance of a proton storm hours before protons arrive.
Use get_space_weather_outlook for those questions.
When you receive a message that starts with [Fleet event], announce it briefly.`;

function voiceTools() {
  return {
    get_situation: {
      description: "Current replay time, measured proton flux and NOAA S-scale level, which hub is the analyst, and which satellites are exposed right now.",
      run: () => {
        const current = frame();
        return {
          utc: current.utc,
          measuredFluxPfu: current.pfu,
          radiationStormLevel: sScale(current.pfu),
          event: state.run.mission.title,
          analystHub: current.fleets[primary()].analysts,
          exposedNow: Object.entries(current.sats).filter(([, sat]) => sat.exposure > 0.5).map(([id]) => satName(id)),
        };
      },
    },
    get_fleet_status: {
      description: "Status of every satellite in one strategy's fleet: role, orbit, mode, hosted services, radiation reading, exposure, error count.",
      parameters: { type: "object", properties: { strategy: { type: "string", enum: STRATEGY_ORDER, description: "Default lifeline" } } },
      run: ({ strategy }) => {
        const current = frame();
        const fleet = current.fleets[strategy || primary()] || current.fleets[primary()];
        return Object.fromEntries(Object.entries(fleet.nodes).map(([id, node]) => [id, {
          name: state.run.satellites[id].name,
          role: state.run.satellites[id].role,
          orbit: state.run.satellites[id].orbit,
          mode: node.label,
          services: node.services,
          radiationRisk: current.sats[id].risk,
          exposurePercent: Math.round(current.sats[id].exposure * 100),
          memoryErrorsThisStep: current.sats[id].errors,
        }]));
      },
    },
    get_latest_decision: {
      description: "Lifeline decisions with the reason and every option the analyst compared (forecast downtime, lost-work risk, interlock risk). Pass a service name (Emergency comms, Imagery, Flood maps) to get the latest decision about that service; otherwise returns every decision from the most recent decision step. Use this for why and what-if questions.",
      parameters: { type: "object", properties: { service: { type: "string", description: "Optional: Emergency comms, Imagery, or Flood maps" } } },
      run: ({ service }) => {
        const past = state.run.decisions.filter((item) => item.t <= state.t);
        let picked;
        if (service) {
          const wanted = service.toLowerCase();
          const match = past.filter((item) => item.service && (item.service.toLowerCase().includes(wanted) || wanted.includes(item.service.toLowerCase().split(" ")[0])));
          picked = match.length ? [match[match.length - 1]] : [];
        } else if (past.length) {
          const last = past[past.length - 1].t;
          picked = past.filter((item) => item.t === last);
        } else {
          picked = [];
        }
        if (!picked.length) return { none: service ? `No decision about ${service} yet in this replay.` : "No decision yet in this replay." };
        return picked.map((item) => ({
          utc: item.utc,
          title: item.title,
          reason: item.text,
          decidedBy: item.source && item.source !== "analyst" ? "Grok in-flight analyst" : `${item.hub} in-flight analyst`,
          options: (item.options || []).map((option) => ({
            option: option.label,
            forecastDowntimeMinutes: option.downtime * 5,
            lostWorkRisk: option.lostRisk,
            interlockRisk: option.interlockRisk,
            chosen: option.id === item.chosen,
          })),
        }));
      },
    },
    get_space_weather_outlook: {
      description: "The early-warning chain: Grok's Sun-watch briefing from real SDO images for this storm, the latest ML flare warning (proton-storm probability, flare class, location, expected onset), and the model's held-out skill.",
      run: () => {
        const warning = state.run.decisions.filter((item) => item.kind === "flare-warning" && item.t <= state.t).pop();
        const verification = state.run.decisions.filter((item) => item.kind === "grok-verification" && item.t <= state.t).pop();
        const cascade = model.cascade && model.cascade.cascade;
        const model = state.run.earlyWarning;
        const sun = state.sun && state.sun.assessment;
        return {
          sunWatch: sun ? { imagesFrom: sun.utc, outlook48h: sun.outlook, earthConnectedRisk: sun.earthConnectedRisk, briefing: sun.briefing } : "not loaded",
          latestScreenerWarning: warning ? { utc: warning.utc, flareClass: warning.flareClass, screenerProbability: warning.probability, expectedOnsetUtc: warning.onsetUtc, detail: warning.text } : "no flare warning yet in this replay",
          latestGrokVerification: verification ? { utc: verification.utc, verdict: verification.verdict, grokProbability: verification.probability, cme: `${verification.cmeVisible} (${verification.cmeExtent})`, reason: verification.text } : "no verification yet",
          screener: { model: model.model, heldOutAuc: model.metrics.testAuc, heldOutStormsCaught: `${model.metrics.stage1.tp} of ${model.metrics.testStorms}`, heldOutWarnings: model.metrics.stage1.warnings, medianLeadHours: model.metrics.medianLeadHours },
          cascadeHeldOut: cascade ? { stormsConfirmed: `${cascade.stormsConfirmed} of ${cascade.storms}`, falseAlarmsRemoved: cascade.falseAlarmsRemoved, warningsKept: cascade.warnings } : "evaluation pending",
        };
      },
    },
    compare_strategies: {
      description: "Outcomes so far and for the whole replay for every strategy, in terms of people served: emergency-comms availability and outage hours during the storm, imagery and flood maps delivered on time, missed deadlines, interlocks.",
      run: () => {
        const current = frame();
        const pick = (s) => ({ emergencyCommsAvailableDuringStorm: `${Math.round(s.commsAvailability * 100)}%`, emergencyCommsOutageHours: s.commsOutageHours, imageryOnTime: `${s.imageryOnTime} of ${s.imageryDue}`, floodMapsOnTime: `${s.floodMapsOnTime} of ${s.floodMapsDue}`, allDeadlinesMissed: s.missed, hardwareInterlocks: s.interlocks });
        return Object.fromEntries(STRATEGY_ORDER.map((name) => [state.run.strategies[name], {
          soFar: pick(current.fleets[name].scores),
          wholeReplay: pick(state.run.summary[name]),
        }]));
      },
    },
    get_recent_events: {
      description: "The last few Lifeline fleet events up to now (storm alerts, handoffs, analyst takeovers, all clear).",
      parameters: { type: "object", properties: { count: { type: "integer" } } },
      run: ({ count }) => state.run.decisions.filter((item) => item.t <= state.t).slice(-(count || 5)).map((item) => ({ utc: item.utc, title: item.title, detail: item.text })),
    },
    control_playback: {
      description: "Control the replay: play, pause, or jump to the next Lifeline decision.",
      parameters: { type: "object", properties: { action: { type: "string", enum: ["play", "pause", "next_decision"] } }, required: ["action"] },
      run: ({ action }) => {
        if (action === "play" && !state.playing) play();
        if (action === "pause" && state.playing) play();
        if (action === "next_decision") jumpToNextDecision();
        return { ok: true, utc: frame().utc };
      },
    },
  };
}

async function toggleVoice() {
  if (state.voice && state.voice.connected) {
    state.voice.disconnect();
    state.voice = null;
    $("voice-connect").textContent = "Connect Grok Voice";
    $("voice-connect").classList.add("primary");
    $("voice-mic").disabled = true;
    $("voice-mic").textContent = "Mic off";
    $("voice-mic").classList.remove("live");
    return;
  }
  const voice = new window.GrokVoice({
    instructions: VOICE_INSTRUCTIONS,
    tools: voiceTools(),
    onTranscript,
    onStatus: (text) => { $("voice-status").textContent = text; },
    onTool: (name) => addBubble("tool", `Grok checked ${name.replace(/_/g, " ")}`),
  });
  try {
    await voice.connect();
    state.voice = voice;
    $("voice-connect").textContent = "Disconnect";
    $("voice-connect").classList.remove("primary");
    $("voice-mic").disabled = false;
    voice.say("Introduce yourself to the operator in one sentence and summarize the current situation using your tools.");
  } catch (error) {
    $("voice-status").textContent = error.message;
  }
}

async function toggleMic() {
  const voice = state.voice;
  if (!voice) return;
  if (voice.micOn) {
    voice.stopMic();
    $("voice-mic").textContent = "Mic off";
    $("voice-mic").classList.remove("live");
    return;
  }
  try {
    await voice.startMic();
    $("voice-mic").textContent = "Mic on · listening";
    $("voice-mic").classList.add("live");
  } catch (error) {
    $("voice-status").textContent = `Microphone unavailable: ${error.message}`;
  }
}

async function ask(event) {
  event.preventDefault();
  const text = $("ask-text").value.trim();
  if (!text) return;
  $("ask-text").value = "";
  if (!state.voice || !state.voice.connected) {
    await toggleVoice();
    if (!state.voice) return;
  }
  addBubble("you", text);
  state.voice.say(text);
}

function jumpToNextDecision() {
  const next = state.run.decisions.find((item) => item.t > state.t && ANNOUNCE_KINDS.has(item.kind));
  if (!next) return;
  seek(next.t);
  announce();
  render();
}

async function loadDebrief() {
  const button = $("debrief-button");
  button.disabled = true;
  $("debrief-text").innerHTML = `<p class="muted">Grok is writing the debrief…</p>`;
  try {
    const response = await fetch("/api/debrief", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const data = await response.json();
    if (!data.text) {
      $("debrief-text").innerHTML = `<p class="muted">${esc(data.warning || "No debrief returned.")}</p>`;
      return;
    }
    const paragraphs = data.text.split(/\n\s*\n/).map((part) => `<p>${esc(part.trim())}</p>`).join("");
    $("debrief-text").innerHTML = `${paragraphs}<p class="muted small">Written by ${esc(data.model)} in ${(data.latencyMs / 1000).toFixed(1)} s from simulator outputs.</p>`;
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring

$("play").addEventListener("click", play);
$("jump-decision").addEventListener("click", jumpToNextDecision);
$("speeds").addEventListener("click", (event) => {
  const speed = Number(event.target.dataset.speed);
  if (!speed) return;
  state.speed = speed;
  document.querySelectorAll("#speeds button").forEach((button) => button.classList.toggle("on", Number(button.dataset.speed) === speed));
  if (state.playing) { stop(); play(); }
});
$("chart").addEventListener("click", (event) => {
  const svg = event.currentTarget.querySelector("svg");
  if (!svg || !state.run) return;
  const rect = svg.getBoundingClientRect();
  const ratio = (event.clientX - rect.left) / rect.width;
  const x = ratio * CHART.width;
  const t = Math.round(((x - CHART.left) / (CHART.width - CHART.left - CHART.right)) * (state.run.frames.length - 1));
  seek(t);
});
$("chart").addEventListener("keydown", (event) => {
  if (!state.run) return;
  const step = event.shiftKey ? 12 : 1;
  if (event.key === "ArrowRight") seek(state.t + step);
  else if (event.key === "ArrowLeft") seek(state.t - step);
  else return;
  event.preventDefault();
});
$("voice-connect").addEventListener("click", toggleVoice);
$("voice-mic").addEventListener("click", toggleMic);
$("ask").addEventListener("submit", ask);
$("debrief-button").addEventListener("click", loadDebrief);
$("sun-refresh").addEventListener("click", () => loadSun(true));
$("window").addEventListener("change", loadRun);
$("seed").addEventListener("change", loadRun);
$("grok-analyst").addEventListener("change", loadRun);
let sliderTimer = null;
["latency", "loss"].forEach((id) => $(id).addEventListener("input", () => {
  $("latency-out").textContent = `${Number($("latency").value) * 5} min`;
  $("loss-out").textContent = `${Math.round(Number($("loss").value) * 100)}%`;
  clearTimeout(sliderTimer);
  sliderTimer = setTimeout(loadRun, 300);
}));

init();
