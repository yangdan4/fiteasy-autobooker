"use strict";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let TOKEN = localStorage.getItem("fiteasy_token") || "";
let prefsLoaded = false;
let selectedWeekdays = new Set([0, 1, 2, 3, 4, 5, 6]);

const $ = (id) => document.getElementById(id);

function headers() {
  return { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN };
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: headers(), ...opts });
  if (res.status === 401) {
    showGate("Token rejected.");
    throw new Error("unauthorized");
  }
  return res;
}

/* ---------------- gate ---------------- */
function showGate(msg) {
  $("app").classList.add("hidden");
  $("gate").classList.remove("hidden");
  $("gateError").textContent = msg || "";
}
function showApp() {
  $("gate").classList.add("hidden");
  $("app").classList.remove("hidden");
}

$("tokenSave").addEventListener("click", async () => {
  TOKEN = $("tokenInput").value.trim();
  localStorage.setItem("fiteasy_token", TOKEN);
  try {
    const r = await fetch("/api/status", { headers: headers() });
    if (r.ok) { showApp(); boot(); }
    else showGate("Token rejected.");
  } catch { showGate("Connection error."); }
});

/* ---------------- weekdays UI ---------------- */
function renderWeekdays() {
  const box = $("weekdays");
  box.innerHTML = "";
  WEEKDAY_LABELS.forEach((lbl, i) => {
    const el = document.createElement("div");
    el.className = "wd" + (selectedWeekdays.has(i) ? " on" : "");
    el.textContent = lbl;
    el.addEventListener("click", () => {
      if (selectedWeekdays.has(i)) selectedWeekdays.delete(i);
      else selectedWeekdays.add(i);
      el.classList.toggle("on");
    });
    box.appendChild(el);
  });
}

/* ---------------- status + prefs ---------------- */
function fmt(dt) {
  if (!dt) return "—";
  return String(dt).replace("T", " ").slice(0, 16);
}

function applyStatus(data) {
  const s = data.state, p = data.preferences;
  $("shopLabel").textContent = `${data.shop.slug} · shop ${data.shop.id}`;

  const login = $("pillLogin");
  login.textContent = s.logged_in ? "logged in" : "logged out";
  login.className = "pill " + (s.logged_in ? "ok" : "bad");

  const eng = $("pillEngine");
  eng.textContent = p.enabled ? (s.running ? "running" : "starting") : "paused";
  eng.className = "pill " + (p.enabled ? "ok" : "warn");

  const mode = $("pillMode");
  mode.textContent = p.dry_run ? "dry-run" : "LIVE";
  mode.className = "pill " + (p.dry_run ? "warn" : "ok");

  $("stCurrent").textContent = s.current ? s.current.label : "none";
  $("stReservations").textContent = (s.reservations && s.reservations.length)
    ? s.reservations.map((r) => r.label).join(", ")
    : "none";
  $("stAction").textContent = s.last_action || "—";
  $("stCheck").textContent = fmt(s.last_check);
  $("stDetected").textContent = (s.detected ? s.detected.length : 0) + " slot(s)";
  $("stError").textContent = s.last_error || "—";

  $("btnPause").textContent = p.enabled ? "Pause" : "Resume";
  $("btnDry").textContent = p.dry_run ? "Go LIVE" : "Dry-run";

  renderSlots(s.detected || [], s.current, p);

  if (!prefsLoaded) {
    $("latest_time").value = p.latest_time;
    $("earliest_time").value = p.earliest_time;
    $("date_from").value = p.date_from || "";
    $("date_to").value = p.date_to || "";
    $("min_improvement_minutes").value = p.min_improvement_minutes;
    $("rebook_strategy").value = p.rebook_strategy;
    $("poll_min_seconds").value = p.poll_min_seconds;
    $("poll_max_seconds").value = p.poll_max_seconds;
    $("weeks_to_scan").value = p.weeks_to_scan;
    $("manage_existing").value = String(p.manage_existing);
    selectedWeekdays = new Set(p.allowed_weekdays);
    renderWeekdays();
    prefsLoaded = true;
  }
}

function renderSlots(slots, current, prefs) {
  const box = $("slotList");
  if (!slots.length) {
    box.innerHTML = '<p class="muted">No slots detected on the last poll.</p>';
    $("slotsHint").textContent = "";
    return;
  }
  $("slotsHint").textContent = `(${slots.length})`;
  // mark the first slot that satisfies the "before latest" rule as best
  const bestKey = (() => {
    for (const s of slots) {
      const t = s.label.match(/(\d{1,2}):(\d{2})/);
      return s.slot_key; // slots are server-sorted; first is earliest
    }
    return null;
  })();
  box.innerHTML = "";
  slots.slice(0, 60).forEach((s, idx) => {
    const row = document.createElement("div");
    row.className = "slot" + (idx === 0 ? " best" : "");
    const isCurrent = current && current.slot_key === s.slot_key;
    row.innerHTML = `<span>${s.label}</span>` +
      `<span class="tag">${isCurrent ? "booked" : idx === 0 ? "earliest" : ""}</span>`;
    box.appendChild(row);
  });
}

function collectPrefs() {
  return {
    latest_time: $("latest_time").value || "18:00",
    earliest_time: $("earliest_time").value || "00:00",
    date_from: $("date_from").value || null,
    date_to: $("date_to").value || null,
    allowed_weekdays: [...selectedWeekdays].sort((a, b) => a - b),
    min_improvement_minutes: parseInt($("min_improvement_minutes").value || "10", 10),
    rebook_strategy: $("rebook_strategy").value,
    poll_min_seconds: parseFloat($("poll_min_seconds").value || "8"),
    poll_max_seconds: parseFloat($("poll_max_seconds").value || "12"),
    weeks_to_scan: parseInt($("weeks_to_scan").value || "3", 10),
    manage_existing: $("manage_existing").value === "true",
  };
}

async function savePrefs(extra = {}) {
  const body = { ...collectPrefs(), ...extra };
  const r = await api("/api/preferences", { method: "POST", body: JSON.stringify(body) });
  if (r.ok) {
    $("saveMsg").textContent = "Saved ✓";
    setTimeout(() => ($("saveMsg").textContent = ""), 1500);
    refresh();
  }
}

$("btnSave").addEventListener("click", () => savePrefs());
$("btnPause").addEventListener("click", async () => {
  const cur = await (await api("/api/status")).json();
  savePrefs({ enabled: !cur.preferences.enabled });
});
$("btnDry").addEventListener("click", async () => {
  const cur = await (await api("/api/status")).json();
  savePrefs({ dry_run: !cur.preferences.dry_run });
});
$("btnClear").addEventListener("click", async () => {
  await api("/api/clear-booking", { method: "POST" });
  refresh();
});
$("debugLink").addEventListener("click", (e) => {
  e.preventDefault();
  fetch("/api/debug/last-html", { headers: headers() })
    .then((r) => r.text())
    .then((html) => {
      const w = window.open("", "_blank");
      w.document.write("<pre>" + html.replace(/[&<>]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])) + "</pre>");
    });
});

/* ---------------- logs ---------------- */
function renderLogs(logs) {
  const box = $("logList");
  box.innerHTML = "";
  logs.forEach((l) => {
    const row = document.createElement("div");
    row.className = "logline " + l.level;
    const t = String(l.ts).slice(11, 19);
    row.innerHTML = `<span class="t">${t}</span><span class="m">${escapeHtml(l.message)}</span>`;
    box.appendChild(row);
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

/* ---------------- loops ---------------- */
async function refresh() {
  try {
    const r = await api("/api/status");
    applyStatus(await r.json());
  } catch {}
}
async function refreshLogs() {
  try {
    const r = await api("/api/logs?limit=120");
    renderLogs((await r.json()).logs);
  } catch {}
}

function boot() {
  refresh();
  refreshLogs();
  setInterval(refresh, 2500);
  setInterval(refreshLogs, 3500);
}

/* ---------------- init ---------------- */
(async function init() {
  renderWeekdays();
  if (!TOKEN) { showGate(); return; }
  try {
    const r = await fetch("/api/status", { headers: headers() });
    if (r.ok) { showApp(); boot(); }
    else showGate();
  } catch { showGate("Connection error."); }
})();
