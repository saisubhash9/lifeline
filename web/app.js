// Lifeline dashboard: replay, map, four fleets, copilot, evaluation.

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

const STRATEGY_ORDER = ["ground", "threshold", "local", "lifeline"];
const STRATEGY_ABOUT = {
  ground: "Onboard safe mode only. The ground reads telemetry during station passes and uplinks commands on a later pass.",
  threshold: "Every satellite follows fixed onboard rules on its own sensor. No sharing.",
  local: "Hubs forecast and protect themselves; simple satellites keep fixed rules. Nothing is shared.",
  lifeline: "Hubs forecast the whole fleet, send protective schedules ahead of exposure, and hand services to shielded peers.",
};
const SAT_COLORS = { H1: "#b99cff", H2: "#d7c6ff", S1: "#e2b15a", S2: "#8eb4ff", S3: "#7fe0b8" };
const ANNOUNCE_KINDS = new Set(["storm", "handoff", "takeover", "clear", "return"]);

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
  return past.filter((item) => item.t === last).sort((a, b) => (TIER_RANK[a.tier] ?? 2) - (TIER_RANK[b.tier] ?? 2))[0];
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
  const marks = state.run.decisions.filter((item) => ANNOUNCE_KINDS.has(item.kind)).map((item) =>
    `<line x1="${chartX(item.t).toFixed(1)}" x2="${chartX(item.t).toFixed(1)}" y1="${CHART.height - CHART.bottom}" y2="${CHART.height - CHART.bottom + 8}" stroke="${item.kind === "handoff" ? "#3dce86" : "#e2b15a"}" stroke-width="2"/>`
  ).join("");
  const days = frames.map((item, i) => [item.utc, i]).filter(([utc, i]) => utc.endsWith("T00:00:00Z") && i > 0).map(([utc, i]) =>
    `<text x="${chartX(i).toFixed(1)}" y="${CHART.height - 2}" fill="#6f7f95" font-size="10" text-anchor="middle">${utc.slice(5, 10)}</text>`
  ).join("");
  state.chartBase = `${levels}
    <path d="${polar}" fill="none" stroke="#e2b15a" stroke-opacity="0.35" stroke-width="1"/>
    <path d="${flux}" fill="none" stroke="#f4c56d" stroke-width="2.2"/>
    ${marks}${days}`;
  $("legend").innerHTML = `<span><i class="swatch" style="background:#f4c56d"></i>Measured GOES-18 proton flux on the risk scale</span>
    <span><i class="swatch" style="background:rgba(226,177,90,0.4)"></i>Relay-1 (polar) local reading: spikes on every polar-cap pass</span>
    <span><i class="swatch" style="background:#3dce86"></i>Lifeline handoff</span>
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
  const lifeline = current.fleets.lifeline;
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
    ["Emergency comms deadlines missed", (s) => s.criticalMissed, (v) => v],
    ["All service deadlines missed", (s) => s.missed, (v) => v],
    ["Service downtime", (s) => s.downtime, (v) => minutes(v)],
    ["Lost work (units)", (s) => s.lost, (v) => v.toFixed(1)],
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
  const change = data.headline.change.threshold;
  const totals = data.headline.totals;
  const pct = (value) => (value == null ? "—" : `${value > 0 ? "+" : ""}${Math.round(value)}%`);
  const tile = (label, key, fmt) =>
    `<div class="tile"><div class="label">${label}</div><div class="value">${pct(change[key])}</div><div class="sub">${fmt(totals.lifeline[key])} vs ${fmt(totals.threshold[key])} fixed-threshold</div></div>`;
  const hours = (v) => `${(v * 5 / 60).toFixed(0)} h`;
  const count = (v) => v.toFixed(0);
  $("headline").innerHTML = [
    tile("Emergency comms deadlines missed", "criticalMissed", count),
    tile("All service deadlines missed", "missed", count),
    tile("Service downtime", "downtime", hours),
    tile("Lost work", "lost", (v) => v.toFixed(0)),
    tile("Hardware interlocks", "interlocks", count),
  ].join("");
  const head = STRATEGY_ORDER.map((name) => `<th>${esc(data.strategies[name])}</th>`).join("");
  const body = data.rows.map((row) => {
    const cells = STRATEGY_ORDER.map((name) => {
      const s = row.scores[name];
      const best = STRATEGY_ORDER.every((other) => row.scores[other].missed >= s.missed) && STRATEGY_ORDER.some((other) => row.scores[other].missed > s.missed);
      return `<td><span class="${best ? "best" : ""}">${s.criticalMissed.toFixed(1)} comms · ${s.missed.toFixed(1)} all</span><br><span class="muted">${(s.downtime * 5 / 60).toFixed(1)} h down · ${s.lost.toFixed(1)} lost${s.interlocks ? ` · ${s.interlocks.toFixed(1)} interlocks` : ""}</span></td>`;
    }).join("");
    return `<tr><td>${esc(row.label)}</td>${cells}</tr>`;
  }).join("");
  $("eval-table").innerHTML = `<table class="grid"><thead><tr><th>Storm (per run, missed deadlines)</th>${head}</tr></thead><tbody>${body}</tbody></table>
    <p class="muted small">Totals exclude the quiet control. In the quiet control every strategy delivers every task; Lifeline's hub analysis costs about 4% of background archive throughput.</p>`;
}

function renderFooter() {
  $("footer").innerHTML = `<p><b>Measured:</b> NOAA GOES-18 five-minute proton flux (NCEI sgps-l2-avg5m, westward differential channels summed at and above 10 MeV), timestamps, and quality flags.</p>
    <p><b>Simulated:</b> the five satellites, their orbits, a tilted-dipole shielding model (full exposure over the polar caps), sensor noise, memory-error telemetry, faults, services, crosslinks, and ground passes. The flux-to-risk scale is a simulator scale: 100 pfu meets the hold level and 1,000 pfu the hardware interlock.</p>
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
When asked about a specific service, pass its name to get_latest_decision.
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
          analystHub: current.fleets.lifeline.analysts,
          exposedNow: Object.entries(current.sats).filter(([, sat]) => sat.exposure > 0.5).map(([id]) => satName(id)),
        };
      },
    },
    get_fleet_status: {
      description: "Status of every satellite in one strategy's fleet: role, orbit, mode, hosted services, radiation reading, exposure, error count.",
      parameters: { type: "object", properties: { strategy: { type: "string", enum: STRATEGY_ORDER, description: "Default lifeline" } } },
      run: ({ strategy }) => {
        const current = frame();
        const fleet = current.fleets[strategy || "lifeline"] || current.fleets.lifeline;
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
    compare_strategies: {
      description: "Scores so far and for the whole replay for the four strategies: missed emergency-comms deadlines, all missed deadlines, downtime, lost work, interlocks.",
      run: () => {
        const current = frame();
        const pick = (s) => ({ emergencyCommsDeadlinesMissed: s.criticalMissed, allDeadlinesMissed: s.missed, downtimeMinutes: s.downtime * 5, lostWork: s.lost, hardwareInterlocks: s.interlocks });
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
