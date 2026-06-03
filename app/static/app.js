const DAYS = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const token = new URLSearchParams(location.search).get("token") || localStorage.getItem("token") || "";

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token) opts.headers["X-Auth-Token"] = token;
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;" }[c]));

// ─── status ──────────────────────────────────────────────────────────────────
// Live "prereg unlocks in Ns" countdown. refreshStatus() re-baselines the target
// time from the server; this ticker repaints every second so it visibly decreases.
let preregUnlocksAt = null;
function renderPreregTimer() {
  const el = $("preregTimer");
  if (preregUnlocksAt == null) { el.textContent = ""; return; }
  const remaining = Math.max(0, Math.round((preregUnlocksAt - Date.now()) / 1000));
  el.textContent = remaining > 0 ? ` · prereg unlocks in ${remaining}s` : " · prereg ready";
}
setInterval(renderPreregTimer, 1000);

// Live "next section load in Ns" countdown for the registerable panel. Like the
// prereg timer: refreshStatus() re-baselines from the server, this repaints each
// second. Only ticks while the poller is engaged (server sends a number then).
let sectionsNextAt = null;
function renderSectionsTimer() {
  const el = $("secsTimer");
  if (!el) return;
  if (sectionsNextAt == null) { el.textContent = ""; return; }
  const remaining = Math.max(0, Math.round((sectionsNextAt - Date.now()) / 1000));
  el.textContent = remaining > 0 ? `next section load in ${remaining}s` : "loading sections…";
}
setInterval(renderSectionsTimer, 1000);

// Map a server "seconds remaining" into a browser-local target time, but keep the
// existing target if it's still within 2s of the server value — so the per-second
// ticker counts down smoothly instead of stuttering on every status poll.
function syncCountdown(currentAt, serverSeconds) {
  if (serverSeconds == null || serverSeconds <= 0) return null;
  if (currentAt != null) {
    const projected = Math.round((currentAt - Date.now()) / 1000);
    if (Math.abs(projected - serverSeconds) <= 2) return currentAt;  // close enough; don't jolt
  }
  return Date.now() + serverSeconds * 1000;
}

async function refreshStatus() {
  let s; try { s = await api("/api/status"); } catch { return; }
  const reg = $("regPill");
  reg.textContent = s.registration_open ? "registration OPEN" : "registration closed";
  reg.className = "pill " + (s.registration_open ? "on" : "off");
  const lp = $("loginPill");
  lp.textContent = s.logged_in ? "logged in" : "logged out";
  lp.className = "pill " + (s.logged_in ? "on" : "off");
  $("student").textContent = s.student?.Name
    ? `${s.student.Name} · ${s.student["Student ID"]||""} · CGPA ${s.student.Cgpa||"—"}` : "";
  $("semester").textContent = s.semester?.Title ? `Semester: ${s.semester.Title}  ` : "";
  $("lastPoll").textContent = s.last_poll ? `· last poll ${new Date(s.last_poll).toLocaleTimeString()}` : "";
  $("confirmBtn").disabled = !s.confirm_q;
  const fb = $("forceBtn");
  fb.textContent = "Force flow: " + (s.force_workspace ? "ON" : "off");
  fb.className = s.force_workspace ? "" : "ghost";
  const pp = $("proxyPill");
  pp.classList.toggle("hidden", !s.proxy);
  if (s.proxy) pp.textContent = "proxy" + (s.verify_tls ? "" : " (no TLS verify)");
  // Baseline the live countdown once, then let the 1s ticker run it down smoothly.
  // Only re-sync when it first appears or the server value diverges by >2s (a real
  // change, e.g. Select2 restarted) — re-baselining every poll made the display
  // stutter/hang for a beat (server sends an int()-floored value + request latency).
  preregUnlocksAt = syncCountdown(preregUnlocksAt, s.prereg_unlocks_in);
  renderPreregTimer();

  // Same idea for the next live section refresh (only while engaged).
  sectionsNextAt = syncCountdown(sectionsNextAt, s.sections_next_in);
  renderSectionsTimer();

  renderCatalog(s.catalog, s.logged_in);

  // Login panel vs logged-in chrome.
  $("loginPanel").classList.toggle("hidden", s.logged_in);
  $("logoutBtn").classList.toggle("hidden", !s.logged_in);
  if (!s.logged_in && s.username && !$("liUser").value) $("liUser").value = s.username;
  $("liCaptcha").classList.toggle("hidden", !s.needs_captcha);
  if (s.needs_captcha) loadCaptcha();
  if (s.login_error) { $("liError").textContent = s.login_error; lp.title = s.login_error; }
}
$("forceBtn").onclick = async () => {
  const turningOn = $("forceBtn").textContent.endsWith("off");
  if (turningOn && !confirm("Force the registration flow and BYPASS the AIUB open-window check?\n\nThis enters Select2 + GetPreReg2 even if registration is closed.")) return;
  await api("/api/force-workspace", { method: "POST", body: JSON.stringify({ enabled: turningOn }) });
  refreshStatus();
};

async function loadCaptcha() {
  try {
    const r = await api("/api/login/captcha");
    if (r.captcha_image) $("liCaptchaImg").src = "data:image/gif;base64," + r.captcha_image;
  } catch {}
}
async function doLogin(extra) {
  $("liError").textContent = "";
  const body = Object.assign({ username: $("liUser").value, password: $("liPass").value }, extra || {});
  const r = await api("/api/login", { method: "POST", body: JSON.stringify(body) });
  if (r.ok) { $("liPass").value = ""; $("liCaptcha").classList.add("hidden"); }
  else if (r.needs_captcha) { $("liCaptcha").classList.remove("hidden"); if (r.captcha_image) $("liCaptchaImg").src = "data:image/gif;base64," + r.captcha_image; }
  else if (r.error) $("liError").textContent = r.error;
  refreshStatus();
}
$("liBtn").onclick = () => doLogin();
$("liPass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("liCaptchaBtn").onclick = () => { doLogin({ answer: $("liCaptchaAns").value }); $("liCaptchaAns").value = ""; };
$("logoutBtn").onclick = async () => {
  if (!confirm("Log out and clear stored credentials?")) return;
  await api("/api/logout", { method: "POST" });
  $("liUser").value = ""; refreshStatus();
};
$("confirmBtn").onclick = async () => {
  if (!confirm("Finalize registration now?")) return;
  const r = await api("/api/confirm", { method: "POST" });
  alert("Confirm HTTP " + r.status);
};

// ─── catalog (offered-course report) ─────────────────────────────────────────
function fmtAge(sec) {
  if (sec == null) return "never";
  if (sec < 90) return "just now";
  const m = Math.round(sec / 60);
  if (m < 90) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
let catalogRefreshing = false;  // guard against double-clicks / status overwrite
function renderCatalog(c, loggedIn) {
  const btn = $("refreshCatalogBtn");
  const age = $("catalogAge");
  if (!btn || !age) return;
  if (!c) { age.textContent = ""; return; }
  const busy = catalogRefreshing || c.refreshing;
  age.textContent = busy
    ? "Catalog: refreshing…"
    : `Catalog: ${c.count} courses · ${c.source === "live" ? "updated " + fmtAge(c.age_seconds) : "bundled (never refreshed)"}`;
  if (!catalogRefreshing) {
    btn.disabled = !loggedIn || c.refreshing;
    btn.textContent = c.refreshing ? "Refreshing…" : "Refresh catalog";
  }
}
$("refreshCatalogBtn").onclick = async () => {
  const btn = $("refreshCatalogBtn");
  catalogRefreshing = true;
  btn.disabled = true; btn.textContent = "Refreshing…";
  $("catalogAge").textContent = "Catalog: refreshing…";
  try {
    const r = await api("/api/catalog/refresh", { method: "POST" });
    if (!r.ok) { $("catalogAge").textContent = "Catalog: refresh failed — " + (r.error || "error"); }
  } catch (e) {
    $("catalogAge").textContent = "Catalog: refresh failed";
  } finally {
    catalogRefreshing = false;
    btn.textContent = "Refresh catalog";
    refreshStatus();
    // Re-run the picker so the freshly-loaded sections show.
    if ($("courseSearch").value) loadCatalogSections();
  }
};

// ─── catalog search ────────────────────────────────────────────────────────
let searchTimer;
$("courseSearch").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const { titles } = await api("/api/catalog?q=" + encodeURIComponent($("courseSearch").value));
    $("courseList").innerHTML = titles.map(t => `<option value="${esc(t)}">`).join("");
    if (titles.includes($("courseSearch").value)) loadCatalogSections();
  }, 200);
});
async function loadCatalogSections() {
  const t = $("courseSearch").value;
  try {
    const r = await api("/api/catalog/sections?title=" + encodeURIComponent(t));
    $("catalogSecs").textContent = r.sections.length
      ? "Known sections: " + r.sections.map(x => `${x.section} (${x.routine})`).join("  ·  ")
      : "No sections in the offered-course report (live sections may still appear).";
  } catch {}
}

// ─── alert form ──────────────────────────────────────────────────────────────
// Days as clickable toggle chips (the checkbox is hidden, the chip lights up via CSS).
$("dayChecks").innerHTML = DAYS.map(d =>
  `<label class="chip"><input type="checkbox" class="dayChk" value="${d}"> ${d.slice(0,3)}</label>`).join("");

// From/To use native <input type="time"> pickers; their 24h "HH:MM" value is parsed
// as-is by the backend (schedule.parse_time_to_minutes). "" = no bound (any).
const timeVal = (id) => $(id).value.trim();
// Pretty-print a 24h "HH:MM" value as 12h "H:MM AM/PM" for display only.
function fmt12(t) {
  const m = /^(\d{1,2}):(\d{2})/.exec(t || "");
  if (!m) return t || "";
  let h = +m[1]; const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12;
  return `${h}:${m[2]} ${ap}`;
}
$("filterType").onchange = () => {
  const v = $("filterType").value;
  $("bySection").classList.toggle("hidden", v !== "section");
  $("byDaytime").classList.toggle("hidden", v !== "daytime");
  if (v === "section") loadCatalogSections();
  updateClashBtn();
  onModeChange();   // "any" vs scoped changes whether step 4 offers dropping
};

// "Check clash & drop" is only meaningful once the user has narrowed the alert to
// specific section(s) or a day/time window — enable it only then.
function clashInputsReady() {
  const ft = $("filterType").value;
  if (ft === "section") return $("sectionLabels").value.trim().length > 0;
  if (ft === "daytime") {
    const anyDay = document.querySelectorAll(".dayChk:checked").length > 0;
    const anyTime = $("timeStart").value.trim() || $("timeEnd").value.trim();
    return anyDay || !!anyTime;
  }
  return false;  // "any open section" has nothing to check a clash against
}
function updateClashBtn() { $("checkClash").disabled = !clashInputsReady(); }
$("sectionLabels").addEventListener("input", updateClashBtn);
$("timeStart").addEventListener("change", updateClashBtn);
$("timeEnd").addEventListener("change", updateClashBtn);
$("dayChecks").addEventListener("change", updateClashBtn);
updateClashBtn();
// Step 3 mode: "alert" = notify only; "join" = auto-join and drop any clashing
// registered course to make room. Step 4 (clash handling) only applies to "join".
function joinMode() {
  return document.querySelector('input[name="joinMode"]:checked')?.value || "alert";
}
// Dropping a registered course to make room is only offered for a deliberately-scoped
// alert. "Any section" auto-join never drops — it just alerts & skips on a clash.
function canDrop() { return joinMode() === "join" && $("filterType").value !== "any"; }
function onModeChange() {
  const join = joinMode() === "join";
  $("clashBlock").classList.toggle("hidden", !join);
  const drop = canDrop();
  $("clashDrop").classList.toggle("hidden", !drop);
  $("clashNoDrop").classList.toggle("hidden", drop);
}
$("modeAlert").onchange = onModeChange;
$("modeJoin").onchange = onModeChange;

$("checkClash").onclick = async () => {
  // Clash = (a) pure time overlap between the day/time window you pick and your
  // registered courses, PLUS (b) same-course supersede: a section of THIS course you're
  // already in (the portal won't let you hold two sections of one course). Send the
  // course title so the server can flag (b) the way auto-join does.
  const days = [...document.querySelectorAll(".dayChk:checked")].map(c => c.value);
  const r = await api("/api/alerts/clash-check", {
    method: "POST",
    body: JSON.stringify({
      course_title: $("courseSearch").value.trim(),
      days,
      time_start: timeVal("timeStart"),
      time_end: timeVal("timeEnd"),
    }),
  });
  const considered = r.registered_considered || [];
  const sameCourse = r.same_course || [];
  const w = r.window || {};
  const winLabel = `${(w.days && w.days.length ? w.days.map(d => d.slice(0, 3)).join("/") : "any day")} ${fmt12(w.time_start) || "start"}–${fmt12(w.time_end) || "end"}`;
  const src = `vs ${considered.length} registered course(s) from ${r.source || "?"}`;
  const clearTargets = () => { $("unregTargets").classList.add("hidden"); $("targetChecks").innerHTML = ""; };
  if (!considered.length) {
    $("clashMsg").textContent = "No registered courses found to compare against yet (register something first).";
    clearTargets();
    return;
  }
  if (!r.clash_course_titles.length) {
    // No window means a section-scoped check with no time filter — only the same-course
    // supersede was evaluated, so word the all-clear accordingly.
    $("clashMsg").textContent = r.has_window
      ? `No clashes ✅ — your window (${winLabel}) is free of registered classes. ${src}.`
      : `No clashes ✅ — you're not already registered in this course. ${src}.`;
    clearTargets();
    return;
  }
  // Two independent reasons a course can be flagged: a real time overlap with the
  // window, and/or already being registered in the same course (no overlap needed).
  const parts = [];
  if (r.clashes.length) parts.push(`your window (${winLabel}) overlaps ${r.clashes.map(x => `${x.title} [${x.section}]`).join(", ")}`);
  if (sameCourse.length) parts.push(`you're already registered in the same course: ${sameCourse.map(x => `${x.title} [${x.section}]`).join(", ")}`);
  let body = parts.join("; ");
  body = body[0].toUpperCase() + body.slice(1);
  $("clashMsg").textContent = `⚠️ ${body}.  (${src})`;
  $("targetChecks").innerHTML = r.clash_course_titles.map(t =>
    `<label><input type="checkbox" class="tgtChk" value="${esc(t)}" checked> ${esc(t)}</label>`).join("<br>");
};

$("createAlert").onclick = async () => {
  const ft = $("filterType").value;
  const title = $("courseSearch").value.trim();
  if (!title) return alert("Pick a course");
  const sectionLabels = ft === "section"
    ? $("sectionLabels").value.split(",").map(s => s.trim()).filter(Boolean) : [];

  // "Just alert me" = notify only. "Auto-join it" = register; drop clashing course(s)
  // only for a scoped filter (never for "any section" — too broad to drop for).
  const auto = joinMode() === "join";
  const drop = canDrop();
  const clash_policy = drop ? "unregister" : "alert";
  const unregister_targets = drop
    ? [...document.querySelectorAll(".tgtChk:checked")].map(c => c.value) : [];

  const data = {
    course_title: title,
    filter_type: ft,
    auto_join: auto,
    clash_policy,
    section_labels: sectionLabels,
    days: ft === "daytime"
      ? [...document.querySelectorAll(".dayChk:checked")].map(c => c.value) : [],
    time_start: ft === "daytime" ? timeVal("timeStart") : null,
    time_end: ft === "daytime" ? timeVal("timeEnd") : null,
    unregister_targets,
  };
  await api("/api/alerts", { method: "POST", body: JSON.stringify(data) });
  // Reset the form back to its collapsed default (alert-only).
  $("modeAlert").checked = true;
  $("clashBlock").classList.add("hidden");
  $("clashMsg").textContent = "";
  $("targetChecks").innerHTML = "";
  $("unregTargets").classList.add("hidden");
  refreshAlerts();
};

// ─── alerts list ──────────────────────────────────────────────────────────────
async function refreshAlerts() {
  const { alerts } = await api("/api/alerts");
  $("noAlerts").style.display = alerts.length ? "none" : "block";
  $("alertRows").innerHTML = alerts.map(a => {
    let filt = a.filter_type === "section" ? "sec: " + (a.section_labels.join(",") || "any")
      : a.filter_type === "daytime" ? `${(a.days||[]).map(d=>d.slice(0,3)).join("/")||"any day"} ${fmt12(a.time_start)}-${fmt12(a.time_end)}`
      : "any open";
    return `<tr>
      <td><b>${esc(a.course_title)}</b><br><small class="muted">${esc(filt)}</small></td>
      <td>${a.auto_join ? '<span class="tag">auto-join</span>' : '<span class="tag">alert</span>'}
          <span class="tag">${esc(a.status)}</span></td>
      <td>
        <button class="ghost" onclick="toggleAlert(${a.id},${a.active?0:1})">${a.active?'pause':'resume'}</button>
        <button class="danger" onclick="delAlert(${a.id})">×</button>
      </td></tr>`;
  }).join("");
}
window.toggleAlert = async (id, active) => { await api("/api/alerts/"+id, { method:"PATCH", body: JSON.stringify({active}) }); refreshAlerts(); };
window.delAlert = async (id) => { if(confirm("Delete alert?")){ await api("/api/alerts/"+id,{method:"DELETE"}); refreshAlerts(); } };

// ─── registerable courses ─────────────────────────────────────────────────────
const SECMAP = {};      // section ID -> section object (for register payloads)
let regSig = null;      // last rendered signature, to avoid flicker

function fmtRoutine(r) {
  if (!r) return "";
  return r.split("&").map(p =>
    p.replace(/\[[^\]]*\]/g, "").trim()
     .replace(/^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)/, (m) => m.slice(0, 3))
  ).filter(Boolean).join("  ·  ");
}

function courseCredit(c) {
  // AIUB's course credit is the lecture-credit field; the Sci/Comp/Lan/Studio
  // fields are practical/contact components that do NOT add to the credit value
  // (e.g. INTRO TO DATABASE Lec3 + Comp1 is a 3-credit course, not 4). Fall back to
  // the largest component only if there's no lecture credit at all.
  return c.LecCredit
    || Math.max(c.SciCredit || 0, c.CompCredit || 0, c.LanCredit || 0, c.StudioCredit || 0);
}

function renderCourses(courses, open) {
  // Signature so we only touch the DOM when something actually changed (no flicker,
  // no interrupting an in-progress click).
  const sig = JSON.stringify((courses || []).map(c => [c.OfferedCourseId, c.Status,
    (c.RegisterableSections || []).map(s => [s.ID, s.StudentCount, s.Capacity, s.Registered])]));
  if (sig === regSig) return;
  regSig = sig;
  const totalCr = (courses || []).reduce((n, c) => n + courseCredit(c), 0);
  $("regHint").textContent =
    (open ? "" : "(registration closed — sections show only while engaged) ") +
    ((courses || []).length ? `· ${courses.length} courses · ${totalCr} cr` : "");

  $("courseRows").innerHTML = (courses || []).map(c => {
    const secs = (c.RegisterableSections || []);
    secs.forEach(s => { SECMAP[s.ID] = s; });
    const cr = courseCredit(c);
    const lines = secs.map(s => {
      const isOpen = s.Capacity > s.StudentCount && !s.Registered;
      const badge = s.Registered ? "secbadge reg" : (isOpen ? "secbadge open" : "secbadge full");
      const btn = s.Registered
        ? `<button class="danger" onclick="unreg(${s.ID})">drop</button>`
        : (isOpen ? `<button onclick="reg(${s.ID})">take</button>`
                  : `<span class="muted">full</span>`);
      return `<div class="secline">
        <span class="${badge}">${esc(s.Title)}</span>
        <span class="seats">${s.StudentCount}/${s.Capacity}</span>
        <span class="muted time">${esc(fmtRoutine(s.Routine))}</span>
        ${btn}</div>`;
    }).join("");
    const body = lines || `<span class="muted">no sections published yet</span>`;
    const loadBtn = `<div class="secline"><button class="ghost" onclick="loadCourse(${c.OfferedCourseId}, this)" title="Reload just this course's sections">load sections</button></div>`;
    return `<tr>
      <td><b>${esc(c.Title)}</b><br><small class="muted">${cr} credit${cr === 1 ? "" : "s"}</small></td>
      <td><span class="tag">${esc(c.Status)}</span></td>
      <td>${body}${loadBtn}</td>
    </tr>`;
  }).join("");
}

async function refreshRegisterable() {
  let r; try { r = await api("/api/registerable"); } catch { return; }
  renderCourses(r.courses, r.open);
}

window.reg = async (id) => {
  const s = SECMAP[id]; if (!s) return;
  const r = await api("/api/register", { method: "POST", body: JSON.stringify(s) });
  if (!r.IsSuccess) alert("Failed: " + (r.Error || JSON.stringify(r)));
  regSig = null; refreshRegisterable();
};
window.unreg = async (id) => {
  const r = await api("/api/unregister", { method: "POST", body: JSON.stringify({ sectionID: id }) });
  if (!r.IsSuccess) alert("Failed: " + (r.Error || ""));
  regSig = null; refreshRegisterable();
};
window.loadCourse = async (cid, btn) => {
  if (btn) { btn.disabled = true; btn.textContent = "loading…"; }
  try {
    const r = await api("/api/course/reload", { method: "POST", body: JSON.stringify({ offered_course_id: cid }) });
    if (r && r.ok === false) alert("Load failed: " + (r.error || "unknown"));
    regSig = null; await refreshRegisterable();
  } catch (e) { alert("Load failed"); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = "load sections"; } }
};
$("reloadSecsBtn").onclick = async () => {
  const b = $("reloadSecsBtn"); b.disabled = true; b.textContent = "Reloading…";
  try { await api("/api/registerable/reload", { method: "POST" }); regSig = null; await refreshRegisterable(); }
  catch (e) { /* ignore */ }
  finally { b.disabled = false; b.textContent = "Reload sections"; }
};

// ─── seat monitors ──────────────────────────────────────────────────────────
// A monitor watches one section's seat count and pings Discord on a change /
// threshold crossing. It keeps running after the tab closes (own background loop).
let monSearchTimer;
$("monCourseSearch").addEventListener("input", () => {
  clearTimeout(monSearchTimer);
  monSearchTimer = setTimeout(async () => {
    const { titles } = await api("/api/catalog?q=" + encodeURIComponent($("monCourseSearch").value));
    $("monCourseList").innerHTML = titles.map(t => `<option value="${esc(t)}">`).join("");
    if (titles.includes($("monCourseSearch").value)) loadMonitorSections();
  }, 200);
});
async function loadMonitorSections() {
  try {
    const r = await api("/api/catalog/sections?title=" + encodeURIComponent($("monCourseSearch").value));
    $("monSecs").textContent = r.sections.length
      ? "Known sections: " + r.sections.map(x => `${x.section} (${x.routine})`).join("  ·  ")
      : "No sections in the offered-course report (live seats may still be found).";
  } catch {}
}
// Day chips for the new-section day/time filter (own IDs, separate from the alert form).
$("monDayChecks").innerHTML = DAYS.map(d =>
  `<label class="chip"><input type="checkbox" class="monDayChk" value="${d}"> ${d.slice(0,3)}</label>`).join("");

// Toggle which inputs apply to the picked mode: threshold N (threshold mode),
// day/time filter + hide the single-section step (new_section mode).
function syncMonMode() {
  const mode = document.querySelector('input[name="monMode"]:checked')?.value || "threshold";
  $("monThreshold").disabled = mode !== "threshold";
  $("monDaytime").classList.toggle("hidden", mode !== "new_section");
  $("monSectionStep").classList.toggle("hidden", mode === "new_section");
}
$("monModeThreshold").onchange = syncMonMode;
$("monModeChange").onchange = syncMonMode;
$("monModeNew").onchange = syncMonMode;
syncMonMode();

$("createMonitor").onclick = async () => {
  $("monError").textContent = "";
  const title = $("monCourseSearch").value.trim();
  const section = $("monSection").value.trim();
  const mode = document.querySelector('input[name="monMode"]:checked')?.value || "threshold";
  if (!title) { $("monError").textContent = "Pick a course"; return; }
  const data = { course_title: title, mode };
  if (mode === "new_section") {
    data.days = [...document.querySelectorAll(".monDayChk:checked")].map(c => c.value);
    data.time_start = timeVal("monTimeStart");
    data.time_end = timeVal("monTimeEnd");
  } else {
    if (!section) { $("monError").textContent = "Enter a section label"; return; }
    data.section_label = section;
    if (mode === "threshold") {
      const n = parseInt($("monThreshold").value, 10);
      if (!n || n < 1) { $("monError").textContent = "Enter a seat number N for the threshold"; return; }
      data.threshold = n;
    }
  }
  try {
    await api("/api/monitors", { method: "POST", body: JSON.stringify(data) });
  } catch (e) {
    $("monError").textContent = String(e.message || e).replace(/^\d+\s*/, "") || "Failed to create monitor";
    return;
  }
  $("monSection").value = ""; $("monThreshold").value = "";
  refreshMonitors();
};

async function refreshMonitors() {
  let r; try { r = await api("/api/monitors"); } catch { return; }
  const monitors = r.monitors || [];
  $("noMonitors").style.display = monitors.length ? "none" : "block";
  $("monitorRows").innerHTML = monitors.map(m => {
    const checked = m.last_checked_at ? `checked ${new Date(m.last_checked_at).toLocaleTimeString()}` : "not checked yet";
    if (m.mode === "new_section") {
      const win = `${(m.days && m.days.length ? m.days.map(d => d.slice(0,3)).join("/") : "any day")} ${fmt12(m.time_start) || "start"}–${fmt12(m.time_end) || "end"}`;
      const nseen = Object.keys(m.seen_sections || {}).length;
      return `<tr>
        <td><b>${esc(m.course_title)}</b> <span class="tag">new section</span><br>
            <small class="muted">ping when a new section opens · ${esc(win)}</small></td>
        <td><span class="seats">${nseen} seen</span><br>
            <small class="muted"><span class="tag">${esc(m.status)}</span> · ${esc(checked)}</small></td>
        <td>
          <button class="ghost" onclick="toggleMonitor(${m.id},${m.active?0:1})">${m.active?'pause':'resume'}</button>
          <button class="danger" onclick="delMonitor(${m.id})">×</button>
        </td></tr>`;
    }
    const mode = m.mode === "threshold" ? `ping when filled < ${m.threshold}` : "ping on any change";
    const seats = (m.last_count != null && m.last_capacity != null)
      ? `${m.last_count}/${m.last_capacity}` : "—";
    return `<tr>
      <td><b>${esc(m.course_title)}</b> <span class="tag">${esc(m.section_label)}</span><br>
          <small class="muted">${esc(mode)}</small>${m.routine ? `<br><small class="muted">${esc(m.routine)}</small>` : ""}</td>
      <td><span class="seats">${seats}</span><br>
          <small class="muted"><span class="tag">${esc(m.status)}</span> · ${esc(checked)}</small></td>
      <td>
        <button class="ghost" onclick="toggleMonitor(${m.id},${m.active?0:1})">${m.active?'pause':'resume'}</button>
        <button class="danger" onclick="delMonitor(${m.id})">×</button>
      </td></tr>`;
  }).join("");
}
window.toggleMonitor = async (id, active) => { await api("/api/monitors/"+id, { method:"PATCH", body: JSON.stringify({active}) }); refreshMonitors(); };
window.delMonitor = async (id) => { if(confirm("Delete monitor?")){ await api("/api/monitors/"+id,{method:"DELETE"}); refreshMonitors(); } };

// ─── log ──────────────────────────────────────────────────────────────────────
async function refreshLog() {
  let r; try { r = await api("/api/events?limit=80"); } catch { return; }
  $("log").innerHTML = r.events.map(e =>
    `<div class="${e.level}">${new Date(e.ts).toLocaleTimeString()} ${esc(e.message)}</div>`).join("");
}

// ─── reset-on-exit ─────────────────────────────────────────────────────────────
// When the user leaves the dashboard (closes/navigates away), flush settings back
// to defaults: Force-flow off, alerts cleared, registerable panel cleared.
function resetOnExit() {
  const url = "/api/reset" + (token ? "?token=" + encodeURIComponent(token) : "");
  // sendBeacon survives the unload; falls back to a keepalive fetch.
  if (navigator.sendBeacon) navigator.sendBeacon(url, new Blob([], { type: "text/plain" }));
  else api("/api/reset", { method: "POST", keepalive: true }).catch(() => {});
}
window.addEventListener("pagehide", resetOnExit);

// ─── loops ──────────────────────────────────────────────────────────────────
refreshStatus(); refreshAlerts(); refreshRegisterable(); refreshLog(); refreshMonitors();
setInterval(refreshStatus, 3000);
setInterval(refreshRegisterable, 5000);
setInterval(refreshLog, 4000);
setInterval(refreshAlerts, 8000);
setInterval(refreshMonitors, 8000);
