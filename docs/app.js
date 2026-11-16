"use strict";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const $ = (id) => document.getElementById(id);

// Derive owner/repo from the Pages URL (owner.github.io/repo/), or ?repo=owner/name.
function repoInfo() {
  const q = new URLSearchParams(location.search).get("repo");
  if (q && q.includes("/")) {
    const [owner, repo] = q.split("/");
    return { owner, repo };
  }
  const owner = location.hostname.split(".")[0];
  const repo = location.pathname.split("/").filter(Boolean)[0] || "";
  return { owner, repo };
}
const { owner, repo } = repoInfo();
const STATE_BRANCH = "bot-state";
let defaultBranch = "main";
let prefsLoaded = false;
let selectedWeekdays = new Set([0, 1, 2, 3, 4, 5, 6]);

$("editLink").href = `https://github.com/${owner}/${repo}/edit/${defaultBranch}/preferences.json`;

function rawStateUrl() {
  return `https://raw.githubusercontent.com/${owner}/${repo}/${STATE_BRANCH}/state.json?t=${Date.now()}`;
}

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

function fmt(dt) { return dt ? String(dt).replace("T", " ").slice(0, 16) : "—"; }

function applyState(s) {
  const p = s.preferences || {};
  $("pillLogin").textContent = s.logged_in ? "logged in" : "logged out";
  $("pillLogin").className = "pill " + (s.logged_in ? "ok" : "bad");
  $("pillEngine").textContent = p.enabled ? "running" : "paused";
  $("pillEngine").className = "pill " + (p.enabled ? "ok" : "warn");
  $("pillMode").textContent = p.dry_run ? "dry-run" : "LIVE";
  $("pillMode").className = "pill " + (p.dry_run ? "warn" : "ok");
  $("pillUpdated").textContent = "updated " + fmt(s.updated_at);

  $("stCurrent").textContent = s.current ? s.current.label : "none";
  $("stReservations").textContent = (s.reservations && s.reservations.length)
    ? s.reservations.map((r) => r.label).join(", ") : "none";
  $("stAction").textContent = s.last_action || "—";
  $("stCheck").textContent = fmt(s.last_check);
  $("stDetected").textContent = (s.detected ? s.detected.length : 0) + " slot(s)";
  $("stError").textContent = s.last_error || "—";

  renderSlots(s.detected || [], s.current);
  renderLogs(s.logs || []);

  if (!prefsLoaded && Object.keys(p).length) {
    $("enabled").value = String(p.enabled);
    $("dry_run").value = String(p.dry_run);
    $("latest_time").value = p.latest_time || "18:00";
    $("earliest_time").value = p.earliest_time || "00:00";
    $("date_from").value = p.date_from || "";
    $("date_to").value = p.date_to || "";
    $("min_improvement_minutes").value = p.min_improvement_minutes ?? 10;
    $("rebook_strategy").value = p.rebook_strategy || "book_then_cancel";
    $("weeks_to_scan").value = p.weeks_to_scan ?? 3;
    $("manage_existing").value = String(p.manage_existing);
    $("poll_min_seconds").value = p.poll_min_seconds ?? 8;
    $("poll_max_seconds").value = p.poll_max_seconds ?? 12;
    selectedWeekdays = new Set(p.allowed_weekdays || [0, 1, 2, 3, 4, 5, 6]);
    renderWeekdays();
    prefsLoaded = true;
  }
}

function renderSlots(slots, current) {
  const box = $("slotList");
  if (!slots.length) { box.innerHTML = '<p class="muted">No open slots on last scan.</p>'; $("slotsHint").textContent = ""; return; }
  $("slotsHint").textContent = `(${slots.length})`;
  box.innerHTML = "";
  slots.slice(0, 40).forEach((s, idx) => {
    const row = document.createElement("div");
    const isCur = current && current.slot_key === s.slot_key;
    row.className = "slot" + (idx === 0 ? " best" : "");
    row.innerHTML = `<span>${s.label}</span><span class="tag">${isCur ? "booked" : idx === 0 ? "earliest" : ""}</span>`;
    box.appendChild(row);
  });
}

function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function renderLogs(logs) {
  const box = $("logList");
  box.innerHTML = "";
  [...logs].reverse().forEach((l) => {
    const row = document.createElement("div");
    row.className = "logline " + l.level;
    row.innerHTML = `<span class="t">${String(l.ts).slice(11, 19)}</span><span class="m">${escapeHtml(l.message)}</span>`;
    box.appendChild(row);
  });
}

async function refresh() {
  try {
    const r = await fetch(rawStateUrl(), { cache: "no-store" });
    if (!r.ok) throw new Error("state " + r.status);
    applyState(await r.json());
    $("freshNote").textContent = "";
  } catch (e) {
    $("freshNote").textContent =
      "No status yet (the bot writes it on its first run — give it a few minutes).";
  }
}

/* ---- saving preferences via GitHub API ---- */
function collectPrefs(existing) {
  return {
    ...existing,
    enabled: $("enabled").value === "true",
    dry_run: $("dry_run").value === "true",
    shop_id: existing.shop_id || "230",
    latest_time: $("latest_time").value || "18:00",
    earliest_time: $("earliest_time").value || "00:00",
    date_from: $("date_from").value || null,
    date_to: $("date_to").value || null,
    allowed_weekdays: [...selectedWeekdays].sort((a, b) => a - b),
    min_improvement_minutes: parseInt($("min_improvement_minutes").value || "10", 10),
    rebook_strategy: $("rebook_strategy").value,
    weeks_to_scan: parseInt($("weeks_to_scan").value || "3", 10),
    manage_existing: $("manage_existing").value === "true",
    poll_min_seconds: parseFloat($("poll_min_seconds").value || "8"),
    poll_max_seconds: parseFloat($("poll_max_seconds").value || "12"),
  };
}

async function ghApi(path, opts = {}) {
  const token = $("ghtoken").value.trim();
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", ...(opts.headers || {}) };
  if (token) headers.Authorization = "Bearer " + token;
  return fetch(`https://api.github.com${path}`, { ...opts, headers });
}

async function savePrefs() {
  const token = $("ghtoken").value.trim();
  if (!token) { $("saveMsg").textContent = "Paste a token, or use Edit on GitHub."; return; }
  localStorage.setItem("gh_pat", token);
  $("saveMsg").textContent = "Saving…";
  try {
    // current file sha + content
    const cur = await ghApi(`/repos/${owner}/${repo}/contents/preferences.json?ref=${defaultBranch}`);
    let sha, existing = {};
    if (cur.ok) { const j = await cur.json(); sha = j.sha; existing = JSON.parse(decodeURIComponent(escape(atob(j.content)))); }
    const next = collectPrefs(existing);
    const body = {
      message: "update preferences via dashboard",
      content: btoa(unescape(encodeURIComponent(JSON.stringify(next, null, 2)))),
      branch: defaultBranch,
    };
    if (sha) body.sha = sha;
    const put = await ghApi(`/repos/${owner}/${repo}/contents/preferences.json`, {
      method: "PUT", body: JSON.stringify(body),
    });
    if (put.ok) { $("saveMsg").textContent = "Saved ✓ (applies within a few minutes)"; }
    else { const e = await put.json(); $("saveMsg").textContent = "Error: " + (e.message || put.status); }
  } catch (e) { $("saveMsg").textContent = "Error: " + e.message; }
  setTimeout(() => ($("saveMsg").textContent = ""), 4000);
}

$("btnSave").addEventListener("click", savePrefs);

/* ---- init ---- */
(async function init() {
  renderWeekdays();
  const saved = localStorage.getItem("gh_pat");
  if (saved) $("ghtoken").value = saved;
  // resolve default branch for edit link + saves
  try {
    const r = await fetch(`https://api.github.com/repos/${owner}/${repo}`);
    if (r.ok) { defaultBranch = (await r.json()).default_branch || "main"; }
  } catch {}
  $("editLink").href = `https://github.com/${owner}/${repo}/edit/${defaultBranch}/preferences.json`;
  refresh();
  setInterval(refresh, 20000);
})();
