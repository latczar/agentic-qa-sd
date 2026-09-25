"use strict";
// The overseer: watch the work, and decide what needs deciding, on one page.
//
// Three sources, each used for what it is good at:
//   - /overseer/stream (Server-Sent Events): what each worker is doing right
//     now, and broker queue depths. Live, but nothing is stored.
//   - /overseer/board: tickets waiting on a person, with everything needed to
//     decide them, and recent outcomes. Polled, and refreshed when a ticket
//     finishes.
//   - /overseer/team: the org chart and routines. Polled slowly - it changes
//     on the scale of minutes, and it runs a couple of health checks.
//
// Every value that came from a ticket is escaped before it touches the page:
// the subject and description are whatever a member of the public typed.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// Per-viewer conveniences only. Private windows and blocked storage throw, and
// the page must work the same without them.
const prefs = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

// --- Words ---------------------------------------------------------------

const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const pct = (x) => `${Math.round(Number(x || 0) * 100)}%`;
const lower = (s) => String(s || "").toLowerCase().replace(/_/g, " ");

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 90) return "a minute ago";
  const m = s / 60;
  if (m < 60) return `${Math.round(m)} min ago`;
  const h = m / 60;
  if (h < 24) return `${Math.round(h)} h ago`;
  const d = h / 24;
  return d < 2 ? "yesterday" : `${Math.round(d)} days ago`;
}

function waited(iso) {
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  if (m < 1) return "under a minute";
  if (m < 60) return plural(Math.round(m), "minute");
  if (m < 48 * 60) return plural(Math.round(m / 60), "hour");
  return plural(Math.round(m / 1440), "day");
}

function clock(iso) {
  return new Date(iso || Date.now()).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// The gate's reasons, in words a person would use. The gate writes them in a
// fixed shape (worker/app/rules.py); anything this does not recognise is shown
// exactly as written, so a new rule is never hidden - only less polished.
// "payroll, payslip" -> "payroll and payslip"; three or more get commas too.
function listed(csv) {
  const parts = csv.split(/,\s*/).filter(Boolean);
  return parts.length < 2 ? parts.join("") : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
}

function plainReason(r) {
  if (!r) return "";
  let m;
  if ((m = r.match(/^sensitive subject \((.+?)\)/))) return `It's about ${listed(m[1])}, which always needs a person.`;
  if ((m = r.match(/^destructive action proposed \((.+?)\)/))) return `The suggested fix involves ${listed(m[1].split(/,\s*/).map((x) => `"${x}"`).join(", "))}, which needs your sign-off.`;
  if (r.startsWith("ticket text contains analyser-directed instructions")) return "The ticket reads like it's trying to give the AI instructions, so the AI didn't touch it.";
  if (r === "no resolution was proposed") return "The AI couldn't suggest a fix.";
  if (r === "no supporting knowledge articles were cited") return "The AI's answer isn't backed by any help article.";
  if (r.startsWith("cited documents that were never retrieved")) return "The AI referred to help articles it was never given.";
  if ((m = r.match(/^confidence ([\d.]+) is below ([\d.]+)/))) return `The AI is only ${pct(m[1])} sure, and it needs to be ${pct(m[2])}.`;
  if (r.startsWith("CRITICAL priority")) return "It's marked critical, so a person decides.";
  if ((m = r.match(/^confidence ([\d.]+) with cited evidence/))) return `The AI is ${pct(m[1])} sure, and the help articles back it up.`;
  return r;
}

// --- State ---------------------------------------------------------------

const state = {
  workers: new Map(),   // worker id -> lane
  subjects: new Map(),  // ticket id -> subject, for tokens that joined mid-ticket
  board: null,
  team: null,
  queues: null,
  brokerWorkers: null,
  feed: [],             // kept so the feed can be redrawn when the view changes
  decided: new Map(),   // ticket id -> time, so a just-decided card is not re-added by a stale poll
};

const engineer = () => document.body.classList.contains("engineer");

// Where each step puts the token. "finished" picks its exit from the status.
const STOP_OF = {
  heartbeat: "queue", retry_scheduled: "queue", skipped: "queue",
  picked_up: "screen", screening: "screen",
  screened_out: "gate",
  retrieving: "retrieve", retrieved: "retrieve", prompt_built: "retrieve",
  calling_model: "model", invalid_answer: "model", analysed: "model", run_failed: "model",
  gate: "gate", notified: "gate",
  dead_lettered: "failed",
};
const EXIT_OF_STATUS = { RESOLVED: "solved", AWAITING_APPROVAL: "needs", FAILED: "failed" };
const TERMINAL = new Set(["finished", "skipped", "retry_scheduled", "dead_lettered"]);
const CHAIN = ["queue", "screen", "retrieve", "model", "gate"];
const EXITS = ["solved", "needs", "failed"];

// How long a finished ticket rests at its exit before the token walks back.
// Long enough to read where it went, short enough not to mislead.
const LINGER_MS = 3500;

function laneFor(id) {
  if (!state.workers.has(id)) {
    state.workers.set(id, {
      id, number: state.workers.size + 1,
      stop: "queue", prevStop: null, step: "heartbeat", detail: {}, tone: "", ticket: null,
      stepAt: Date.now(), lastSeen: Date.now(), linger: null, el: null, placed: false,
    });
  }
  return state.workers.get(id);
}

function setIdle(lane) {
  Object.assign(lane, { prevStop: lane.stop, stop: "queue", step: "heartbeat", detail: {}, tone: "", ticket: null, stepAt: Date.now() });
}

const busyLanes = () => [...state.workers.values()].filter((l) => l.step !== "heartbeat" && !l.linger);

function workerName(lane) {
  return engineer() ? lane.id.slice(0, 12) : `Worker ${lane.number}`;
}

function toneFor(step, d) {
  switch (step) {
    case "calling_model": return "thinking";
    case "invalid_answer": case "screened_out": case "retry_scheduled": return "warn";
    case "gate": return d.requires_human ? "warn" : "ok";
    case "finished": return { RESOLVED: "ok", AWAITING_APPROVAL: "warn", FAILED: "bad" }[d.status] || "";
    case "dead_lettered": case "run_failed": return "bad";
    default: return "";
  }
}

// What a worker is doing, in one line, as plain text (escaped where drawn).
function doing(lane) {
  const s = lane.ticket ? state.subjects.get(lane.ticket) : "";
  const t = lane.ticket ? `#${lane.ticket}${s ? ` “${s}”` : ""}` : "";
  const d = lane.detail || {};
  if (engineer()) {
    const bits = Object.entries(d).filter(([k]) => k !== "subject").map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : typeof v === "object" ? JSON.stringify(v) : v}`);
    return lane.step === "heartbeat" ? "idle (heartbeat)" : `${lane.step}${lane.ticket ? ` #${lane.ticket}` : ""}${bits.length ? " · " + bits.join(" · ") : ""}`;
  }
  switch (lane.stop) {
    case "queue": return lane.step === "retry_scheduled" ? `Hit a problem with ${t}, so it will try again shortly` : "Waiting for work";
    case "screen": return `Checking ${t}`;
    case "retrieve": return `Looking up help articles for ${t}`;
    case "model": return d.attempt > 1 ? `Thinking about ${t} (attempt ${d.attempt})` : `Thinking about ${t}`;
    case "gate": return `Deciding what happens to ${t}`;
    case "solved": return `Solved ${t} on its own`;
    case "needs": return `Passed ${t} to you`;
    case "failed": return `Couldn't finish ${t || "a ticket"}`;
    default: return "";
  }
}

// --- Live events -----------------------------------------------------------

function onProgress(e) {
  const lane = laneFor(e.worker);
  lane.lastSeen = Date.now();

  if (e.step === "heartbeat") {
    // Only an idle worker sends one, but it must not cut short the moment a
    // finished ticket spends at its exit being readable.
    if (!lane.linger && lane.step !== "heartbeat") setIdle(lane);
    beat("queue");
    return redrawLive();
  }
  pet.lastWork = Date.now();

  if (e.step === "picked_up" && e.detail?.subject) state.subjects.set(e.ticket_id, e.detail.subject);
  else learnSubject(e.ticket_id);

  clearTimeout(lane.linger);
  lane.linger = null;
  const next = e.step === "finished" ? EXIT_OF_STATUS[e.detail?.status] || "queue" : STOP_OF[e.step] || lane.stop;
  // A pickup is an arrival: the ticket comes in through the inlet first.
  const from = e.step === "picked_up" ? "in" : lane.stop;
  // Pulses come in three colours only; "thinking" is a token state, not one of them.
  const pulseTone = ["ok", "warn", "bad"].includes(toneFor(e.step, e.detail || {})) ? toneFor(e.step, e.detail || {}) : "";
  if (next !== lane.stop || e.step === "picked_up") travel(from, next, pulseTone);
  if (next !== lane.stop) lane.prevStop = lane.stop;
  teamSignal(e);
  celebrateIfSolved(e);
  Object.assign(lane, {
    stop: next, step: e.step, detail: e.detail || {}, tone: toneFor(e.step, e.detail || {}),
    ticket: e.ticket_id ?? lane.ticket, stepAt: Date.now(),
  });

  if (TERMINAL.has(e.step)) {
    lane.linger = setTimeout(() => { lane.linger = null; setIdle(lane); redrawLive(); }, LINGER_MS);
    refreshBoardSoon(400);
    refreshTeamSoon(1500);
  }

  pushFeed({ at: e.at, event: e });
  redrawLive();
}

async function learnSubject(ticketId) {
  if (!ticketId || state.subjects.has(ticketId)) return;
  state.subjects.set(ticketId, "");
  try {
    const t = await api(`/tickets/${ticketId}`);
    state.subjects.set(ticketId, t.subject);
    redrawLive();
  } catch {}
}

function onQueues(list) {
  state.queues = list;
  const processing = list.find((q) => q.name === "ticket.processing");
  state.brokerWorkers = processing ? processing.consumers : null;
  drawChips();
  drawHeadline();
  drawWorkers();
  drawQueues();
  if (teamVisible()) drawTeam();
}

// Only redraw the org chart while it is on screen: it is rebuilt wholesale,
// and doing that every two seconds behind a hidden tab is wasted work.
const teamVisible = () => state.team && !$("#tab-team").hidden;

function redrawLive() {
  layoutTokens();
  drawWorkers();
  drawHeadline();
  if (teamVisible()) drawTeam();
}

// --- Pulses ----------------------------------------------------------------
//
// A pulse is a real event travelling along a wire: a ticket moving between
// stations, a search going out and coming back, a notification reaching you.
// Nothing pulses on a timer, so a still page means a still system.

const SVG_NS = "http://www.w3.org/2000/svg";
// Motion follows the system's "reduce motion" setting unless the viewer says
// otherwise with the Motion switch. Someone who asked their system for less
// motion gets a still page by default; someone whose animations are off for
// no particular reason can still turn the pulses on. Either way, every fact
// the motion shows is also written down in the text.
const systemCalm = matchMedia("(prefers-reduced-motion: reduce)");
let motionPref = prefs.get("overseer.motion"); // "on", "off", or null to follow the system
const calm = () => motionPref === "off" || (motionPref !== "on" && systemCalm.matches);
const PULSE_MS = 560;

function pulseAlong(svg, d, { tone = "", delay = 0, ms = PULSE_MS, lit = null, arrive = null } = {}) {
  if (calm() || !svg || !d) return;
  setTimeout(() => {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "none");
    svg.appendChild(path);
    const length = path.getTotalLength();
    const group = document.createElementNS(SVG_NS, "g");
    // A glow, a head and a short tail, so it reads as something travelling.
    const dots = [[7, "pulse-glow", 0], [3.8, "", 0], [2.7, "", 0.07], [1.8, "", 0.14]].map(([r, extra, lag], i) => {
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("r", r);
      c.setAttribute("class", `pulse ${tone} ${extra}`);
      if (i > 1) c.style.opacity = String(1 - (i - 1) * 0.3);
      c.dataset.lag = lag;
      group.appendChild(c);
      return c;
    });
    svg.appendChild(group);
    lit?.classList.add("lit");
    const start = performance.now();
    const frame = (now) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      for (const dot of dots) {
        const pt = path.getPointAtLength(Math.max(0, eased - Number(dot.dataset.lag)) * length);
        dot.setAttribute("cx", pt.x);
        dot.setAttribute("cy", pt.y);
      }
      if (t < 1) return requestAnimationFrame(frame);
      group.remove();
      path.remove();
      lit?.classList.remove("lit");
      arrive?.();
    };
    requestAnimationFrame(frame);
  }, delay);
}

// The stops in travel order, so a jump (Checking straight to Deciding, when
// a ticket is screened out) still pulses along every wire it passes.
const ROUTE = ["in", ...CHAIN];

function travel(from, to, tone) {
  if (!from || from === to || EXITS.includes(from)) return;
  const i = ROUTE.indexOf(from);
  const j = EXITS.includes(to) ? ROUTE.length : ROUTE.indexOf(to);
  if (i < 0 || j <= i) return; // backwards (a retry going back to the queue) is not a delivery
  const hops = [];
  for (let k = i; k < Math.min(j, ROUTE.length - 1); k++) hops.push([ROUTE[k], ROUTE[k + 1]]);
  if (EXITS.includes(to)) hops.push(["gate", to]);
  hops.forEach(([a, b], n) => {
    const wire = $(`#wires path[data-from="${a}"][data-to="${b}"]`);
    if (wire) pulseAlong($("#wires"), wire.getAttribute("d"), { tone: n === hops.length - 1 ? tone : "", delay: n * PULSE_MS * 0.85, lit: wire });
  });
}

function beat(stop) {
  if (calm()) return;
  const el = $(`[data-stop="${stop}"]`);
  if (!el) return;
  el.classList.remove("beat");
  void el.offsetWidth; // restart the animation
  el.classList.add("beat");
}

// --- The track -------------------------------------------------------------

function box(track, key) {
  const t = track.getBoundingClientRect();
  const r = $(`[data-stop="${key}"]`, track).getBoundingClientRect();
  return { l: r.left - t.left, r: r.right - t.left, t: r.top - t.top, b: r.bottom - t.top, cx: (r.left + r.right) / 2 - t.left, cy: (r.top + r.bottom) / 2 - t.top };
}

// Wires are measured from where the stops actually are, so the same code
// draws the wide layout (left to right, forking right) and the narrow one
// (top to bottom, forking down).
function layoutWires() {
  const track = $("#track");
  if (!track || track.offsetParent === null) return;
  const t = track.getBoundingClientRect();
  const curve = (a, b) => {
    if (b.l >= a.r - 1) {
      const mx = (a.r + b.l) / 2;
      return `M${a.r},${a.cy} C${mx},${a.cy} ${mx},${b.cy} ${b.l - 3},${b.cy}`;
    }
    const my = (a.b + b.t) / 2;
    return `M${a.cx},${a.b} C${a.cx},${my} ${b.cx},${my} ${b.cx},${b.t - 3}`;
  };
  const pairs = [];
  for (let i = 0; i < CHAIN.length - 1; i++) pairs.push([CHAIN[i], CHAIN[i + 1]]);
  for (const x of EXITS) pairs.push(["gate", x]);
  // The inlet: where new tickets come in from, the web form or Telegram.
  const q = box(track, "queue");
  const inlet = q.l > 14
    ? { d: `M6,${q.cy} L${q.l - 3},${q.cy}`, x: 6, y: q.cy }
    : { d: `M${q.cx},6 L${q.cx},${q.t - 3}`, x: q.cx, y: 6 };
  const svg = $("#wires");
  svg.setAttribute("viewBox", `0 0 ${t.width} ${t.height}`);
  svg.innerHTML =
    `<defs><marker id="arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="7" markerHeight="7" orient="auto">` +
    `<path d="M0,1 L8,5 L0,9 z" fill="var(--muted)"/></marker></defs>` +
    pairs.map(([a, b]) => `<path data-from="${a}" data-to="${b}" marker-end="url(#arrow)" d="${curve(box(track, a), box(track, b))}"/>`).join("") +
    `<path data-from="in" data-to="queue" marker-end="url(#arrow)" d="${inlet.d}"/><circle class="inlet-dot" cx="${inlet.x}" cy="${inlet.y}" r="3"/>`;
  markWires();
}

// The wire a ticket has just travelled flows, so the eye follows where it went.
function markWires() {
  const live = new Set(busyLanes().filter((l) => l.prevStop).map((l) => `${l.prevStop}>${l.stop}`));
  for (const p of $$("#wires path[data-from]")) p.classList.toggle("live", live.has(`${p.dataset.from}>${p.dataset.to}`));
  const here = new Set(busyLanes().map((l) => l.stop));
  for (const s of $$("#track .stop")) s.classList.toggle("here", here.has(s.dataset.stop) && !EXITS.includes(s.dataset.stop));
}

// Tokens are created once per worker and moved, never rebuilt. Rebuilding
// them on every event is what made them jump rather than travel.
function layoutTokens() {
  const track = $("#track");
  if (!track || track.offsetParent === null) return;
  const t = track.getBoundingClientRect();
  const atStop = new Map();
  for (const lane of state.workers.values()) {
    if (!atStop.has(lane.stop)) atStop.set(lane.stop, []);
    atStop.get(lane.stop).push(lane);
  }
  for (const [stop, lanes] of atStop) {
    const dock = $(`[data-stop="${stop}"] .dock`, track);
    if (!dock) continue;
    const r = dock.getBoundingClientRect();
    lanes.forEach((lane, i) => {
      if (!lane.el) {
        lane.el = document.createElement("div");
        lane.el.innerHTML = `<span class="face"></span><span class="what"></span>`;
        track.appendChild(lane.el);
      }
      const cls = lane.step === "heartbeat" ? "idle" : lane.tone || "";
      lane.el.className = `token ${cls}${lane.placed ? "" : " no-anim"}`;
      const label = lane.ticket ? `#${lane.ticket}` : engineer() ? lane.id.slice(0, 6) : "";
      $(".what", lane.el).textContent = label;
      lane.el.title = `${workerName(lane)}: ${doing(lane)}`;
      const x = r.left - t.left + i * 24;
      const y = r.top - t.top + Math.max(0, (r.height - 28) / 2);
      lane.el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
      if (!lane.placed) {
        lane.placed = true;
        requestAnimationFrame(() => lane.el.classList.remove("no-anim"));
      }
    });
  }
  markWires();
}

function layoutTrack() {
  layoutWires();
  layoutTokens();
}

function drawWorkers() {
  const el = $("#workers");
  if (!state.workers.size) {
    el.innerHTML = `<div class="nobody">${
      state.brokerWorkers === 0
        ? "No worker is running, so new tickets will wait in line until one starts."
        : "Waiting to hear from a worker…"
    }</div>`;
    return;
  }
  el.innerHTML = [...state.workers.values()].map((lane) => `
    <div class="worker-line" data-worker="${esc(lane.id)}">
      <span class="who">${esc(workerName(lane))}</span>
      <span>${esc(doing(lane))}</span>
      <span class="t" data-since="${lane.stepAt}" data-busy="${lane.step !== "heartbeat"}"></span>
      <span class="quiet"></span>
    </div>`).join("");
  tick();
}

// Timers update in place, so a token mid-slide is not disturbed.
function tick() {
  for (const node of $$(".worker-line .t")) {
    const secs = Math.floor((Date.now() - Number(node.dataset.since)) / 1000);
    node.textContent = node.dataset.busy === "true" && secs >= 1 ? `${secs} s` : "";
  }
  for (const line of $$(".worker-line")) {
    const lane = state.workers.get(line.dataset.worker);
    if (!lane) continue;
    const quiet = Math.floor((Date.now() - lane.lastSeen) / 1000);
    // A worker waiting on the model cannot send a heartbeat: its I/O loop is
    // blocked for the length of the call. Silence then is expected.
    $(".quiet", line).textContent = quiet > 20 && lane.step !== "calling_model" ? `not heard from for ${quiet} s` : "";
  }
}

// --- Headline and chips ------------------------------------------------------

function drawHeadline() {
  const busy = busyLanes();
  const needs = state.board?.waiting.length ?? 0;
  const say = $("#say");
  let text, cls = "";
  if (busy.length) {
    const lane = busy[0];
    const s = state.subjects.get(lane.ticket);
    text = `Working on #${lane.ticket}${s ? ` “${s}”` : ""}${busy.length > 1 ? `, and ${busy.length - 1} more` : ""}`;
  } else if (needs) {
    text = `${plural(needs, "ticket")} need${needs === 1 ? "s" : ""} you`;
    cls = "needs";
  } else if (state.brokerWorkers === 0) {
    text = "No worker is running. New tickets will wait until one starts.";
  } else if (state.brokerWorkers == null && !state.workers.size) {
    text = "Connecting…";
  } else {
    text = "All quiet. Ready for the next ticket.";
  }
  if (say.textContent !== text) say.textContent = text;
  say.className = `say ${cls}`;
  drawPet();
}

function drawChips() {
  const q = Object.fromEntries((state.queues || []).map((x) => [x.name, x]));
  const b = state.board;
  const chips = [];
  if (state.brokerWorkers != null) {
    chips.push(`<span class="chip ${state.brokerWorkers ? "" : "bad"}"><b>${state.brokerWorkers}</b> ${engineer() ? `consumer${state.brokerWorkers === 1 ? "" : "s"} on ticket.processing` : `worker${state.brokerWorkers === 1 ? "" : "s"} ready`}</span>`);
  }
  if (q["ticket.processing"]) chips.push(`<span class="chip"><b>${q["ticket.processing"].messages}</b> in line</span>`);
  if (q["ticket.retry"]?.messages) chips.push(`<span class="chip warn"><b>${q["ticket.retry"].messages}</b> trying again</span>`);
  if (b) {
    chips.push(`<span class="chip ${b.waiting.length ? "warn" : ""}"><b>${b.waiting.length}</b> need${b.waiting.length === 1 ? "s" : ""} you</span>`);
    chips.push(`<span class="chip"><b>${b.solved_today}</b> solved today${b.solved_today ? ` (${b.solved_automatically_today} on its own)` : ""}</span>`);
  }
  $("#chips").innerHTML = chips.join("");
}

function drawQueues() {
  const does = {
    "ticket.processing": "Waiting for a free worker",
    "ticket.retry": "Backing off after a failure, then straight back to processing",
    "ticket.dead-letter": "Given up on. Where the ticket exists, it was marked FAILED",
  };
  $("#queues").innerHTML = (state.queues || []).map((q) => {
    let tone = "";
    if (q.name === "ticket.dead-letter" && q.messages) tone = "bad";
    else if (q.name === "ticket.retry" && q.messages) tone = "warn";
    else if (q.name === "ticket.processing" && q.messages && !q.consumers) tone = "bad";
    return `<div class="q ${tone}"><div class="mono muted">${esc(q.name)}</div><div class="num">${q.messages}</div>
      <div class="does">${esc(does[q.name] || "")}${q.name === "ticket.processing" ? ` · ${plural(q.consumers, "consumer")}` : ""}</div></div>`;
  }).join("");
}

// --- The board: exits and decisions ----------------------------------------

async function refreshBoard() {
  try {
    state.board = await api("/overseer/board");
  } catch {
    return;
  }
  drawExits();
  drawDecide();
  drawChips();
  drawHeadline();
  if (teamVisible()) drawTeam();
  layoutTrack();
}

let boardTimer = null;
function refreshBoardSoon(ms = 300) {
  clearTimeout(boardTimer);
  boardTimer = setTimeout(refreshBoard, ms);
}

function drawExits() {
  const b = state.board;
  const first = (name) => String(name || "").split(" ")[0];
  $("#n-solved").textContent = b.solved_today;
  // Two per exit, so the three exits stay the same height and the fork reads
  // as a fork rather than one tall box and two stubs.
  $("#recent-solved").innerHTML = b.solved.length
    ? b.solved.slice(0, 2).map((o) => `<li title="${esc(o.subject)}"><span class="what">#${o.id} ${esc(o.subject)}</span><span class="when">${o.decided_by ? esc(first(o.decided_by)) : "on its own"} · ${ago(o.at)}</span></li>`).join("")
    : `<li class="muted">Nothing yet</li>`;
  $("#n-needs").textContent = b.waiting.length;
  $('[data-stop="needs"]').classList.toggle("has", b.waiting.length > 0);
  // Newest arrivals here, oldest first in the cards below. The exit answers
  // "where did the ticket I just watched go?"; the list answers "what should
  // I decide first?". Each exit item jumps to its card.
  const newest = [...b.waiting].sort((a, z) => new Date(z.waiting_since) - new Date(a.waiting_since)).slice(0, 2);
  $("#recent-needs").innerHTML = newest.map((w) =>
    `<li title="${esc(w.subject)}"><a href="#decide-title" class="what" data-card="${w.id}">#${w.id} ${esc(w.subject)}</a><span class="when">${esc(waited(w.waiting_since))}</span></li>`).join("");
  $("#go-decide").textContent = b.waiting.length ? "Decide below ↓" : "Nothing waiting";
  $("#recent-failed").innerHTML = b.failed.length
    ? b.failed.slice(0, 2).map((o) => `<li title="${esc(o.subject)}"><span class="what">#${o.id} ${esc(o.subject)}</span><span class="when">${ago(o.at)}</span></li>`).join("")
    : `<li class="muted">Nothing has failed</li>`;
  const count = $("#needs-count");
  count.textContent = b.waiting.length;
  count.classList.toggle("zero", !b.waiting.length);
}

// How sure the AI must be, with evidence, before Approve becomes the button
// that stands out. Below it every option carries the same weight: a page that
// makes a 20%-sure, unsupported answer the biggest green button on the card is
// nudging people to wave it through.
const CONFIDENT_ENOUGH = 0.7;

function cardHtml(w) {
  const analysed = !!w.suggestion;
  const earned = analysed && (w.confidence ?? 0) >= CONFIDENT_ENOUGH && (w.sources || []).length > 0;
  const sources = (w.sources || []).map((s) => esc(s.title || s.document));
  const tags = [
    w.category && `<span class="tag">${esc(w.category)}</span>`,
    w.priority && `<span class="tag">${esc(lower(w.priority))} priority</span>`,
    w.confidence != null && `<span class="tag"><span class="bar"><i style="width:${Math.round(w.confidence * 100)}%"></i></span>${pct(w.confidence)} sure</span>`,
  ].filter(Boolean).join("");
  return `
    <div class="top-row">
      <span class="id">#${w.id}</span>
      <span class="subj">${esc(w.subject)}</span>
      <span class="meta">${w.source === "TELEGRAM" ? "via Telegram · " : ""}waiting ${waited(w.waiting_since)}</span>
    </div>
    <div class="why"><b>Why you:</b> ${esc(plainReason(w.reason) || "The rules asked for a person.")}
      <div class="eng mono muted">${esc(w.reason || "")}</div></div>
    ${analysed ? `
      <div class="suggest">
        <div class="label">The AI suggests</div>
        <p class="fix">${esc(w.suggestion)}</p>
        ${w.root_cause ? `<div class="detail">Likely cause: ${esc(w.root_cause)}</div>` : ""}
        <div class="detail">${sources.length ? `Based on: ${sources.join(", ")}` : "Not based on any help article"}</div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
      </div>` : `
      <div class="suggest"><div class="detail">The AI didn't analyse this one, so there is no suggestion to approve.</div></div>`}
    ${w.steered_by ? `<div class="steer-note"><b>Earlier correction:</b> ${esc(w.steered_by)}</div>` : ""}
    <div class="actions">
      ${analysed ? `<button class="btn ${earned ? "go" : "go-soft"}" data-act="approve">✓ Approve the suggestion</button>` : ""}
      <button class="btn" data-act="handled">I'll handle it myself</button>
      ${analysed
        ? `<button class="btn wrong" data-open="reject">✗ The AI got it wrong</button>
           <button class="btn fix" data-open="instruct">✎ Correct it</button>`
        : `<button class="btn wrong" data-open="reject">Pass it to someone else</button>`}
    </div>
    <div class="more" data-panel="reject" hidden>
      <input type="text" maxlength="500" placeholder="${analysed ? "What was wrong? (optional)" : "Anything they should know? (optional)"}" aria-label="Reason">
      <div class="row">
        <button class="btn send danger" data-act="reject">Send it to someone else</button>
        <span class="hint">${analysed ? "It is marked as escalated, and the AI's answer is recorded as wrong." : "It is marked as escalated for someone else to pick up."}</span>
      </div>
    </div>
    ${analysed ? `
    <div class="more" data-panel="instruct" hidden>
      <textarea rows="2" maxlength="2000" placeholder="Tell the AI what it missed, e.g. “This is a network fault, not an access one”" aria-label="Your correction"></textarea>
      <div class="row">
        <button class="btn send" data-act="instruct">Send it back to the AI</button>
        <span class="hint">It runs again with your correction, then comes back here.</span>
      </div>
    </div>` : ""}
    <div class="outcome" hidden></div>`;
}

// Cards are kept, not rebuilt, so a half-typed correction survives the next
// poll. One is only redrawn if its ticket changed and nobody is using it.
function drawDecide() {
  const list = $("#decide");
  const waiting = (state.board?.waiting || []).filter((w) => !state.decided.has(w.id));
  const want = new Set(waiting.map((w) => String(w.id)));

  $("#decide-title").innerHTML = `Needs you <span class="count ${waiting.length ? "" : "zero"}">${waiting.length}</span>${waiting.length > 1 ? ` <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:500">longest waiting first</span>` : ""}`;

  for (const card of $$(".ticket-card", list)) {
    if (!want.has(card.dataset.id) && !card.dataset.done) card.remove();
  }
  $(".empty", list)?.remove();

  let prev = null;
  for (const w of waiting) {
    let card = $(`.ticket-card[data-id="${w.id}"]`, list);
    const sig = JSON.stringify([w.reason, w.suggestion, w.confidence, w.steered_by, w.waiting_since]);
    if (!card) {
      card = document.createElement("article");
      // Only flash cards that arrive after the first load; on load, every
      // card is new and flashing them all would mean nothing.
      card.className = state.boardSeen ? "ticket-card arrived" : "ticket-card";
      card.dataset.id = w.id;
      card.innerHTML = cardHtml(w);
    } else if (card.dataset.sig !== sig && !card.dataset.expanded && !card.dataset.done && !card.contains(document.activeElement)) {
      card.innerHTML = cardHtml(w);
    }
    card.dataset.sig = sig;
    const where = prev ? prev.nextSibling : list.firstChild;
    if (where !== card) list.insertBefore(card, where);
    prev = card;
  }

  state.boardSeen = true;

  if (!$(".ticket-card", list)) {
    list.innerHTML = `<div class="empty">Nothing needs you right now. When the AI isn't sure, or a ticket is sensitive, it lands here with the AI's suggestion and the reason.</div>`;
  }
}

const OUTCOME = {
  approve: (id) => `✓ Approved. #${id} is solved.`,
  handled: (id) => `Noted: you're handling #${id} yourself. It's marked solved, and the AI isn't blamed.`,
  reject: (id) => `#${id} has gone to someone else.`,
  instruct: (id) => `Sent #${id} back to the AI with your correction. Watch it go round again above.`,
};
const YOU_DID = {
  approve: (id) => `You approved the AI's suggestion for #${id}`,
  handled: (id) => `You took #${id} on yourself`,
  reject: (id) => `You sent #${id} to someone else`,
  instruct: (id) => `You corrected the AI on #${id} and sent it round again`,
};

function settle(card, text, tone) {
  card.dataset.done = "1";
  $(".actions", card).hidden = true;
  $$(".more", card).forEach((p) => { p.hidden = true; });
  const out = $(".outcome", card);
  out.textContent = text;
  out.className = `outcome ${tone}`;
  out.hidden = false;
  setTimeout(() => {
    card.classList.add("leaving");
    setTimeout(() => { card.remove(); drawDecide(); }, 400);
  }, 2400);
}

$("#decide").addEventListener("click", async (ev) => {
  const card = ev.target.closest(".ticket-card");
  if (!card || card.dataset.done) return;
  const id = Number(card.dataset.id);

  const opener = ev.target.closest("[data-open]");
  if (opener) {
    const name = opener.dataset.open;
    let open = false;
    for (const panel of $$(".more", card)) {
      panel.hidden = panel.dataset.panel !== name ? true : !panel.hidden;
      if (!panel.hidden) { open = true; $("input, textarea", panel)?.focus(); }
    }
    // Not data-open: that is how the buttons that open panels are found, and
    // the card itself carrying it made every click inside look like one.
    card.dataset.expanded = open ? "1" : "";
    return;
  }

  const btn = ev.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const me = Number($("#me").value) || null;
  let body;
  if (act === "instruct") {
    const box = $('[data-panel="instruct"] textarea', card);
    const instruction = box.value.trim();
    if (!instruction) { box.focus(); return; }
    body = { instruction, instructed_by_id: me };
  } else {
    const reason = act === "reject" ? $('[data-panel="reject"] input', card).value.trim() || null : null;
    body = { decided_by_id: me, reason };
  }

  $$("button", card).forEach((b) => { b.disabled = true; });
  try {
    await api(`/tickets/${id}/${act}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.decided.set(id, Date.now());
    settle(card, OUTCOME[act](id), "ok");
    pushFeed({ at: new Date().toISOString(), you: YOU_DID[act](id) });
    if (act === "instruct") handoff("you", "triage", { tone: "" });
    else ping("you", "ok");
    if (act === "approve" || act === "handled") celebrate(`says thanks: #${id} is sorted.`);
  } catch (err) {
    if (err.status === 409) {
      state.decided.set(id, Date.now());
      settle(card, "Someone has already decided this one.", "");
    } else {
      $$("button", card).forEach((b) => { b.disabled = false; });
      const out = $(".outcome", card);
      out.textContent = `That didn't work: ${err.message}`;
      out.className = "outcome bad";
      out.hidden = false;
      return;
    }
  }
  refreshBoardSoon(900);
});

// Enter sends a correction; Shift+Enter still makes a new line.
$("#decide").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" || ev.shiftKey) return;
  const panel = ev.target.closest(".more");
  if (!panel) return;
  ev.preventDefault();
  $("[data-act]", panel)?.click();
});

// --- The feed ----------------------------------------------------------------

const FEED_LIMIT = 80;

function plainLine(e) {
  const d = e.detail || {};
  const t = `#${e.ticket_id}`;
  switch (e.step) {
    case "picked_up": return [`Picked up ${t}${d.subject ? ` “${esc(d.subject)}”` : ""}${d.retry_count ? `, try ${d.retry_count + 1}` : ""}`, ""];
    case "screened_out": return [`${t} reads like it's trying to give the AI instructions, so it's coming straight to you`, "warn"];
    case "retrieved": {
      const names = d.titles?.length ? d.titles : d.slugs || [];
      return names.length
        ? [`Found ${plural(names.length, "help article")}: ${names.map(esc).join(", ")}`, ""]
        : ["Found no help articles for this one. The AI is told it has no evidence", "warn"];
    }
    case "prompt_built": return d.steered ? ["Using your correction this time", "you"] : null;
    case "calling_model": return d.attempt > 1 ? [`Asking the AI again (attempt ${d.attempt})`, ""] : ["The AI is working on an answer", ""];
    case "invalid_answer": return [d.retrying ? "The AI's answer came back garbled, so it's asking again" : "The AI gave up after too many garbled answers", "warn"];
    case "analysed": return [`The AI's answer: ${esc(d.category)}, ${esc(lower(d.priority))} priority, ${pct(d.confidence)} sure`, ""];
    case "gate": return d.requires_human
      ? [`Needs you. ${esc(plainReason(d.reason))}`, "warn"]
      : [`Safe to solve on its own. ${esc(plainReason(d.reason))}`, "ok"];
    case "notified": {
      const told = Object.entries(d.channels || {}).filter(([, ok]) => ok).map(([k]) => (k === "telegram" ? "on Telegram" : "through n8n"));
      return told.length ? [`Let you know ${told.join(" and ")}`, ""] : null;
    }
    case "finished":
      if (d.status === "RESOLVED") return [`Done. ${t} is solved`, "ok"];
      if (d.status === "AWAITING_APPROVAL") return [`Done. ${t} is waiting for you below`, "warn"];
      return [`Done. ${t} is ${esc(lower(d.status))}`, ""];
    case "retry_scheduled": return [`Hit a problem, so it will try ${t} again in ${Math.round((d.delay_ms || 0) / 1000)} s`, "warn"];
    case "dead_lettered": return [`Couldn't finish ${e.ticket_id ? t : "a ticket"} after several tries`, "bad"];
    default: return null; // screening, retrieving, run_failed, skipped: the track already shows them
  }
}

function engineerLine(e) {
  const d = e.detail || {};
  const t = e.ticket_id ? `#${e.ticket_id} ` : "";
  switch (e.step) {
    case "picked_up": return [`${t}picked_up “${esc(d.subject)}” retry_count=${d.retry_count ?? 0}`, ""];
    case "screened_out": return [`${t}screened_out: ${esc(d.reason)}`, "warn"];
    case "retrieving": return [`${t}retrieving via ${esc(d.via)}`, ""];
    case "retrieved": return [`${t}retrieved [${(d.slugs || []).map(esc).join(", ") || "nothing"}]`, (d.slugs || []).length ? "" : "warn"];
    case "prompt_built": return [`${t}prompt_built articles=${d.articles} steered=${d.steered}`, ""];
    case "calling_model": return [`${t}calling_model ${esc(d.model)} attempt=${d.attempt}`, ""];
    case "invalid_answer": return [`${t}invalid_answer attempt=${d.attempt}: ${esc(d.error)}`, "warn"];
    case "analysed": return [`${t}analysed ${esc(d.category)}/${esc(d.priority)} confidence=${d.confidence}`, ""];
    case "gate": return [`${t}gate requires_human=${d.requires_human}: ${esc(d.reason)}`, d.requires_human ? "warn" : "ok"];
    case "notified": return [`${t}notified ${esc(JSON.stringify(d.channels))}`, ""];
    case "finished": return [`${t}finished status=${esc(d.status)} (committed)`, d.status === "RESOLVED" ? "ok" : ""];
    case "retry_scheduled": return [`${t}retry_scheduled attempt=${d.attempt} delay_ms=${d.delay_ms}`, "warn"];
    case "dead_lettered": return [`${t}dead_lettered: ${esc(d.reason)}`, "bad"];
    case "run_failed": return [`${t}run_failed: ${esc(d.error)}`, "bad"];
    case "skipped": return [`${t}skipped, already ${esc(d.status)}`, ""];
    default: return [`${t}${esc(e.step)}`, ""];
  }
}

function feedLi(item, fresh) {
  let html, tone, sub = "";
  if (item.you) {
    html = esc(item.you);
    tone = "you";
  } else {
    const line = engineer() ? engineerLine(item.event) : plainLine(item.event);
    if (!line) return null;
    [html, tone] = line;
    const lane = state.workers.get(item.event.worker);
    sub = lane ? esc(workerName(lane)) : "";
  }
  const li = document.createElement("li");
  if (fresh) li.className = "fresh";
  li.innerHTML = `<span class="at">${clock(item.at)}</span><span><span class="${esc(tone)}">${html}</span>${sub ? `<div class="sub">${sub}</div>` : ""}</span>`;
  return li;
}

function pushFeed(item) {
  state.feed.push(item);
  if (state.feed.length > 300) state.feed.splice(0, state.feed.length - 300);
  const li = feedLi(item, true);
  if (!li) return;
  const ul = $("#feed");
  $(".feed-empty", ul)?.remove();
  ul.prepend(li);
  while (ul.children.length > FEED_LIMIT) ul.lastElementChild.remove();
}

function drawFeed() {
  const ul = $("#feed");
  ul.innerHTML = "";
  const items = state.feed.map((i) => feedLi(i, false)).filter(Boolean).reverse().slice(0, FEED_LIMIT);
  if (!items.length) {
    ul.innerHTML = `<li class="feed-empty"><span></span><span class="muted">Nothing yet. Send a sample ticket above and watch it go.</span></li>`;
    return;
  }
  ul.append(...items);
}

async function refreshRecord() {
  if (!engineer()) return;
  let rows;
  try { rows = await api("/overseer/recent?limit=50"); } catch { return; }
  $("#record").innerHTML = rows.map((r) => {
    const d = r.detail || {};
    const bits = [d.reason, d.status, d.slugs && (d.slugs.join(", ") || "no articles"), d.instruction && `“${d.instruction}”`, d.error].filter(Boolean).map(esc);
    return `<li><span class="at">${clock(r.created_at)}</span><span><b>${r.ticket_id ? `#${r.ticket_id}` : ""}</b> ${esc(r.event_type.replace(/_/g, " "))}${bits.length ? `<div class="sub">${bits.join(" · ")}</div>` : ""}</span></li>`;
  }).join("") || `<li><span></span><span class="muted">No audit rows yet.</span></li>`;
}

// --- Team: the org chart -----------------------------------------------------

const KIND = { person: "Person", agent: "AI agent", tool: "Tool", bot: "Bot", automation: "Automation" };
const ICON = {
  person: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c1-4 4-6 7-6s6 2 7 6"/></svg>`,
  agent: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="5" y="7" width="14" height="11" rx="3"/><path d="M12 3v4M9 12h.01M15 12h.01M9.5 15.5h5"/></svg>`,
  tool: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/></svg>`,
  bot: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 5L3 11.5l6.5 2L12 20l3-5.5z"/><path d="M9.5 13.5L15 9"/></svg>`,
  automation: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
};

// The triage agent's card is the one thing on this tab the stream knows better
// than the database, so its status comes from there when there is any.
function triageLive() {
  const busy = busyLanes();
  if (busy.length) return { state: "busy", status: doing(busy[0]) };
  if (state.brokerWorkers === 0) return { state: "down", status: "Not running: no worker is connected" };
  if (state.brokerWorkers) return { state: "up", status: "Waiting for work" };
  return null;
}

function memberHtml(m) {
  let st = m.state, status = m.status;
  const facts = [...m.facts];
  if (m.key === "triage") {
    const live = triageLive();
    if (live) ({ state: st, status } = live);
    if (state.brokerWorkers != null) facts.unshift(`${plural(state.brokerWorkers, "worker")} connected`);
  }
  if (m.key === "you" && state.board) {
    const n = state.board.waiting.length;
    st = n ? "waiting" : "up";
    status = n ? `${plural(n, "ticket")} waiting for you` : "Nothing is waiting for you";
  }
  const routines = (state.team.routines || []).filter((r) => r.owner === m.key);
  return `
    <div class="member ${esc(m.kind)} s-${esc(st)}" data-key="${esc(m.key)}">
      <div class="head">
        <span class="icon">${ICON[m.kind] || ""}</span>
        <div><div class="name">${esc(m.name)}</div><div class="kind">${esc(KIND[m.kind] || m.kind)}</div></div>
      </div>
      <div class="role">${esc(m.role)}</div>
      <div class="status"><i></i><span>${esc(status)}</span></div>
      <ul class="facts">
        ${facts.map((f) => `<li>${esc(f)}</li>`).join("")}
        ${m.last_active ? `<li>Last active ${esc(ago(m.last_active))}</li>` : ""}
      </ul>
      ${routines.length ? `<div class="routines-of">Runs ${routines.map((r) => `<a href="#routines" data-goto="routines">${esc(r.name)}</a>`).join(", ")}</div>` : ""}
      <div class="tech eng mono">${m.tech.map((x) => `<div>${esc(x)}</div>`).join("")}</div>
    </div>`;
}

// Where each member's card is, relative to the chart.
function orgRect(key) {
  const org = $("#org"), card = $(`.member[data-key="${key}"]`, org);
  if (!org || !card) return null;
  const o = org.getBoundingClientRect(), r = card.getBoundingClientRect();
  return { l: r.left - o.left, r: r.right - o.left, t: r.top - o.top, b: r.bottom - o.top, cx: (r.left + r.right) / 2 - o.left };
}

// A route between two members along the chart's lines: down to a report,
// up to a manager, or across the shared rail to a colleague.
function orgPoints(a, b) {
  const members = Object.fromEntries((state.team?.members || []).map((m) => [m.key, m]));
  const A = orgRect(a), B = orgRect(b);
  if (!A || !B || !members[a] || !members[b]) return null;
  if (members[b].reports_to === a) {
    const my = (A.b + B.t) / 2;
    return [[A.cx, A.b], [A.cx, my], [B.cx, my], [B.cx, B.t]];
  }
  if (members[a].reports_to === b) return orgPoints(b, a)?.reverse() ?? null;
  const boss = members[a].reports_to;
  if (boss && boss === members[b].reports_to) {
    const P = orgRect(boss);
    const my = (P.b + Math.min(A.t, B.t)) / 2;
    return [[A.cx, A.t], [A.cx, my], [B.cx, my], [B.cx, B.t]];
  }
  return null;
}
const toD = (pts) => pts && `M${pts.map((p) => p.map(Math.round).join(",")).join(" L")}`;

function layoutOrgWires() {
  const svg = $("#org-wires"), org = $("#org");
  if (!svg || !org || org.offsetParent === null) return;
  const o = org.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${o.width} ${o.height}`);
  svg.innerHTML = (state.team?.members || [])
    .filter((m) => m.reports_to)
    .map((m) => `<path data-from="${m.reports_to}" data-to="${m.key}" d="${toD(orgPoints(m.reports_to, m.key))}"/>`)
    .join("");
}

function ping(key, tone = "") {
  if (calm()) return;
  const card = $(`.member[data-key="${key}"]`);
  if (!card) return;
  card.classList.remove("ping", "ok");
  void card.offsetWidth;
  card.classList.add("ping");
  if (tone) card.classList.add(tone);
}

// One member handing something to another, drawn only while the chart is on
// screen. The receiving card pings when the pulse arrives.
function handoff(a, b, { tone = "", delay = 0 } = {}) {
  if (!teamVisible()) return;
  const d = toD(orgPoints(a, b));
  if (!d) return;
  const wire = $(`#org-wires path[data-from="${a}"][data-to="${b}"]`) || $(`#org-wires path[data-from="${b}"][data-to="${a}"]`);
  pulseAlong($("#org-wires"), d, { tone, delay, ms: 700, lit: wire, arrive: () => ping(b, tone === "ok" ? "ok" : "") });
}

// Which real events are a handoff between members of the team.
function teamSignal(e) {
  const d = e.detail || {};
  if (e.step === "retrieving") handoff("triage", "knowledge");
  else if (e.step === "retrieved") handoff("knowledge", "triage");
  else if (e.step === "finished" && d.status === "AWAITING_APPROVAL") handoff("triage", "you", { tone: "warn" });
  else if (e.step === "finished" && d.status === "RESOLVED") ping("triage", "ok");
  else if (e.step === "notified") {
    if (d.channels?.n8n) { handoff("triage", "automations"); handoff("automations", "you", { delay: 750 }); }
    if (d.channels?.telegram) { handoff("triage", "telegram"); handoff("telegram", "you", { delay: 750 }); }
  }
}

function drawTeam() {
  const members = state.team?.members || [];
  const root = members.find((m) => !m.reports_to);
  if (!root) return;
  const under = (key) => members.filter((m) => m.reports_to === key);
  const node = (m) => {
    const kids = under(m.key);
    return `<li class="node">${memberHtml(m)}${kids.length ? `<ul class="tree">${kids.map(node).join("")}</ul>` : ""}</li>`;
  };
  $("#org-tree").innerHTML = `${memberHtml(root)}<ul class="tree">${under(root.key).map(node).join("")}</ul>`;
  layoutOrgWires();
}

// --- Routines ----------------------------------------------------------------

const ROUTINE_STATE = { ok: "Seen working", quiet: "Needs a look", unknown: "Can't see from here" };

function drawRoutines() {
  const routines = state.team?.routines || [];
  const owners = Object.fromEntries((state.team?.members || []).map((m) => [m.key, m.name]));
  const quiet = routines.filter((r) => r.state === "quiet").length;
  const count = $("#routines-count");
  count.textContent = quiet;
  count.hidden = !quiet;

  $("#routines").innerHTML = routines.map((r) => `
    <article class="routine ${esc(r.state)}">
      <div><span class="state-badge ${esc(r.state)}"><i></i>${esc(ROUTINE_STATE[r.state] || r.state)}</span></div>
      <div>
        <div class="name">${esc(r.name)}</div>
        <div class="when-run">${esc(r.trigger)}</div>
        <div class="does">${esc(r.does)}</div>
        ${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}
        <div class="tech eng mono">${r.tech.map((x) => `<div>${esc(x)}</div>`).join("")}</div>
      </div>
      <div>
        <div class="evidence">${r.evidence
          ? `${esc(r.evidence)}${r.last_evidence ? ` <span class="ago">· ${esc(ago(r.last_evidence))}</span>` : ""}`
          : `<span class="muted">No trace yet</span>`}</div>
        <div class="owner">Run by ${esc(owners[r.owner] || r.owner)}</div>
      </div>
    </article>`).join("");
}

async function refreshTeam() {
  try {
    state.team = await api("/overseer/team");
  } catch {
    return;
  }
  if (teamVisible()) drawTeam();
  drawRoutines();
  drawPet();
}

let teamTimer = null;
function refreshTeamSoon(ms = 1000) {
  clearTimeout(teamTimer);
  teamTimer = setTimeout(refreshTeam, ms);
}

// --- Pip, the desk pet -----------------------------------------------------------
//
// A status light with a face. Every mood comes from a real signal, in a fixed
// order of importance, and Pip always says which one - so the pet is never
// cheerful over a broken pipeline, and never worried about nothing.

const pet = {
  name: prefs.get("overseer.petName") || "Pip",
  mood: null, line: "", celebrateUntil: 0, lastWork: Date.now(), streamDown: false,
};

const MOUTH = {
  content: "M34 48 Q40 53 46 48",
  watching: "M37.6 49 Q40 52 42.4 49 Q40 46.4 37.6 49 Z",
  celebrating: "M33 46.5 Q40 57 47 46.5 Q40 49.5 33 46.5 Z",
  waiting: "M35.5 49.5 L44.5 49.5",
  worried: "M34 51 Q37 48 40 50 Q43 52 46 49",
  puzzled: "M35 50.5 Q40 47.5 45 50",
  sleepy: "M36.5 49.5 Q40 51 43.5 49.5",
  unwell: "M34 52 Q40 46.5 46 52",
};

const lowerFirst = (t) => t.charAt(0).toLowerCase() + t.slice(1);

function petMood() {
  if (pet.streamDown) return ["unwell", "can't hear the workers: the live connection has dropped."];
  if (state.brokerWorkers === 0) return ["unwell", "feels poorly: no worker is connected, so nothing is getting done."];
  const down = (state.team?.members || []).find((m) => m.state === "down");
  if (down) return ["unwell", `feels poorly: ${down.name} ${down.key === "telegram" ? "has nobody on its allowlist" : "isn't answering"}.`];
  if (Date.now() < pet.celebrateUntil) return ["celebrating", pet.line];
  const busy = busyLanes();
  if (busy.length) return ["watching", `is watching: ${lowerFirst(doing(busy[0]))}.`];
  const waiting = state.board?.waiting || [];
  if (waiting.length) {
    const oldest = waiting[0];
    const minutes = (Date.now() - new Date(oldest.waiting_since).getTime()) / 60000;
    if (minutes > 60) {
      return ["worried", `is worried: ${plural(waiting.length, "ticket")} ${waiting.length === 1 ? "is" : "are"} waiting for you, the oldest for ${waited(oldest.waiting_since)}.`];
    }
    return ["waiting", `is keeping an eye on ${plural(waiting.length, "ticket")} waiting for you.`];
  }
  const quiet = (state.team?.routines || []).find((r) => r.state === "quiet");
  if (quiet) return ["puzzled", `is puzzled: the ${quiet.name.toLowerCase()} seems to have gone quiet.`];
  const hour = new Date().getHours();
  if (hour >= 22 || hour < 7 || Date.now() - pet.lastWork > 10 * 60000) return ["sleepy", "is having a nap. All quiet."];
  return ["content", "is happy. Nothing needs you."];
}

// Pip looks at whatever is happening: the busy token, or the tickets waiting.
function lookAtWork(mood) {
  const petEl = $("#pet");
  let dx = 0, dy = 0;
  const target = busyLanes()[0]?.el || (mood === "worried" || mood === "waiting" ? $('[data-stop="needs"]') : null);
  if (target && target.offsetParent !== null) {
    const a = petEl.getBoundingClientRect(), b = target.getBoundingClientRect();
    const vx = b.left + b.width / 2 - (a.left + a.width / 2), vy = b.top + b.height / 2 - (a.top + a.height / 2);
    const len = Math.hypot(vx, vy) || 1;
    dx = (vx / len) * 2.3;
    dy = (vy / len) * 2.3;
  }
  for (const pupil of $$(".pupil", petEl)) pupil.style.transform = `translate(${dx.toFixed(2)}px, ${dy.toFixed(2)}px)`;
}

function drawPet() {
  const [mood, line] = petMood();
  const el = $("#pet");
  if (pet.mood !== mood) {
    el.classList.remove(`mood-${pet.mood}`);
    el.classList.add(`mood-${mood}`);
    $(".mouth", el).setAttribute("d", MOUTH[mood]);
    pet.mood = mood;
  }
  $("#pet-line").textContent = line;
  const name = $("#pet-name");
  if (name && name.textContent !== pet.name) name.textContent = pet.name;
  lookAtWork(mood);
  const b = state.board;
  if (b) {
    $("#pet-tally").innerHTML = `Today: <b>${b.solved_automatically_today}</b> solved on its own, <b>${b.solved_today - b.solved_automatically_today}</b> with your help`;
  }
}

function burst(kind) {
  if (calm()) return;
  const wrap = $(".pet-wrap");
  const colours = ["var(--ok)", "var(--accent)", "var(--warn)", "#ff8fa3", "var(--info)"];
  const n = kind === "confetti" ? 16 : 1;
  for (let i = 0; i < n; i++) {
    const bit = document.createElement("span");
    if (kind === "confetti") {
      bit.className = "fx confetti";
      bit.style.background = colours[i % colours.length];
      const angle = (Math.PI * 2 * i) / n + Math.random() * 0.4;
      const dist = 34 + Math.random() * 26;
      bit.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
      bit.style.setProperty("--dy", `${Math.sin(angle) * dist - 12}px`);
      bit.style.setProperty("--rot", `${Math.round(Math.random() * 540 - 270)}deg`);
    } else {
      bit.className = "fx";
      bit.textContent = "♥";
      bit.style.color = "#ff6b8a";
      bit.style.setProperty("--dx", `${Math.round(Math.random() * 16 - 8)}px`);
      bit.style.setProperty("--dy", "-38px");
      bit.style.setProperty("--rot", "0deg");
    }
    bit.style.left = "34px";
    bit.style.top = "20px";
    wrap.appendChild(bit);
    setTimeout(() => bit.remove(), 1300);
  }
}

function celebrate(line) {
  pet.line = line;
  pet.celebrateUntil = Date.now() + 4500;
  burst("confetti");
  drawPet();
  setTimeout(drawPet, 4600);
}

function celebrateIfSolved(e) {
  if (e.step === "finished" && e.detail?.status === "RESOLVED") celebrate(`is delighted: #${e.ticket_id} was solved on its own!`);
}

$("#pet").addEventListener("click", () => {
  burst("heart");
  const el = $("#pet");
  el.classList.remove("hello");
  void el.offsetWidth;
  el.classList.add("hello");
});

// Rename: click the name, type, Enter. Kept in this browser only.
$("#pet-name").addEventListener("click", () => {
  const btn = $("#pet-name");
  const input = document.createElement("input");
  input.className = "pet-name-input";
  input.value = pet.name;
  input.maxLength = 20;
  input.setAttribute("aria-label", "Pet name");
  btn.replaceWith(input);
  input.focus();
  input.select();
  const done = (save) => {
    if (save && input.value.trim()) {
      pet.name = input.value.trim();
      prefs.set("overseer.petName", pet.name);
    }
    btn.textContent = pet.name;
    input.replaceWith(btn);
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") done(true);
    if (ev.key === "Escape") done(false);
  });
  input.addEventListener("blur", () => done(true));
});

// --- Tabs, view switch, identity, samples -------------------------------------

function showTab(name) {
  for (const b of $$("nav.tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const p of $$("[role=tabpanel]")) p.hidden = p.id !== `tab-${name}`;
  if (name === "floor") requestAnimationFrame(layoutTrack);
  if (name === "team") drawTeam();
}

$("nav.tabs").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});

document.addEventListener("click", (ev) => {
  const go = ev.target.closest("[data-goto]");
  if (!go) return;
  ev.preventDefault();
  showTab(go.dataset.goto);
});

function showCard(id) {
  const card = $(`.ticket-card[data-id="${id}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("arrived");
  void card.offsetWidth; // restart the animation
  card.classList.add("arrived");
}

$("#recent-needs").addEventListener("click", (ev) => {
  const a = ev.target.closest("[data-card]");
  if (!a) return;
  ev.preventDefault();
  showCard(a.dataset.card);
});

$("#go-decide").addEventListener("click", (ev) => {
  ev.preventDefault();
  $("#decide-title").scrollIntoView({ behavior: "smooth", block: "start" });
});

const motionBox = $("#motion");
function applyMotion() {
  document.body.classList.toggle("calm", calm());
  motionBox.checked = !calm();
}
motionBox.addEventListener("change", () => {
  motionPref = motionBox.checked ? "on" : "off";
  prefs.set("overseer.motion", motionPref);
  applyMotion();
});
systemCalm.addEventListener?.("change", applyMotion);

const engBox = $("#engineer");
engBox.checked = prefs.get("overseer.engineer") === "1";
function applyEngineer() {
  document.body.classList.toggle("engineer", engBox.checked);
  prefs.set("overseer.engineer", engBox.checked ? "1" : "0");
  drawChips();
  drawWorkers();
  drawFeed();
  drawTeam();
  drawRoutines();
  refreshRecord();
  requestAnimationFrame(layoutTrack);
}
engBox.addEventListener("change", applyEngineer);

async function loadPeople() {
  try {
    const users = await api("/users");
    const saved = prefs.get("overseer.me");
    $("#me").innerHTML = users.map((u) => `<option value="${u.id}" ${String(u.id) === saved ? "selected" : ""}>${esc(u.name)}</option>`).join("");
  } catch {}
}
$("#me").addEventListener("change", () => prefs.set("overseer.me", $("#me").value));

// Synthetic tickets, each aimed at a real help article so the run is worth
// watching. The payroll one always comes to you: payroll is on the sensitive
// list, whatever the AI says, which makes it the one to show the gate with.
const SAMPLES = [
  ["VPN drops", "VPN keeps dropping", "Since changing my password this morning the VPN disconnects every few minutes.", ""],
  ["Printer stuck", "Printer queue is stuck", "Nothing I send to the second-floor printer comes out. The queue shows three jobs stuck.", ""],
  ["Payroll access", "Payroll portal says access denied", "The payroll portal says access denied when I try to view my payslip.", "comes to you"],
  ["Laptop won't charge", "Laptop will not charge", "My laptop says plugged in, not charging, and the battery is down to 12%.", ""],
];

$("#samples").innerHTML = SAMPLES.map(([label, , , hint], i) =>
  `<button class="pill-btn" data-sample="${i}">${esc(label)}${hint ? ` <span class="hint">· ${esc(hint)}</span>` : ""}</button>`).join(" ");

$("#samples").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-sample]");
  if (!btn) return;
  const [, subject, description] = SAMPLES[Number(btn.dataset.sample)];
  btn.disabled = true;
  try {
    const t = await api("/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submitted_by_id: Number($("#me").value), subject, description }),
    });
    state.subjects.set(t.id, t.subject);
    pushFeed({ at: new Date().toISOString(), you: `You sent a sample ticket, #${t.id} “${subject}”` });
    showTab("floor");
    travel("in", "queue", "");
  } catch (err) {
    pushFeed({ at: new Date().toISOString(), you: `Couldn't send the sample: ${err.message}` });
  } finally {
    setTimeout(() => { btn.disabled = false; }, 800);
  }
});

// --- The stream --------------------------------------------------------------

function connect() {
  const es = new EventSource("/overseer/stream");
  const live = $("#live"), text = $("#live-text");
  es.onopen = () => { live.className = "live on"; text.textContent = "Live"; pet.streamDown = false; drawPet(); };
  es.onerror = () => { live.className = "live off"; text.textContent = "Reconnecting…"; pet.streamDown = true; drawPet(); };
  es.addEventListener("progress", (m) => onProgress(JSON.parse(m.data)));
  es.addEventListener("queues", (m) => onQueues(JSON.parse(m.data)));
  es.addEventListener("unavailable", (m) => {
    live.className = "live off";
    text.textContent = JSON.parse(m.data).detail;
    pet.streamDown = true;
    drawPet();
  });
}

// --- Start -------------------------------------------------------------------

new ResizeObserver(() => layoutTrack()).observe($("#track"));
new ResizeObserver(() => layoutOrgWires()).observe($("#org"));
document.fonts?.ready.then(layoutTrack);

applyMotion();
applyEngineer();
drawFeed();
loadPeople();
connect();
refreshBoard();
refreshTeam();

setInterval(tick, 500);
setInterval(drawPet, 30000); // moods that depend on the clock, like a night-time nap
setInterval(refreshBoard, 5000);
setInterval(refreshTeam, 15000);
setInterval(refreshRecord, 3000);
// Forget decided tickets after a while; by then every poll has caught up.
setInterval(() => {
  for (const [id, at] of state.decided) if (Date.now() - at > 20000) state.decided.delete(id);
}, 5000);
