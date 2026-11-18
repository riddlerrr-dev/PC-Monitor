"use strict";
/* PC Monitor front end: plain JS, no build step. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const DAYS = [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"], ["sat", "Saturday"], ["sun", "Sunday"]];

// Event categories for the chart annotations (colour = category, never colour alone: labels + legend too)
const CATEGORIES = [
  { id: "alert", label: "Turned on outside hours / problems", color: "--ev-alert", types: ["power_on_outside", "ssh_unreachable", "shutdown_failed"] },
  { id: "enforce", label: "Warnings & shutdowns", color: "--ev-enforce", types: ["warning", "shutdown_sent"] },
  { id: "power", label: "Turned on / off", color: "--ev-power", types: ["power_on", "power_off", "monitor_started", "ssh_restored"] },
  { id: "change", label: "Changes made here", color: "--ev-change", dash: true,
    types: ["block_on", "block_off", "extension", "schedule_changed", "holiday_on", "holiday_off", "settings_changed", "enforcement_on", "enforcement_off"] },
];
const catOf = (type) => CATEGORIES.find((c) => c.types.includes(type)) || CATEGORIES[2];

const state = { me: null, status: null, history: null, range: "24h", hidden: new Set(), labels: {}, settings: null };
let timelineChart = null, dailyChart = null;

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Requested-With": "pcmonitor" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("Signed out"); }
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    let msg = data?.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg?.replace(/^Value error, /, "")).join("; ");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 2800);
}

async function act(fn, okMsg) {
  try { await fn(); if (okMsg) toast(okMsg); await refreshStatus(); }
  catch (e) { toast(e.message, true); }
}

// ---------- time helpers (static/tz.js) ----------
const { fmt, parts, fmtTime, fmtWhen, fmtDate, localInput, tzAt, setZone } = PCTZ;
function ago(d) {
  const s = Math.round((Date.now() - d) / 1000);
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)} min ago`;
}
function hoursLabel(h) {
  if (h < 1) return `${Math.round(h * 60)}m`;
  const hh = Math.floor(h), mm = Math.round((h - hh) * 60);
  return mm ? `${hh}h ${mm}m` : `${hh}h`;
}

// ---------- auth ----------
function showLogin(needsSetup = false) {
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  $("#setup-confirm").hidden = !needsSetup;
  $("#login-form").dataset.setup = needsSetup ? "1" : "";
  $("#login-intro").textContent = needsSetup ? "First run: create the admin account." : "Sign in to manage the PC.";
  $("#login-btn").textContent = needsSetup ? "Create account" : "Sign in";
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const err = $("#login-error");
  err.hidden = true;
  try {
    if (e.target.dataset.setup) {
      if (f.get("password") !== f.get("confirm")) throw new Error("Passwords don't match");
      await api("/api/setup", { method: "POST", body: { username: f.get("username"), password: f.get("password"), is_admin: true } });
    } else {
      await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    }
    e.target.reset();
    await boot();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});

$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ---------- tabs ----------
$$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(tab) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + tab));
  if (tab === "schedule" || tab === "settings") loadSettings();
  if (tab === "users") loadUsers();
  if (tab === "overview") refreshHistory();
  history.replaceState(null, "", "#" + tab);
}

// ---------- status ----------
async function refreshStatus() {
  const s = await api("/api/status");
  state.status = s;
  $("#pc-name").textContent = s.pc_name;

  const dot = $("#status-dot");
  dot.className = "status-dot " + (s.online ? (s.ssh_ok ? "on" : "nossh") : "off");
  $("#status-label").textContent = s.online ? (s.ssh_ok ? "On" : s.ssh_port_open ? "On (SSH login failing)" : "On (can't reach SSH)") : "Off";
  $("#status-checked").textContent = s.checked_at ? "Checked " + ago(new Date(s.checked_at)) : "";
  $("#status-summary").textContent = s.summary || "";

  const p = $("#pending");
  if (s.pending_shutdown_at) {
    const at = new Date(s.pending_shutdown_at);
    p.hidden = false;
    p.textContent = at > Date.now() ? `Shutdown scheduled for ${fmtTime(at)}` : "Shutdown in progress…";
  } else p.hidden = true;

  const chips = [];
  if (s.holiday?.applies_today) chips.push(`<span class="chip">Holiday mode${s.holiday.until ? " until " + esc(fmtDate(s.holiday.until, { day: "numeric", month: "short" })) : ""}</span>`);
  if (s.extension?.until && new Date(s.extension.until) > Date.now()) chips.push(`<span class="chip">Extra time until ${fmtTime(new Date(s.extension.until))}${s.extension.by ? " · " + esc(s.extension.by) : ""}</span>`);
  if (!s.enforcement_enabled) chips.push(`<span class="chip warn">Monitor only</span>`);
  if (s.dry_run) chips.push(`<span class="chip warn">Dry run: commands aren't really sent</span>`);
  $("#status-chips").innerHTML = chips.join("");

  $("#problems").innerHTML = (s.problems || []).map((t) => `<div class="banner">${esc(t)}</div>`).join("");

  // Block button
  const blocked = !!s.block?.effective;
  const bb = $("#block-btn");
  bb.setAttribute("aria-pressed", blocked);
  $("#block-btn-label").textContent = blocked ? "Blocked. Tap to unblock" : "Block PC";
  $("#block-opts").hidden = blocked;
  $("#block-desc").textContent = blocked
    ? (s.block.until ? `Blocked until ${fmtWhen(new Date(s.block.until))}` : "Blocked until someone unblocks it") + (s.block.by ? ` (by ${s.block.by})` : "") + "."
    : "Blocked means it gets shut down whenever it's switched on, whatever the schedule says.";

  const ext = s.extension?.until && new Date(s.extension.until) > Date.now();
  $("#ext-cancel").hidden = !ext;
  $("#ext-desc").textContent = ext ? `Allowed until ${fmtWhen(new Date(s.extension.until))} (given by ${s.extension.by}).` : "Extra time is added to the end of today's allowed time.";
}

// ---------- controls ----------
$("#block-until").addEventListener("change", (e) => {
  const c = $("#block-custom");
  c.hidden = e.target.value !== "custom";
  if (!c.hidden && !c.value) c.value = localInput(tzAt(1, 7));
});
$("#block-btn").addEventListener("click", () => {
  const blocked = $("#block-btn").getAttribute("aria-pressed") === "true";
  if (blocked) return act(() => api("/api/block", { method: "POST", body: { active: false } }), "Unblocked");
  const v = $("#block-until").value;
  let until = null;
  if (v === "1h") until = localInput(new Date(Date.now() + 36e5));
  else if (v === "morning") until = localInput(tzAt(1, 7));
  else if (v === "custom") until = $("#block-custom").value || null;
  act(() => api("/api/block", { method: "POST", body: { active: true, until } }), "Blocked");
});
$$("[data-ext]").forEach((b) => b.addEventListener("click", () =>
  act(() => api("/api/extension", { method: "POST", body: { minutes: +b.dataset.ext } }), `Added ${b.textContent.replace("+", "")}`)));
$("#ext-until-btn").addEventListener("click", () => {
  const v = $("#ext-until").value;
  if (!v) return toast("Pick a time first", true);
  act(() => api("/api/extension", { method: "POST", body: { until: v } }), "Extra time given");
});
$("#ext-cancel").addEventListener("click", () => act(() => api("/api/extension", { method: "DELETE" }), "Extra time cancelled"));
$("#shutdown-btn").addEventListener("click", () => {
  if (!confirm("Shut the PC down? It gets a 60-second warning first.")) return;
  act(() => api("/api/shutdown", { method: "POST", body: { seconds: 60 } }), "Shutdown sent");
});
$("#message-btn").addEventListener("click", () => {
  const m = prompt("Message to show on the PC (needs Windows Pro; Home doesn't support pop-up messages):");
  if (m) act(() => api("/api/message", { method: "POST", body: { message: m } }), "Message sent");
});

// ---------- history ----------
$$("#range button").forEach((b) => b.addEventListener("click", () => {
  state.range = b.dataset.range;
  $$("#range button").forEach((x) => x.classList.toggle("active", x === b));
  refreshHistory();
}));

function renderFilters() {
  $("#filters").innerHTML = CATEGORIES.map((c) =>
    `<button class="filter" data-cat="${c.id}" aria-pressed="${!state.hidden.has(c.id)}"><i class="line" style="background:${css(c.color)}"></i>${esc(c.label)}</button>`).join("");
  $$("#filters .filter").forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.cat;
    state.hidden.has(id) ? state.hidden.delete(id) : state.hidden.add(id);
    renderFilters();
    drawTimeline();
  }));
  $("#legend").innerHTML =
    `<span><i style="background:${css("--on")}"></i>PC on</span>` +
    `<span><i style="background:${css("--nossh")}"></i>On, but SSH not answering</span>` +
    `<span><i style="background:repeating-linear-gradient(45deg,${css("--grid")} 0 2px,transparent 2px 5px);border:1px solid ${css("--border")}"></i>No data (monitor offline)</span>`;
}

async function refreshHistory() {
  try {
    const h = await api("/api/history?range=" + state.range);
    state.history = h;
    state.labels = h.event_labels;
    renderTiles(h);
    drawTimeline();
    drawDaily(h);
  } catch (e) { if (e.message !== "Signed out") console.error(e); }
  loadEvents();
}

function renderTiles(h) {
  const rangeLabel = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days", "90d": "last 90 days" }[state.range];
  const t = (k, v, s) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  $("#tiles").innerHTML =
    t("Time on", hoursLabel(h.stats.on_hours), rangeLabel) +
    t("Turned on outside hours", h.stats.turned_on_outside, rangeLabel) +
    t("Shutdowns sent", h.stats.shutdowns, rangeLabel) +
    t("SSH problems", h.stats.ssh_problems, h.stats.ssh_problems ? "check SSH on the PC" : rangeLabel);
}

function segmentData(segs) {
  // Two series of rectangles: on (y=1) and on-without-SSH (y=1); off = 0; unknown = gap
  const on = [], nossh = [];
  for (const [a, b, st] of segs) {
    const A = a * 1000, B = b * 1000;
    const yOn = st === "on" ? 1 : st === "unknown" ? null : 0;
    const yNo = st === "on_no_ssh" ? 1 : null;
    on.push({ x: A, y: yOn }, { x: B, y: yOn });
    nossh.push({ x: A, y: yNo }, { x: B, y: yNo });
    if (yOn === null) on.push({ x: B, y: null });
  }
  return { on, nossh };
}

function drawTimeline() {
  const h = state.history;
  if (!h) return;
  const { on, nossh } = segmentData(h.segments);
  const visible = h.events.filter((e) => !state.hidden.has(catOf(e.type).id));
  const annotations = {};
  // Hatched bands where there's no data
  h.segments.filter((s) => s[2] === "unknown").forEach((s, i) => {
    annotations["u" + i] = { type: "box", xMin: s[0] * 1000, xMax: s[1] * 1000, yMin: 0, yMax: 1.25,
      backgroundColor: hatch(), borderWidth: 0, drawTime: "beforeDatasetsDraw" };
  });
  visible.forEach((e, i) => {
    const c = catOf(e.type);
    const x = e.ts * 1000;
    annotations["e" + i] = { type: "line", xMin: x, xMax: x, yMin: 0, yMax: 1.25,
      borderColor: css(c.color), borderWidth: 2, borderDash: c.dash ? [4, 3] : undefined, drawTime: "afterDatasetsDraw" };
    annotations["p" + i] = { type: "point", xValue: x, yValue: 1.25, radius: 4.5,
      backgroundColor: css(c.color), borderColor: css("--surface"), borderWidth: 2, drawTime: "afterDatasetsDraw" };
  });

  const grid = css("--grid"), muted = css("--muted");
  const cfg = {
    type: "line",
    data: {
      datasets: [
        { label: "PC on", data: on, borderColor: css("--on"), backgroundColor: css("--on") + "33", fill: "origin", borderWidth: 2, pointRadius: 0, spanGaps: false, tension: 0 },
        { label: "On, SSH not answering", data: nossh, borderColor: css("--nossh"), backgroundColor: css("--nossh") + "55", fill: "origin", borderWidth: 2, pointRadius: 0, spanGaps: false, tension: 0 },
      ],
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "nearest", intersect: false },
      layout: { padding: { top: 8, right: 8 } },
      scales: {
        // Real timestamps on the axis (so the repeated hour when clocks go back doesn't fold over);
        // only the labels are written in the server's time zone
        x: { type: "time", min: h.start * 1000, max: h.end * 1000, grid: { color: grid }, border: { display: false },
             ticks: { color: muted, maxRotation: 0, autoSkipPadding: 18,
                      callback: (v) => state.range === "24h" ? fmtTime(new Date(v))
                                       : fmt({ weekday: "short", day: "numeric", month: "short" }).format(new Date(v)) },
             time: { unit: state.range === "24h" ? "hour" : "day" } },
        y: { min: 0, max: 1.3, grid: { display: false }, border: { display: false },
             afterBuildTicks: (ax) => { ax.ticks = [{ value: 0 }, { value: 1 }]; },
             ticks: { color: muted, callback: (v) => (v === 1 ? "On" : v === 0 ? "Off" : "") } },
      },
      plugins: { legend: { display: false }, tooltip: { enabled: false }, annotation: { annotations } },
    },
  };
  if (timelineChart) { timelineChart.destroy(); }
  timelineChart = new Chart($("#timeline"), cfg);
}

let _hatch;
function hatch() {
  if (_hatch) return _hatch;
  const c = document.createElement("canvas");
  c.width = c.height = 8;
  const g = c.getContext("2d");
  g.strokeStyle = css("--grid");
  g.lineWidth = 2;
  g.beginPath(); g.moveTo(0, 8); g.lineTo(8, 0); g.stroke();
  _hatch = g.createPattern(c, "repeat");
  return _hatch;
}

// Custom hover layer: status at the cursor + any events near it
function stateAt(t) {
  for (const [a, b, st] of state.history?.segments || []) if (t >= a && t < b) return st;
  return "unknown";
}
const STATE_TEXT = { on: "On", on_no_ssh: "On, SSH not answering", off: "Off", unknown: "No data" };
function onHover(evt) {
  const chart = timelineChart, tip = $("#tip");
  if (!chart || !state.history) return;
  const rect = chart.canvas.getBoundingClientRect();
  const px = (evt.touches ? evt.touches[0].clientX : evt.clientX) - rect.left;
  const area = chart.chartArea;
  if (px < area.left || px > area.right) { tip.hidden = true; return; }
  const xs = chart.scales.x;
  const t = xs.getValueForPixel(px) / 1000;
  const near = state.history.events
    .filter((e) => !state.hidden.has(catOf(e.type).id))
    .map((e) => ({ e, d: Math.abs(xs.getPixelForValue(e.ts * 1000) - px) }))
    .filter((o) => o.d <= 10)
    .sort((a, b) => a.e.ts - b.e.ts)
    .slice(0, 6);
  const when = near.length ? new Date(near[0].e.ts * 1000) : new Date(t * 1000);
  let html = `<div class="t">${esc(fmtWhen(when))} · ${esc(STATE_TEXT[stateAt(t)])}</div>`;
  for (const { e } of near) {
    html += `<div class="r"><i style="background:${css(catOf(e.type).color)}"></i><span><b>${esc(state.labels[e.type] || e.type)}</b> ${esc(fmtTime(new Date(e.ts * 1000)))}${e.message ? " · " + esc(e.message) : ""}${e.user ? " · " + esc(e.user) : ""}</span></div>`;
  }
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth;
  const left = px + 12 + w > rect.width ? px - 12 - w : px + 12;
  tip.style.left = Math.max(0, left) + "px";
  tip.style.top = "6px";
}
const tl = $("#timeline");
tl.addEventListener("mousemove", onHover);
tl.addEventListener("touchstart", onHover, { passive: true });
tl.addEventListener("touchmove", onHover, { passive: true });
tl.addEventListener("mouseleave", () => ($("#tip").hidden = true));

function drawDaily(h) {
  const card = $("#daily-card");
  card.hidden = state.range === "24h";
  if (card.hidden) return;
  const labels = h.daily_hours.map((d) => d.date);
  const cfg = {
    type: "bar",
    data: { labels, datasets: [{ label: "Hours on", data: h.daily_hours.map((d) => d.hours), backgroundColor: css("--on"),
      borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 28, categoryPercentage: 0.85, barPercentage: 0.9 }] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false }, border: { display: false },
             ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 10,
                      callback: (v, i) => fmtDate(labels[i], state.range === "7d" ? { weekday: "short" } : { day: "numeric", month: "short" }) } },
        y: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => v + "h" } },
      },
      plugins: { legend: { display: false },
        tooltip: { callbacks: {
          title: (items) => fmtDate(labels[items[0].dataIndex], { weekday: "long", day: "numeric", month: "long" }),
          label: (item) => "On for " + hoursLabel(item.raw) } } },
    },
  };
  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart($("#daily"), cfg);
}

async function loadEvents() {
  try {
    const evs = await api("/api/events?limit=200");
    $("#event-rows").innerHTML = evs.map((e) => {
      const c = catOf(e.type);
      return `<tr><td>${esc(fmtWhen(new Date(e.ts * 1000)))}</td><td><span class="ev"><i style="background:${css(c.color)}"></i>${esc(state.labels[e.type] || e.type)}</span></td><td>${esc(e.message)}</td><td>${esc(e.user || "")}</td></tr>`;
    }).join("") || `<tr><td colspan="4" class="muted">Nothing yet.</td></tr>`;
  } catch { /* ignore */ }
}

// ---------- schedule ----------
function ruleRow(key, name, rule) {
  return `<div class="day-row ${rule.mode !== "window" ? "off" : ""}" data-day="${key}">
    <div class="name">${name}</div>
    <select data-f="mode" aria-label="${name} rule">
      <option value="window" ${rule.mode === "window" ? "selected" : ""}>Allowed between</option>
      <option value="all" ${rule.mode === "all" ? "selected" : ""}>Allowed all day</option>
      <option value="none" ${rule.mode === "none" ? "selected" : ""}>Not allowed</option>
    </select>
    <div class="times">
      <input type="time" data-f="start" value="${esc(rule.start)}" aria-label="${name} start">
      <span>to</span>
      <input type="time" data-f="end" value="${esc(rule.end)}" aria-label="${name} end">
      <span class="note"></span>
    </div>
  </div>`;
}
function wireRow(row) {
  const upd = () => {
    const mode = $("[data-f=mode]", row).value;
    row.classList.toggle("off", mode !== "window");
    const s = $("[data-f=start]", row).value, e = $("[data-f=end]", row).value;
    $(".note", row).textContent = mode === "window" && s && e && e <= s ? "(runs past midnight)" : "";
  };
  row.addEventListener("input", upd);
  upd();
}
const readRow = (row) => ({ mode: $("[data-f=mode]", row).value, start: $("[data-f=start]", row).value || "00:00", end: $("[data-f=end]", row).value || "00:00" });

async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  state.labels = s.event_labels;
  $("#days").innerHTML = DAYS.map(([k, n]) => ruleRow(k, n, s.schedule[k])).join("");
  $$("#days .day-row").forEach(wireRow);
  $("#hol-row").innerHTML = ruleRow("holiday", "Every day", s.holiday);
  wireRow($("#hol-row .day-row"));
  $("#hol-enabled").checked = s.holiday.enabled;
  $("#hol-until").value = s.holiday.until || "";
  holidayUi();

  $("#warnings").value = s.warnings.join(", ");
  $("#grace").value = s.grace_seconds;
  $("#enforce").checked = s.enforcement_enabled;
  $("#ntfy-url").value = s.notify.ntfy_url || "";
  $("#ntfy-token").value = s.notify.ntfy_token || "";
  const skip = new Set(["monitor_started", "settings_changed"]);
  $("#notify-events").innerHTML = Object.entries(s.event_labels).filter(([k]) => !skip.has(k)).map(([k, v]) =>
    `<label><input type="checkbox" value="${k}" ${s.notify.events.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("");
  const p = s.pc;
  $("#conn").innerHTML = [
    ["PC", p.name], ["Address", `${p.host || "not set"}:${p.port}`], ["SSH user", p.user || "not set"],
    ["Checks every", p.poll_seconds + " seconds"], ["ARP check (detects SSH being turned off)", p.arp ? "On" : "Off"],
    ["SSH host key", p.host_key], ["Time zone", s.timezone + " (all times on this site)"], ["Dry run", p.dry_run ? "Yes, commands are only logged" : "No"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
function holidayUi() {
  const on = $("#hol-enabled").checked;
  $("#hol-state").textContent = on ? "On" : "Off";
  $("#holiday-card").classList.toggle("on", on);
}
$("#hol-enabled").addEventListener("change", holidayUi);
$("#copy-weekdays").addEventListener("click", () => {
  const mon = readRow($('[data-day="mon"]'));
  ["tue", "wed", "thu"].forEach((d) => {
    const row = $(`[data-day="${d}"]`);
    $("[data-f=mode]", row).value = mon.mode;
    $("[data-f=start]", row).value = mon.start;
    $("[data-f=end]", row).value = mon.end;
    row.dispatchEvent(new Event("input"));
  });
});
$("#schedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const schedule = {};
  $$("#days .day-row").forEach((r) => (schedule[r.dataset.day] = readRow(r)));
  const holiday = { ...readRow($("#hol-row .day-row")), enabled: $("#hol-enabled").checked, until: $("#hol-until").value || null };
  try {
    await api("/api/schedule", { method: "PUT", body: { schedule, holiday } });
    toast("Schedule saved");
    $("#sched-status").textContent = "Saved " + fmtTime(new Date());
    refreshStatus();
  } catch (ex) { toast(ex.message, true); }
});

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const warnings = $("#warnings").value.split(/[,\s]+/).filter(Boolean).map(Number);
  if (warnings.some((n) => !Number.isInteger(n))) return toast("Warning times must be whole minutes", true);
  const body = {
    warnings, grace_seconds: +$("#grace").value || 0, enforcement_enabled: $("#enforce").checked,
    notify: { ntfy_url: $("#ntfy-url").value, ntfy_token: $("#ntfy-token").value,
              events: $$("#notify-events input:checked").map((i) => i.value) },
  };
  try {
    await api("/api/settings", { method: "PUT", body });
    toast("Settings saved");
    $("#settings-status").textContent = "Saved " + fmtTime(new Date());
    loadSettings();
  } catch (ex) { toast(ex.message, true); }
});
$("#test-notify").addEventListener("click", () => act(async () => {
  $("#settings-form").requestSubmit();
  await new Promise((r) => setTimeout(r, 400));
  await api("/api/test-notification", { method: "POST" });
}, "Test notification sent"));

$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/me/password", { method: "POST", body: { current: f.get("current"), new: f.get("new") } });
    e.target.reset();
    toast("Password changed");
  } catch (ex) { toast(ex.message, true); }
});

// ---------- users ----------
async function loadUsers() {
  const users = await api("/api/users");
  $("#user-rows").innerHTML = users.map((u) => `<tr>
    <td><b>${esc(u.username)}</b>${u.id === state.me.id ? ' <span class="muted small">(you)</span>' : ""}</td>
    <td>${u.is_admin ? "Admin" : "Member"}</td>
    <td style="text-align:right;white-space:nowrap">
      <button class="link" data-reset="${u.id}" data-name="${esc(u.username)}">Reset password</button>
      ${u.id === state.me.id ? "" : `&nbsp;·&nbsp;<button class="link" style="color:var(--critical)" data-del="${u.id}" data-name="${esc(u.username)}">Remove</button>`}
    </td></tr>`).join("");
  $$("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
    const pw = prompt(`New password for ${b.dataset.name} (at least 8 characters):`);
    if (!pw) return;
    try { await api(`/api/users/${b.dataset.reset}/password`, { method: "POST", body: { password: pw } }); toast("Password reset"); }
    catch (ex) { toast(ex.message, true); }
  }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Remove ${b.dataset.name}?`)) return;
    try { await api(`/api/users/${b.dataset.del}`, { method: "DELETE" }); toast("Removed"); loadUsers(); }
    catch (ex) { toast(ex.message, true); }
  }));
}
$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/users", { method: "POST", body: { username: f.get("username"), password: f.get("password"), is_admin: f.get("is_admin") === "on" } });
    e.target.reset();
    toast("Added");
    loadUsers();
  } catch (ex) { toast(ex.message, true); }
});

// ---------- boot ----------
let timers = [];
async function boot() {
  const me = await api("/api/me");
  if (me.timezone) setZone(me.timezone);
  if (!me.user) return showLogin(me.needs_setup);
  state.me = me.user;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#who-name").textContent = me.user.username;
  $$("[data-admin]").forEach((el) => (el.hidden = !me.user.is_admin));
  renderFilters();
  if (!$("#ext-until").value) $("#ext-until").value = localInput(tzAt(0, +parts(new Date()).hour + 1));
  await refreshStatus().catch(() => {});
  const tab = location.hash.slice(1);
  switchTab(["overview", "schedule", "settings", "users"].includes(tab) && (tab !== "users" || me.user.is_admin) ? tab : "overview");
  timers.forEach(clearInterval);
  timers = [
    setInterval(() => { if (!document.hidden && !$("#app-view").hidden) refreshStatus().catch(() => {}); }, 10000),
    setInterval(() => { if (!document.hidden && !$("#tab-overview").hidden) refreshHistory(); }, 60000),
  ];
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.me) { refreshStatus().catch(() => {}); refreshHistory(); } });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { _hatch = null; renderFilters(); drawTimeline(); if (state.history) drawDaily(state.history); });
boot();
