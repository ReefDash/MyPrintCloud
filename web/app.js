// MyPrintCloud phone/web app. Same file runs from the server (web) and inside the Capacitor APK.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("mpc_" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("mpc_" + k, JSON.stringify(v)); } catch {} },
};
const isApp = !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
const DEFAULT_SERVER = "https://print.example.com";  // APK only: set this to your server before building (the login screen can change it)
let SERVER = store.get("server", isApp ? DEFAULT_SERVER : location.origin);
let TOKEN = store.get("token", "");
let ST = { bridge: false, slicer: false, printer: {}, job: null, spools: {} };
const spoolLeft = (slot) => { const s = (ST.spools || {})[String(slot)]; return s && s.left != null ? +s.left : null; };
let ws = null, wsTimer = null;

// ---------- api ----------
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (TOKEN) headers.Authorization = "Bearer " + TOKEN;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(SERVER + path, { method: opts.method || (opts.body ? "POST" : "GET"), headers, body: opts.body });
  let data = null;
  try { data = await r.json(); } catch {}
  if (r.status === 401 && path !== "/api/login") { logout(); throw new Error("Please sign in again"); }
  if (!r.ok) { const er = new Error((data && data.detail) || `HTTP ${r.status}`); er.status = r.status; throw er; }
  return data;
}
const img = (u) => u ? `${SERVER}/api/img?u=${encodeURIComponent(u)}&token=${encodeURIComponent(TOKEN)}` : "";
function toast(msg, ms = 3000) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), ms);
}
async function act(btn, fn, ok) {
  if (btn) btn.disabled = true;
  try { await fn(); if (ok) toast(ok); } catch (e) { toast(e.message, 4500); } finally { if (btn) btn.disabled = false; }
}

// ---------- login ----------
function showLogin() {
  $("#main").classList.add("hidden"); $("#login").classList.remove("hidden");
  $("#l-server").value = SERVER; $("#l-server").parentElement.classList.toggle("hidden", !isApp && !!SERVER);
}
$("#l-go").onclick = () => act($("#l-go"), async () => {
  $("#l-err").textContent = "";
  SERVER = ($("#l-server").value || SERVER).replace(/\/+$/, "");
  try {
    const r = await api("/api/login", { json: { password: $("#l-pw").value } });
    TOKEN = r.token; store.set("token", TOKEN); store.set("server", SERVER); start();
  } catch (e) { $("#l-err").textContent = e.message; throw e; }
});
$("#l-pw").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#l-go").click(); });
function logout() { TOKEN = ""; store.set("token", ""); if (ws) ws.close(); showLogin(); }
$("#logout").onclick = logout;
$("#pw-open").onclick = () => { $("#pw-box").classList.toggle("hidden"); $("#pw-msg").textContent = ""; };
$("#pw-save").onclick = (e) => act(e.target, async () => {
  const old = $("#pw-old").value, nw = $("#pw-new").value;
  if (nw.length < 8) throw new Error("Use at least 8 characters for the new password");
  if (nw !== $("#pw-new2").value) throw new Error("The two new passwords don't match");
  const r = await api("/api/password", { json: { old, new: nw, sign_out_all: $("#pw-all").checked } });
  TOKEN = r.token; store.set("token", TOKEN);
  if (r.signed_out && Progress && progressTick.on) { Progress.stop().catch(() => {}); progressTick.on = false; }  // lock-screen service restarts with the new sign-in
  ["#pw-old", "#pw-new", "#pw-new2"].forEach((k) => { $(k).value = ""; }); $("#pw-all").checked = false;
  $("#pw-msg").textContent = r.signed_out ? "Saved. Every other phone and browser has to sign in again." : "Saved. Use the new password next time you sign in.";
}, "Password changed");

// ---------- tabs ----------
let tab = "printer";
$$("nav button").forEach((b) => b.onclick = () => {
  tab = b.dataset.tab;
  $$("nav button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + tab));
  if (tab === "models" && !$("#grid").children.length) loadModels(true);
  if (tab === "jobs") renderJobs();
  if (tab === "settings") loadSettings();
  camTick();
});

// ---------- live status ----------
function connectWS() {
  clearTimeout(wsTimer);
  const u = SERVER.replace(/^http/, "ws") + "/ws/app?token=" + encodeURIComponent(TOKEN);
  ws = new WebSocket(u);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "hello") { ST = { ...ST, bridge: m.bridge, slicer: !!m.slicer, printer: m.printer || {}, job: m.job, eta: m.eta_factor || 1, watch: m.watch }; render(); }
    else if (m.t === "status") { ST.printer = m.s; ST.bridge = true; ST.eta = m.eta_factor || ST.eta || 1; ST.watch = m.watch || ST.watch; render();
      const sig = JSON.stringify([(m.s.trays || []).map((t) => [t.id, t.preset, t.color]), m.s.presets]);  // redraw only on change
      if (tab === "settings" && CAL && sig !== renderCalib.sig) { renderCalib.sig = sig; renderCalib(); } }
    else if (m.t === "bridge") { ST.bridge = m.online; render(); }
    else if (m.t === "slicer") { ST.slicer = m.online; render(); }
    else if (m.t === "queue") renderQueue(m.queue || []);
    else if (m.t === "spools") { ST.spools = m.spools || {}; render(); if (STU && STU.mode === "review") renderReview(); }
    else if (m.t === "job") { upsertJob(m.job); }
    else if (m.t === "job_gone") { JOBS = JOBS.filter((x) => x.id !== m.id); if (tab === "jobs") renderJobs(); }
  };
  ws.onclose = (e) => { if (e.code === 4401) return logout(); if (TOKEN) wsTimer = setTimeout(connectWS, 3000); setConn(false); };
  ws.onopen = () => setConn(true);
  clearInterval(connectWS.ping);
  connectWS.ping = setInterval(() => { try { ws.send("p"); } catch {} }, 25000);
}
function setConn(up) {
  const p = $("#h-conn");
  if (!up) { p.textContent = "Reconnecting…"; p.className = "pill bad"; return; }
  p.textContent = (ST.bridge ? (ST.printer.online ? "Online" : "Printer offline") : "Bridge offline") + (ST.slicer ? " · ☁ slicing" : "");
  p.title = ST.slicer ? "Models are sliced on the cloud server; the bridge only talks to the printer" : "Models are sliced on the PC";
  p.className = "pill " + (ST.bridge && ST.printer.online ? "ok" : "bad");
}
const fmtT = (s) => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
const NICE = { IDLE: "Idle", PRINTING: "Printing", PAUSED: "Paused", PAUSING: "Pausing…", RESUMING: "Resuming…", STOPPING: "Stopping…",
  STOPPED: "Stopped", COMPLETE: "Complete", HEATING: "Heating", PREPARING: "Preparing (heat, level, wipe)", HOMING: "Homing", LEVELING: "Levelling", "FILAMENT SWAP": "Changing filament", ERROR: "Error", OFFLINE: "Offline" };
// ---------- which Canvas slots a job uses (and how much) ----------
function jobUse(j, st) {  // [{slot, grams}] in slot order of first use; grams 0 = not known yet
  st = st || j.stats || {};
  const each = st.grams_each || (st.grams ? [st.grams] : []);
  const slots = j.trays && j.trays.length ? j.trays : (j.request && j.request.trays) || [];
  const need = new Map();
  slots.slice(0, Math.max(each.length, 1)).forEach((t, i) => need.set(t, (need.get(t) || 0) + (each[i] || 0)));
  return [...need].map(([slot, grams]) => ({ slot, grams }));
}
function useChips(use, label, left = 1) {  // left = share of the print still to go
  if (!use.length) return "";
  const trays = (ST.printer && ST.printer.trays) || [];
  return `<div class="use">${label ? `<span class="lbl">${label}</span>` : ""}${use.map(({ slot, grams }) => {
    const tr = trays.find((t) => t.id === slot) || {}, g = grams * left, sl = spoolLeft(slot);
    const low = grams > 0 && sl != null && sl < g * 1.05;
    const what = tr.loaded === false ? "empty" : slotMaterial(slot) || tr.name || tr.type || "?";
    return `<span class="chip ${low ? "low" : ""} ${tr.loaded === false ? "empty" : ""}" title="${low ? `only ~${Math.round(sl)} g left on this spool` : ""}"><i style="background:${esc(tr.color || "#333")}"></i>Slot ${slot + 1} · ${esc(what)}${grams > 0 ? ` · ${g < 10 ? g.toFixed(1) : Math.round(g)} g${left < 1 ? " to go" : ""}` : ""}${low ? " ⚠" : ""}</span>`;
  }).join("")}</div>`;
}
function currentPrint() {  // the app job (and plate stats) behind what the printer is printing now
  const f = (ST.printer || {}).filename;
  if (!f) return null;
  const cands = JOBS.filter((j) => j.remote === f || (j.plates || []).some((p) => p.sent === f));
  const j = cands.find((x) => x.stage === "printing") || cands[0];
  if (!j) return null;
  const p = (j.plates || []).find((x) => x.sent === f);
  return { j, st: (p && p.stats) || j.stats || {} };
}
function loadJobsOnce() {
  if (renderJobs.loaded || loadJobsOnce.busy) return;
  loadJobsOnce.busy = true;
  api("/api/jobs").then((r) => { if (!renderJobs.loaded) { JOBS = r; renderJobs.loaded = true; } render(); }).catch(() => {}).finally(() => { loadJobsOnce.busy = false; });
}

function render() {
  setConn(ws && ws.readyState === 1);
  const p = ST.printer || {};
  const st = ST.bridge ? (p.state || "OFFLINE") : "OFFLINE";
  $("#state").textContent = NICE[st] || st;
  const busy = !["IDLE", "OFFLINE", "COMPLETE", "STOPPED"].includes(st);
  const pct = busy || st === "COMPLETE" ? (p.progress || 0) : 0;
  $("#pct").textContent = pct + "%";
  $("#ring").style.strokeDasharray = `${pct} 100`;
  $("#fname").textContent = busy ? (p.filename || "") : "";
  const rem = (p.remaining_s || 0) * (ST.eta || 1);
  const at = rem > 0 ? new Date(Date.now() + rem * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  $("#times").textContent = busy && p.filename ? `Layer ${p.layer || 0}${p.total_layers ? "/" + p.total_layers : ""} · ${fmtT(rem)} left${at ? " · done ≈ " + at : ""} · ${p.speed_mode || ""}` : (st === "IDLE" ? "Ready to print" : "");
  $$(".maint button").forEach((b) => { b.disabled = busy || st === "OFFLINE"; });
  $("#bridge-restart").disabled = !ST.bridge;
  loadJobsOnce();
  const cur = busy && p.filename ? currentPrint() : null, curUse = cur ? jobUse(cur.j, cur.st) : [];
  const toGo = Math.max(0, 1 - pct / 100);
  $("#using").innerHTML = !(busy && p.filename) ? ""
    : curUse.length ? useChips(curUse, "Using", toGo)
    : `<div class="muted small" style="margin-top:6px">Started from the printer, not this app, so its filament isn't known here.</div>`;
  progressTick(busy && st !== "OFFLINE");
  liveUpdate(p, busy && st !== "OFFLINE");
  const hud = $("#hud");
  hud.classList.toggle("hidden", !(busy && p.filename));
  if (busy && p.filename) hud.innerHTML = `<b>${esc(p.filename.replace(/\.gcode$/, ""))}</b><span>${pct}% · layer ${p.layer || 0}${p.total_layers ? "/" + p.total_layers : ""}${at ? " · done ≈ " + at : ""}</span><i style="width:${pct}%"></i>`;
  const w = ST.watch || {};
  $("#watch").className = "small watch " + (w.score >= 0.66 ? "bad" : w.score >= 0.33 ? "warn" : "");
  $("#watch").textContent = busy && w.error && SET && SET.watch !== "off" ? "👁 Failure watch can't run: " + w.error
    : !(busy && w.ready && SET && SET.watch !== "off") ? ""
    : w.score == null || w.frames < 15 ? "👁 Failure watch: warming up" : w.score >= 0.66 ? "👁 Failure watch: this looks wrong, check the camera"
    : w.score >= 0.33 ? "👁 Failure watch: something looks off" : "👁 Failure watch: looking good";
  $("#b-pause").classList.toggle("hidden", !(busy && st !== "PAUSED"));
  $("#b-resume").classList.toggle("hidden", st !== "PAUSED");
  $("#b-stop").classList.toggle("hidden", !busy);
  const t = (a, b) => (a ? Math.round(a) + "°" : "—") + (b ? ` / ${Math.round(b)}°` : "");
  $("#t-noz").textContent = t(p.nozzle, p.nozzle_target); $("#t-bed").textContent = t(p.bed, p.bed_target); $("#t-ch").textContent = t(p.chamber);
  const usesOf = (id) => curUse.find((u) => u.slot === id);
  $("#trays").innerHTML = (p.trays || []).map((tr) => `<div class="tray ${tr.active ? "active" : ""} ${tr.loaded ? "" : "empty"} ${usesOf(tr.id) ? "inuse" : ""}" data-id="${tr.id}">
      <div class="swatch" style="background:${esc(tr.color || "#333")}"></div><b>${tr.id + 1}</b>${tr.loaded ? esc(tr.name || tr.type) : "Empty"}${spoolLeft(tr.id) != null ? `<div class="left ${spoolLeft(tr.id) < 60 ? "low" : ""}">~${Math.round(spoolLeft(tr.id))} g</div>` : ""}${usesOf(tr.id) ? `<div class="uses">in use${usesOf(tr.id).grams ? ` · ${Math.round(usesOf(tr.id).grams * toGo)} g to go` : ""}</div>` : ""}</div>`).join("")
    || `<div class="muted small">No Canvas data yet</div>`;
  if (document.activeElement !== $("#light")) $("#light").checked = !!p.light;
  if ($("#cam-light")) $("#cam-light").textContent = p.light ? "Light off" : "Light on";   // 11/14: light button on the camera bar
  if ($("#b-sleep")) { $("#b-sleep").classList.toggle("hidden", !!p.sleeping); $("#b-wake").classList.toggle("hidden", !p.sleeping);
    $("#sleep-label").textContent = p.sleeping ? "Asleep (light, heaters, camera off)" : "Night mode"; }
  if (document.activeElement !== $("#refill")) $("#refill").checked = !!p.auto_refill;
  $$("#speed button").forEach((b) => b.classList.toggle("on", ["Silent", "Balanced", "Sport", "Ludicrous"][b.dataset.v] === p.speed_mode));
  $("#cam-state").textContent = !ST.bridge ? "Bridge offline" : (p.camera || "");
}

// ---------- live bed (11/12): where the head is, drawn on the plate's real toolpaths ----------
// The printer reports the head's X/Y/Z about once a second; the PC already made a per-layer toolpath map when it sliced
// the plate (plate<N>.tp.gz, see bridge/preview.py). Prints started on the printer itself: head + trail only.
const LIVE_COL = { "Outer wall": "#ffa53d", "Inner wall": "#f2dd4a", "Overhang wall": "#3d7bff", "Sparse infill": "#b2344b",
  "Internal solid infill": "#b04ad6", "Solid infill": "#b04ad6", "Top surface": "#ff5a5a", "Bottom surface": "#6f7cff",
  "Bridge": "#6fb0ff", "Internal Bridge": "#6fb0ff", "Gap infill": "#ffffff", "Support": "#63d48a", "Support interface": "#2f9e5b",
  "Support transition": "#2f9e5b", "Brim": "#45d9d0", "Skirt": "#45d9d0", "Prime tower": "#999999", "Ironing": "#ff9ad5", "Other": "#888888" };
const BED_MM = 256;
const LIVE = { key: "", busy: false, layers: null, types: [], trail: [], from: null, to: null, cur: null, t0: 0, raf: 0,
  zoom: false, idx: -1, pIdx: -1, paths: null, near: null, layerNo: 0, total: 0 };

async function liveDecode(buf) {
  const raw = await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
  const dv = new DataView(raw);
  if (String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== "MPTP") throw new Error("Unknown preview format");
  let o = 5;
  const nT = dv.getUint8(o++), types = [];
  for (let i = 0; i < nT; i++) { const L = dv.getUint8(o++); types.push(new TextDecoder().decode(new Uint8Array(raw, o, L))); o += L; }
  const nL = dv.getUint32(o, true); o += 4;
  const layers = [];
  for (let l = 0; l < nL; l++) {
    const z = dv.getFloat32(o, true); o += 4;
    const n = dv.getUint32(o, true); o += 4;
    const lines = [];
    for (let k = 0; k < n; k++) {
      const t = dv.getUint8(o); o += 1;
      const np = dv.getUint16(o, true); o += 2;
      const pts = new Int16Array(np * 2);  // 0.01 mm
      for (let i = 0; i < np * 2; i++) { pts[i] = dv.getInt16(o, true); o += 2; }
      lines.push({ t, pts });
    }
    layers.push({ z, lines });
  }
  return { types, layers };
}

function livePlate() {  // which plate's toolpaths belong to what's printing now
  const f = (ST.printer || {}).filename, cur = f ? currentPrint() : null;
  if (!cur || !cur.j.id) return null;
  const pl = (cur.j.plates || []).find((x) => x.sent === f), n = pl ? pl.n : 1;
  return { key: `${cur.j.id}/${n}`, url: `/api/jobs/${cur.j.id}/asset/plate${n}.tp.gz` };
}

function liveLayerIdx(z, layerNo) {
  const Ls = LIVE.layers;
  if (!Ls || !Ls.length) return -1;
  if (layerNo > 0 && layerNo <= Ls.length && Math.abs(Ls[layerNo - 1].z - z) < 1.0) return layerNo - 1;  // printer's own count
  let lo = 0, hi = Ls.length - 1, best = 0;  // else: highest layer at or below the nozzle
  while (lo <= hi) { const m = (lo + hi) >> 1; if (Ls[m].z <= z + 0.05) { best = m; lo = m + 1; } else hi = m - 1; }
  return best;
}

function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  let t = L ? ((px - ax) * dx + (py - ay) * dy) / L : 0;
  t = Math.max(0, Math.min(1, t));
  const qx = ax + t * dx - px, qy = ay + t * dy - py;
  return Math.sqrt(qx * qx + qy * qy);
}

function liveNearest(idx, x, y) {  // the toolpath under the head = what it's working on; also where in the layer it is
  if (idx < 0) return null;
  const lines = LIVE.layers[idx].lines, prev = LIVE.prog && LIVE.prog.idx === idx ? LIVE.prog.li : 0;
  const scan = (from) => {
    let best = 1e9, r = null;
    for (let li = from; li < lines.length; li++) {
      const a = lines[li].pts;
      for (let i = 0; i + 3 < a.length; i += 2) {
        const d = segDist(x, y, a[i] / 100, a[i + 1] / 100, a[i + 2] / 100, a[i + 3] / 100);
        if (d < best) { best = d; r = { li, seg: i }; }
      }
    }
    if (best >= 2.5) return null;
    const a = lines[r.li].pts, ax = a[r.seg] / 100, ay = a[r.seg + 1] / 100, dx = a[r.seg + 2] / 100 - ax, dy = a[r.seg + 3] / 100 - ay, L = dx * dx + dy * dy;
    r.t = L ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / L)) : 0;  // how far along that segment
    return r;
  };
  // progress only moves forward within a layer: look ahead of where it was first (walls sit next to each other)
  const r = (prev > 0 && scan(Math.max(0, prev - 1))) || scan(0);
  if (!r) return null;
  const P = LIVE.prog;
  if (!P || P.idx !== idx || r.li > P.li || (r.li === P.li && (r.seg > P.seg || (r.seg === P.seg && r.t >= P.t)))) LIVE.prog = { idx, li: r.li, seg: r.seg, t: r.t };
  return lines[r.li];
}

// 11/15: between two position reports the head follows the toolpath, not a straight line. Build the stretch of toolpath
// from the old progress point to the new one, so the marker (and the printed colour) runs along it.
function liveRoute(idx, a, b) {
  const lines = LIVE.layers[idx].lines, R = [];
  const vx = (li, i) => [lines[li].pts[i] / 100, lines[li].pts[i + 1] / 100];
  const at = (p) => { const [ax, ay] = vx(p.li, p.seg), [bx, by] = vx(p.li, p.seg + 2); return [ax + (bx - ax) * p.t, ay + (by - ay) * p.t]; };
  R.push({ p: at(a), li: a.li, v: a.seg + 2 });
  for (let li = a.li; li <= b.li; li++) {
    const n = lines[li].pts.length, i0 = li === a.li ? a.seg + 2 : 0, i1 = li === b.li ? b.seg : n - 2;
    for (let i = i0; i <= i1; i += 2) { R.push({ p: vx(li, i), li, v: i + 2 }); if (R.length > 6000) return null; }
  }
  R.push({ p: at(b), li: b.li, v: b.seg + 2 });
  let L = 0; R[0].d = 0;
  for (let k = 1; k < R.length; k++) { L += Math.hypot(R[k].p[0] - R[k - 1].p[0], R[k].p[1] - R[k - 1].p[1]); R[k].d = L; }
  return L > 0.01 ? { R, L, k: 0 } : null;
}

function liveShownAt(pr) {  // drawn progress = exactly the reported progress point (no route to animate)
  const a = LIVE.layers[pr.idx].lines[pr.li].pts, s = pr.seg;
  return { idx: pr.idx, li: pr.li, v: s + 2, x: (a[s] + (a[s + 2] - a[s]) * pr.t) / 100, y: (a[s + 1] + (a[s + 3] - a[s + 1]) * pr.t) / 100, tip: true };
}

function livePath(lines) {
  const p = new Path2D();
  for (const ln of lines) {
    const a = ln.pts;
    p.moveTo(a[0] / 100, a[1] / 100);
    for (let i = 2; i < a.length; i += 2) p.lineTo(a[i] / 100, a[i + 1] / 100);
  }
  return p;
}

function livePaths(idx, sh = LIVE.shown) {  // Path2Ds for the current layer split into printed / still to print, + the layer below (grey)
  const pr = sh && sh.idx === idx ? sh : null, pk = pr ? pr.li + ":" + pr.v : "-";
  if (LIVE.pIdx === idx && LIVE.pKey === pk) return LIVE.paths;
  const lines = LIVE.layers[idx].lines, li0 = pr ? pr.li : -1, bk = idx + ":" + li0;
  if (LIVE.bKey !== bk) {  // everything except the line being printed: rebuilt only when the head moves on to another line
    const done = new Map(), todo = [];
    lines.forEach((ln, li) => {
      if (li < li0) { if (!done.has(ln.t)) done.set(ln.t, []); done.get(ln.t).push(ln); } else if (li > li0) todo.push(ln);
    });
    let total = 0, before = 0;
    lines.forEach((ln, li) => { const n = ln.pts.length / 2; total += n; if (li < li0) before += n; });
    LIVE.bulk = { byT: [...done].map(([t, ls]) => [t, livePath(ls)]), todo: todo.length ? livePath(todo) : null,
      below: idx > 0 ? livePath(LIVE.layers[idx - 1].lines) : null, total, before };
    LIVE.bKey = bk;
  }
  const B = LIVE.bulk, cur = {};
  if (pr) {  // current line: printed up to the last corner passed (+ the tip up to the head, drawn live)
    const ln = lines[li0], cut = Math.min(ln.pts.length, pr.v);
    if (cut >= 4) cur.done = [ln.t, livePath([{ pts: ln.pts.slice(0, cut) }])];
    if (cut < ln.pts.length) cur.todo = livePath([{ pts: ln.pts.slice(Math.max(0, cut - 2)) }]);
  }
  const got = pr ? B.before + pr.v / 2 : 0;
  LIVE.paths = { byT: cur.done ? B.byT.concat([cur.done]) : B.byT, todo: B.todo, todo2: cur.todo || null, below: B.below,
    pct: pr && B.total ? Math.min(100, Math.round(100 * got / B.total)) : null };
  LIVE.pIdx = idx; LIVE.pKey = pk;
  return LIVE.paths;
}

function liveUpdate(p, busy) {
  rwTick(busy);
  const card = $("#live-card"), show = busy && Array.isArray(p.pos) && p.pos[0] != null;
  card.classList.toggle("hidden", !show);
  if (!show) { LIVE.trail = []; LIVE.cur = LIVE.to = null; LIVE.prog = LIVE.shown = LIVE.route = null; return; }
  const lp = livePlate();
  if (!lp) { if (LIVE.key) { LIVE.key = ""; LIVE.layers = null; LIVE.pIdx = -1; LIVE.bKey = ""; } }
  else if (lp.key !== LIVE.key && !LIVE.busy) {
    LIVE.key = lp.key; LIVE.layers = null; LIVE.pIdx = -1; LIVE.bKey = ""; LIVE.prog = LIVE.shown = LIVE.route = null; LIVE.busy = true; LIVE.missing = false;
    apiBuf(lp.url).then(liveDecode).then((r) => { if (LIVE.key === lp.key) { LIVE.layers = r.layers; LIVE.types = r.types; } })
      .catch(() => { if (LIVE.key === lp.key) LIVE.missing = true; }).finally(() => { LIVE.busy = false; LIVE.to = null; render(); });
  }
  const [x, y, z] = p.pos.map(Number);
  LIVE.layerNo = p.layer || 0; LIVE.total = p.total_layers || (LIVE.layers ? LIVE.layers.length : 0);
  if (!LIVE.to || LIVE.to[0] !== x || LIVE.to[1] !== y || LIVE.to[2] !== z) {
    LIVE.from = LIVE.cur || [x, y, z]; LIVE.to = [x, y, z]; LIVE.t0 = performance.now();
    const last = LIVE.trail[LIVE.trail.length - 1];
    if (!last || last[0] !== x || last[1] !== y) { LIVE.trail.push([x, y]); if (LIVE.trail.length > 40) LIVE.trail.shift(); }
    LIVE.idx = liveLayerIdx(z, LIVE.layerNo);
    const old = LIVE.prog && LIVE.prog.idx === LIVE.idx ? LIVE.prog : null;
    LIVE.near = LIVE.layers ? liveNearest(LIVE.idx, x, y) : null;
    const pr = LIVE.prog && LIVE.prog.idx === LIVE.idx ? LIVE.prog : null;
    // finish any glide still running, then route along the toolpath from the old progress point to the new one
    if (LIVE.route && LIVE.route.end) LIVE.shown = LIVE.route.end;
    LIVE.route = old && pr && pr !== old ? liveRoute(LIVE.idx, old, pr) : null;
    if (LIVE.route) { LIVE.route.end = liveShownAt(pr); LIVE.from = LIVE.cur || LIVE.route.R[0].p.concat(z); }
    else LIVE.shown = pr ? liveShownAt(pr) : null;
  }
  const what = LIVE.near ? LIVE.types[LIVE.near.t] || "" : "";
  $("#live-what").textContent = what ? "· " + what : "";
  const lpct = LIVE.layers && LIVE.idx >= 0 ? livePaths(LIVE.idx).pct : null;
  if (RW.live) $("#live-info").textContent = `X ${x.toFixed(1)}  Y ${y.toFixed(1)}  Z ${z.toFixed(2)} · layer ${LIVE.layerNo}${LIVE.total ? "/" + LIVE.total : ""}${lpct != null ? ` (${lpct}% done)` : ""}`
    + (lp ? (LIVE.layers ? "" : LIVE.missing ? " · no plate map for this print, head only" : " · loading the plate map…") : " · started on the printer: no plate map, head only") + " · tap to zoom";
  if (!LIVE.raf) LIVE.raf = requestAnimationFrame(liveFrame);
}

function liveFrame(now) {
  LIVE.raf = 0;
  if (!LIVE.to || $("#live-card").classList.contains("hidden") || tab !== "printer" || document.hidden) return;
  const k = Math.min(1, (now - LIVE.t0) / 1800);  // glide between the ~1-2 s position reports
  const rt = LIVE.route;
  if (rt) {  // along the toolpath, at a steady speed
    const d = k * rt.L, R = rt.R;
    while (rt.k < R.length - 2 && R[rt.k + 1].d <= d) rt.k++;
    const A = R[rt.k], B = R[rt.k + 1], f = B.d > A.d ? (d - A.d) / (B.d - A.d) : 1;
    const px = A.p[0] + (B.p[0] - A.p[0]) * f, py = A.p[1] + (B.p[1] - A.p[1]) * f;
    LIVE.cur = [px, py, LIVE.from[2] + (LIVE.to[2] - LIVE.from[2]) * k];
    // on a travel move between two lines nothing is being laid down: no coloured tip
    LIVE.shown = k >= 1 ? rt.end : { idx: LIVE.idx, li: A.li, v: A.v, x: px, y: py, tip: B.li === A.li };
    if (k >= 1) LIVE.route = null;
  } else LIVE.cur = LIVE.from.map((v, i) => v + (LIVE.to[i] - v) * k);
  liveDraw();
  if (k < 1) LIVE.raf = requestAnimationFrame(liveFrame);
}

function liveDraw() {
  const R = RW.live ? null : rwBed();  // seek bar moved back: draw that moment instead
  const cv = $("#live-bed"), ctx = cv.getContext("2d"), W = cv.width, c = R ? R.cur : LIVE.cur || LIVE.to;
  const idx = R ? R.idx : LIVE.idx, shown = R ? R.shown : LIVE.shown, near = R ? R.near : LIVE.near, trail = R ? R.trail : LIVE.trail;
  if (R && R.cur) $("#live-info").textContent = `Looking back: X ${R.cur[0].toFixed(1)}  Y ${R.cur[1].toFixed(1)}  Z ${R.cur[2].toFixed(2)} · layer ${R.layer || "?"} · tap LIVE to come back`;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#15171c"; ctx.fillRect(0, 0, W, W);
  if (!c) return;
  let s = W / BED_MM, ox = 0, oy = 0;
  if (LIVE.zoom) { s *= 3; ox = W / 2 - c[0] * s; oy = W / 2 - (BED_MM - c[1]) * s; }
  ctx.setTransform(s, 0, 0, -s, ox, oy + BED_MM * s);  // millimetres, y up (printer front at the bottom)
  ctx.lineCap = ctx.lineJoin = "round";
  ctx.fillStyle = "#1d2027"; ctx.fillRect(0, 0, BED_MM, BED_MM);
  ctx.strokeStyle = "#2a2e37"; ctx.lineWidth = 1 / s;
  for (let v = 0; v <= BED_MM; v += 32) { ctx.beginPath(); ctx.moveTo(v, 0); ctx.lineTo(v, BED_MM); ctx.moveTo(0, v); ctx.lineTo(BED_MM, v); ctx.stroke(); }
  ctx.fillStyle = "rgba(255,255,255,0.05)"; ctx.fillRect(246, 0, 10, 20);  // purge chute corner (no printing)
  if (LIVE.layers && idx >= 0) {
    const P = livePaths(idx, shown);
    ctx.lineWidth = 0.45;
    if (P.below) { ctx.strokeStyle = "#3b404c"; ctx.stroke(P.below); }
    if (P.todo || P.todo2) {  // still to print
      ctx.strokeStyle = "#5a6070"; ctx.setLineDash([0.6, 0.9]); if (P.todo) ctx.stroke(P.todo); if (P.todo2) ctx.stroke(P.todo2); ctx.setLineDash([]);
    }
    ctx.globalAlpha = 0.95;
    for (const [t, path] of P.byT) { ctx.strokeStyle = LIVE_COL[LIVE.types[t]] || "#888"; ctx.stroke(path); }
    const sh = shown;
    if (sh && sh.idx === idx && sh.tip && sh.v >= 2) {  // the bit of the current line laid since the last corner, up to the head
      const ln = LIVE.layers[idx].lines[sh.li], a = ln.pts;
      ctx.strokeStyle = LIVE_COL[LIVE.types[ln.t]] || "#888";
      ctx.beginPath(); ctx.moveTo(a[sh.v - 2] / 100, a[sh.v - 1] / 100); ctx.lineTo(sh.x, sh.y); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    if (near) { ctx.strokeStyle = "#ffffff"; ctx.lineWidth = Math.max(0.9, 2.2 / s); ctx.stroke(livePath([near])); }
  }
  ctx.strokeStyle = "rgba(255,255,255,0.13)"; ctx.lineWidth = 3 / s;  // the gantry across the bed at the head's Y
  ctx.beginPath(); ctx.moveTo(0, c[1]); ctx.lineTo(BED_MM, c[1]); ctx.stroke();
  trail.forEach(([x, y], i) => {
    ctx.fillStyle = `rgba(255,122,26,${(0.15 + 0.6 * i / trail.length).toFixed(2)})`;
    ctx.beginPath(); ctx.arc(x, y, 2.2 / s, 0, 7); ctx.fill();
  });
  ctx.beginPath(); ctx.arc(c[0], c[1], 9 / s, 0, 7);  // hollow ring: the toolpath under the nozzle stays visible
  ctx.strokeStyle = "#ff7a1a"; ctx.lineWidth = 3 / s; ctx.stroke(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.2 / s; ctx.stroke();
  ctx.beginPath(); for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) { ctx.moveTo(c[0] + dx * 9 / s, c[1] + dy * 9 / s); ctx.lineTo(c[0] + dx * 16 / s, c[1] + dy * 16 / s); }
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 / s; ctx.stroke();
}
$("#live-bed").onclick = () => { LIVE.zoom = !LIVE.zoom; liveDraw(); };

// ---------- rewind (11/15): a seek bar over the camera + live bed, like a live stream ----------
// The server keeps this print's camera frames (one per 10-20 s) and the head's track (~1/s); drag back to see that moment,
// ▶ plays forward at 60x (a minute of printing per second), LIVE jumps back.
const RW = { id: null, frames: [], track: [], now: 0, got: 0, live: true, t: 0, play: 0, busy: false, last: 0, on: false, liveRun: false, memo: null, img: -1 };
const rwPad = (n) => String(n).padStart(5, "0");
function rwNow() { return RW.now + (performance.now() - RW.got) / 1000; }
function rwT0() {  // the bar starts where the recording does (a print already running when the server came up starts later)
  const a = RW.track.length ? RW.track[0][0] : 1e12, b = RW.frames.length ? RW.frames[0][0] : 1e12;
  return Math.min(a, b, rwNow());
}
function rwFind(arr, t) { let lo = 0, hi = arr.length - 1, r = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (arr[m][0] <= t) { r = m; lo = m + 1; } else hi = m - 1; } return r; }
function rwFmt(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60; return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); }
async function rwFetch() {
  if (RW.busy) return;
  RW.busy = true; RW.last = Date.now();
  try {
    let r = await api(`/api/rewind?since=${RW.id ? RW.track.length : 0}`);
    if (r.id && RW.id && r.id !== RW.id) r = await api("/api/rewind?since=0");  // a new print started
    if (!r.id) { RW.id = null; return; }
    if (r.id !== RW.id) { RW.track = []; RW.memo = null; }
    RW.id = r.id; RW.liveRun = r.live; RW.frames = r.frames || []; RW.track = RW.track.concat(r.track || []);
    if (RW.track.length > r.track_n) RW.track = RW.track.slice(0, r.track_n);
    RW.now = r.now; RW.got = performance.now();
  } catch {} finally { RW.busy = false; rwShow(); }
}
function rwTick(busy) {
  RW.on = busy;
  if (!busy) { if (!RW.live) rwGoLive(); $("#rw").classList.add("hidden"); return; }
  if (Date.now() - RW.last > (RW.live ? 30000 : 15000) && tab === "printer" && !document.hidden) rwFetch();
  rwShow();
}
function rwShow() {
  const ok = RW.on && RW.id && RW.liveRun && (RW.track.length || RW.frames.length);
  $("#rw").classList.toggle("hidden", !ok);
  if (!ok) return;
  const now = rwNow();
  const bar = $("#rw-bar");
  if (RW.live || document.activeElement !== bar) bar.value = RW.live ? 1000 : Math.round(1000 * (RW.t - rwT0()) / Math.max(1, now - rwT0()));
  $("#rw").classList.toggle("past", !RW.live);
  const tr = RW.live ? null : RW.track[Math.max(0, rwFind(RW.track, RW.t))];
  $("#rw-time").textContent = RW.live ? rwFmt(now) : "-" + rwFmt(now - RW.t) + (tr && tr[4] ? " · L" + tr[4] : "");
  $("#rw-play").innerHTML = RW.play ? "&#10074;&#10074;" : "&#9654;";
}
function rwSeek(t) {
  const now = rwNow();
  RW.t = Math.max(rwT0(), Math.min(now, t));
  if (RW.t >= now - 3) return rwGoLive();
  RW.live = false;
  $(".cam-card").classList.add("past");
  const i = RW.frames.length ? Math.max(0, rwFind(RW.frames, RW.t)) : -1, pic = $("#cam-past");
  if (i < 0) { pic.removeAttribute("src"); RW.img = -1; }
  else if (i !== RW.img) { RW.img = i; pic.src = `${SERVER}/api/rewind/${RW.id}/${rwPad(i)}.jpg?token=${encodeURIComponent(TOKEN)}`; }
  pic.classList.remove("hidden");
  rwShow();
  if (!$("#live-card").classList.contains("hidden")) liveDraw();
}
function rwGoLive() {
  RW.live = true; clearInterval(RW.play); RW.play = 0; RW.memo = null; RW.img = -1;
  $(".cam-card").classList.remove("past"); $("#cam-past").classList.add("hidden");
  rwShow();
  if (!$("#live-card").classList.contains("hidden")) liveDraw();
}
function rwBed() {  // the bed view at the picked moment, from the recorded track + the plate map
  const k = RW.track.length ? Math.max(0, rwFind(RW.track, RW.t)) : -1;
  if (k < 0) return { cur: null, idx: -1, shown: null, near: null, trail: [] };
  if (RW.memo && RW.memo.k === k && RW.memo.L === LIVE.layers) return RW.memo.r;
  const [, x, y, z, layer] = RW.track[k], trail = [];
  for (let j = Math.max(0, k - 39); j <= k; j++) trail.push([RW.track[j][1], RW.track[j][2]]);
  let idx = -1, shown = null, near = null;
  if (LIVE.layers) {
    idx = liveLayerIdx(z, layer);
    const lines = idx >= 0 ? LIVE.layers[idx].lines : [];
    let best = 1e9, hit = null;
    for (let li = 0; li < lines.length; li++) {
      const a = lines[li].pts;
      for (let i = 0; i + 3 < a.length; i += 2) {
        const d = segDist(x, y, a[i] / 100, a[i + 1] / 100, a[i + 2] / 100, a[i + 3] / 100);
        if (d < best) { best = d; hit = { li, seg: i }; }
      }
    }
    if (hit && best < 2.5) {
      const a = lines[hit.li].pts, ax = a[hit.seg] / 100, ay = a[hit.seg + 1] / 100, dx = a[hit.seg + 2] / 100 - ax, dy = a[hit.seg + 3] / 100 - ay, L = dx * dx + dy * dy;
      shown = liveShownAt({ idx, li: hit.li, seg: hit.seg, t: L ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / L)) : 0 });
      near = lines[hit.li];
    }
  }
  const r = { cur: [x, y, z], idx, shown, near, trail, layer };
  RW.memo = { k, L: LIVE.layers, r };
  return r;
}
$("#rw-bar").oninput = (e) => { if (RW.play) { clearInterval(RW.play); RW.play = 0; } const t0 = rwT0(); rwSeek(t0 + (rwNow() - t0) * e.target.value / 1000); };
$("#rw-live").onclick = () => rwGoLive();
$("#rw-play").onclick = () => {
  if (RW.play) { clearInterval(RW.play); RW.play = 0; return rwShow(); }
  if (RW.live) rwSeek(rwT0());  // from live, ▶ replays the print from the start
  RW.play = setInterval(() => { rwSeek(RW.t + 12); if (RW.live) { clearInterval(RW.play); RW.play = 0; rwShow(); } }, 200);
  rwShow();
};

// ---------- camera (polls frames only while the printer tab is visible) ----------
let camOn = store.get("cam", true), camBusy = false, camLoop = null, camMode = "";
// live video: one long MJPEG stream (smooth, ~12 fps); single-picture polling only if the stream fails
function camTick() {
  const want = camOn && tab === "printer" && !document.hidden && TOKEN;
  $("#cam-off").classList.toggle("hidden", camOn);
  $("#cam-toggle").textContent = camOn ? "Pause video" : "Watch live";
  clearInterval(camLoop);
  const img = $("#cam");
  if (!want) {
    if (camMode === "stream") img.src = "";  // closes the stream
    camMode = ""; return;
  }
  if (camMode === "stream") return;
  if (camMode !== "poll") {
    camMode = "stream";
    img.onerror = () => { if (camMode === "stream") { camMode = "poll"; camTick(); } };
    img.src = `${SERVER}/api/cam.mjpg?token=${encodeURIComponent(TOKEN)}&_=${Date.now()}`;
    return;
  }
  const pull = () => {
    if (camBusy) return; camBusy = true;
    fetch(`${SERVER}/api/cam.jpg?token=${encodeURIComponent(TOKEN)}&_=${Date.now()}`)
      .then((r) => (r.ok ? r.blob() : null)).then((b) => {
        if (b) { const old = img.src; img.src = URL.createObjectURL(b); if (old.startsWith("blob:")) URL.revokeObjectURL(old); }
      }).catch(() => {}).finally(() => (camBusy = false));
  };
  pull(); camLoop = setInterval(pull, 450);
}
$("#cam-toggle").onclick = $("#cam-go").onclick = () => { camOn = !camOn; store.set("cam", camOn); camTick(); };
// tap the picture: full screen (and sideways where the phone allows it); tap again or go back to leave
$("#cam-past").onclick = () => $("#cam").onclick();
$("#cam").onclick = () => {
  const card = $(".cam-card"), on = !card.classList.contains("cam-full");
  card.classList.toggle("cam-full", on);
  try {
    // (the app's WebView "goes fullscreen" without hiding the system bars and blocks the sideways layout: browsers only)
    if (on && !isApp) card.requestFullscreen?.().then(() => screen.orientation?.lock?.("landscape")).catch(() => {});
    else if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  } catch {}
};
document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) $(".cam-card").classList.remove("cam-full"); });
document.addEventListener("visibilitychange", camTick);

// ---------- controls ----------
const cmd = (name, args) => api("/api/cmd/" + name, { json: args || {} });
$("#b-pause").onclick = (e) => act(e.target, () => cmd("pause"), "Pausing");
$("#b-resume").onclick = (e) => act(e.target, () => cmd("resume"), "Resuming");
$("#b-stop").onclick = () => confirmSheet("Stop this print?", "This cancels the print. It can't be resumed.", "Stop print", () => act(null, () => cmd("stop"), "Stopping"));
$("#light").onchange = (e) => act(null, () => cmd("light", { on: e.target.checked }));
$("#cam-light").onclick = (e) => { const on = e.target.textContent === "Light on"; act(e.target, () => cmd("light", { on }), on ? "Light on" : "Light off"); };
// 11/13: Sleep = light + heaters + camera off (the CC2 firmware has no real standby call)
$("#b-sleep").onclick = (e) => act(e.target, () => cmd("sleep"), "Printer asleep: light, heaters and camera off");
$("#b-wake").onclick = (e) => act(e.target, () => cmd("wake"), "Printer awake");
$("#refill").onchange = (e) => act(null, () => cmd("auto_refill", { on: e.target.checked }), e.target.checked ? "Auto refill on" : "Auto refill off");
$$("#speed button").forEach((b) => b.onclick = () => act(b, () => cmd("speed", { mode: +b.dataset.v }), "Speed: " + b.textContent));
function confirmSheet(title, text, label, fn) {
  openSheet(`<h3>${esc(title)}</h3><p class="muted">${esc(text)}</p><button class="btn danger wide" id="cf-go">${esc(label)}</button>`);
  $("#cf-go").onclick = () => { closeSheet(); fn(); };
}

// ---------- Canvas slot editor (the printer's own slot info) + load / unload ----------
$("#trays").onclick = (e) => { const el = e.target.closest(".tray[data-id]"); if (el) traySheet(+el.dataset.id); };
function traySheet(i) {
  const tr = (ST.printer.trays || []).find((t) => t.id === i) || {};
  const cur = FILAMENTS.find((f) => f[0] === tr.name) || FILAMENTS.find((f) => f[1] === tr.type) || FILAMENTS[0];
  const opts = (brand) => FILAMENTS.filter((f) => brand !== "ELEGOO" || f[5])
    .map((f) => `<option value="${esc(f[0])}" ${f[0] === cur[0] ? "selected" : ""}>${esc(f[0])}</option>`).join("");
  const busy = !["IDLE", "COMPLETE", "STOPPED", "ERROR", undefined].includes(ST.printer.state);
  openSheet(`<h3>Slot ${i + 1}</h3><p class="muted small">What's loaded here. The printer, its auto refill and the app's slicing all go by this.</p>
    <div class="field"><div>Brand</div><select id="ts-brand"><option>Generic</option><option ${/elegoo/i.test(tr.brand || "") ? "selected" : ""}>ELEGOO</option></select></div>
    <div class="field"><div>Filament</div><select id="ts-fil">${opts("Generic")}</select></div>
    <div class="field"><div>Colour</div><input type="color" id="ts-col" value="${esc((tr.color || "#ffffff").slice(0, 7))}"></div>
    <button class="btn primary wide" id="ts-save">Save to the printer</button>
    <div class="row-between" style="gap:8px;margin-top:8px"><button class="btn wide" id="ts-load" ${busy ? "disabled" : ""}>Load</button><button class="btn wide" id="ts-unload" ${busy ? "disabled" : ""}>Unload</button></div>
    ${busy ? `<p class="muted small">Load and unload wait until the printer is idle.</p>` : ""}`);
  $("#ts-brand").onchange = () => { const v = $("#ts-fil").value; $("#ts-fil").innerHTML = opts($("#ts-brand").value); if ([...$("#ts-fil").options].some((o) => o.value === v)) $("#ts-fil").value = v; };
  $("#ts-save").onclick = (e) => act(e.target, async () => {
    const f = FILAMENTS.find((x) => x[0] === $("#ts-fil").value);
    await cmd("set_tray", { tray: i, brand: $("#ts-brand").value, name: f[0], type: f[1], code: f[2], min: f[3], max: f[4], color: $("#ts-col").value });
    if (SET && SET.materials && SET.materials[i]) {  // the printer knows now: drop the app-only override
      const m = { ...SET.materials }; delete m[i];
      SET = await api("/api/settings", { method: "PUT", json: { ...SET, materials: m } });
    }
    closeSheet();
  }, "Slot saved");
  $("#ts-load").onclick = (e) => act(e.target, async () => { await cmd("load", { tray: i }); closeSheet(); }, `Loading slot ${i + 1}`);
  $("#ts-unload").onclick = (e) => act(e.target, async () => { await cmd("unload", { tray: i }); closeSheet(); }, `Unloading slot ${i + 1}`);
}

// ---------- maintenance + emergency stop ----------
$$(".maint [data-m]").forEach((b) => b.onclick = () => {
  const m = b.dataset.m, args = m === "heat" ? { nozzle: +b.dataset.n, bed: +b.dataset.b } : {};
  const run = () => act(b, () => cmd(m, args), b.textContent + ": sent");
  if (b.dataset.long) { const [t, d] = b.dataset.long.split("|"); confirmSheet(t, d, "Start", run); } else run();
});
$("#estop").onclick = () => confirmSheet("Emergency stop?", "Cuts the motors and heaters at once. The print can't be resumed, and the printer may need a restart.", "Stop everything", () => act($("#estop"), () => cmd("estop"), "Emergency stop sent"));

$("#bridge-restart").onclick = () => confirmSheet("Restart the bridge?", "The bridge at home (tablet or PC) restarts and reconnects in about 15 seconds. A running print isn't affected.", "Restart",
  () => act($("#bridge-restart"), () => cmd("restart"), "Bridge restarting"));

// ---------- service log ----------
let SVC = null;
async function renderService(d) {
  try { SVC = d || await api("/api/service"); } catch (e) { return; }
  $("#svc-h").textContent = `${SVC.hours} h printed`;
  $("#svc").innerHTML = SVC.tasks.map((t) => {
    const left = [t.left_h != null ? (t.left_h <= 0 ? `${Math.round(-t.left_h)} h overdue` : `in ${Math.round(t.left_h)} h`) : "",
      t.left_d != null ? (t.left_d <= 0 ? `${Math.round(-t.left_d)} days overdue` : `in ${Math.round(t.left_d)} days`) : ""].filter(Boolean).join(" or ");
    return `<div class="svc-row ${t.due ? "due" : ""}"><div class="grow">${esc(t.name)}<br><span class="small ${t.due ? "" : "muted"}">${t.due ? "Due: " : "Next "}${left} · every ${[t.hours ? t.hours + " h" : "", t.days ? t.days + " days" : ""].filter(Boolean).join(" / ")}</span></div>
      <button class="btn small ${t.due ? "primary" : "ghost"}" data-done="${t.id}">Done</button><button class="btn ghost small" data-del="${t.id}">✕</button></div>`;
  }).join("") || `<div class="muted small">No tasks.</div>`;
  $$("#svc [data-done]").forEach((b) => b.onclick = () => act(b, async () => renderService(await api(`/api/service/${b.dataset.done}/done`, { method: "POST" })), "Ticked off"));
  $$("#svc [data-del]").forEach((b) => b.onclick = () => act(b, async () => renderService(await api("/api/service", { method: "PUT", json: { tasks: SVC.tasks.filter((t) => t.id !== b.dataset.del) } })), "Removed"));
}
$("#svc-addb").onclick = (e) => act(e.target, async () => {
  const name = $("#svc-name").value.trim(), hours = +$("#svc-hours").value;
  if (!name || !hours) throw new Error("Give it a name and how many print hours between");
  renderService(await api("/api/service", { method: "PUT", json: { tasks: [...SVC.tasks, { name, hours }] } }));
  $("#svc-name").value = ""; $("#svc-hours").value = "";
}, "Task added");

// ---------- print queue ----------
let QUEUE = [];
function renderQueue(q) {
  if (q) QUEUE = q;
  $("#queue-card").classList.toggle("hidden", !QUEUE.length);
  const total = QUEUE.reduce((a, x) => a + (x.secs || 0), 0);
  $("#q-total").textContent = QUEUE.length ? `${QUEUE.length} waiting · ${fmtT(total)} to clear` : "";
  $("#queue").innerHTML = QUEUE.map((x, k) => `<div class="hrow"><span class="muted">${k + 1}</span><span class="grow">${esc(x.label)}<br><span class="muted small">${x.secs ? fmtT(x.secs) : ""}${x.grams ? " · " + x.grams.toFixed(0) + " g" : ""}</span></span>
    ${k ? `<button class="btn ghost small" data-up="${x.id}">↑</button>` : ""}<button class="btn ghost small" data-qdel="${x.id}">✕</button></div>`).join("");
  $$("#queue [data-qdel]").forEach((b) => b.onclick = () => act(b, async () => renderQueue(await api(`/api/queue/${b.dataset.qdel}`, { method: "DELETE" })), "Removed from the queue"));
  $$("#queue [data-up]").forEach((b) => b.onclick = () => act(b, async () => {
    const ids = QUEUE.map((x) => x.id), k = ids.indexOf(b.dataset.up); [ids[k - 1], ids[k]] = [ids[k], ids[k - 1]];
    renderQueue(await api("/api/queue/order", { json: { ids } }));
  }));
}
$("#q-clear").onclick = () => confirmSheet("Clear the queue?", `Removes all ${QUEUE.length} waiting prints from the queue. Nothing that's already printing is touched.`, "Clear the queue",
  () => act(null, async () => renderQueue(await api("/api/queue", { method: "DELETE" })), "Queue cleared"));
$("#q-start").onclick = (e) => {
  const st = ST.printer.state;
  if (st && !["IDLE", "COMPLETE", "STOPPED"].includes(st)) return toast("The printer is busy (" + (NICE[st] || st) + ")", 4000);
  confirmSheet("Start the next print?", `${QUEUE[0] ? QUEUE[0].label : ""}. Make sure the bed is clear.`, "Start it", () => act(e.target, () => startChecked((force) => api("/api/queue/start", { json: { force } }), "Starting")));
};

// ---------- bed-clear check ----------
// A start the server refused because the camera still sees something on the bed: show what it sees, offer "start anyway".
async function startChecked(fn, ok) {
  try { await fn(false); if (ok) toast(ok); } catch (e) {
    if (e.status !== 409 || !/still seems to be on the bed/.test(e.message)) throw e;
    openSheet(`<h3>Is the bed clear?</h3><p class="muted">${esc(e.message)}</p>
      <img class="bed-shot" src="${SERVER}/api/cam.jpg?token=${encodeURIComponent(TOKEN)}&_=${Date.now()}" alt="">
      <button class="btn danger wide" id="bed-force">It's clear: start anyway</button>`);
    $("#bed-force").onclick = (ev) => act(ev.target, async () => { await fn(true); closeSheet(); }, ok);
  }
}
async function renderBed(d) {
  try { d = d || await api("/api/bed"); } catch (e) { return; }
  $("#bed-h").innerHTML = !d.available ? "Not available on this server."
    : d.ref ? `On: queued and remote starts wait until the bed looks like this photo (saved ${new Date(d.ref_t * 1000).toLocaleDateString([], { month: "short", day: "numeric" })}).`
    : "Off. Take everything off the bed, let the head park, then save a photo of it empty. After that the queue and notification buttons won't start a print on top of the last one.";
  $("#bed-ref").innerHTML = d.ref ? `<img class="bed-shot" src="${SERVER}/api/bed/empty.jpg?token=${encodeURIComponent(TOKEN)}&_=${Math.round(d.ref_t)}" alt="Empty bed">` : "";
  $("#bed-check").classList.toggle("hidden", !d.ref); $("#bed-off").classList.toggle("hidden", !d.ref);
  $("#bed-save").textContent = d.ref ? "Bed's empty now: retake photo" : "Bed's empty now: save photo";
}
$("#bed-save").onclick = (e) => act(e.target, async () => { $("#bed-out").textContent = ""; renderBed(await api("/api/bed/empty", { method: "POST" })); }, "Empty-bed photo saved");
$("#bed-off").onclick = (e) => act(e.target, async () => { $("#bed-out").textContent = ""; renderBed(await api("/api/bed/empty", { method: "DELETE" })); }, "Bed check off");
$("#bed-check").onclick = (e) => act(e.target, async () => {
  const r = await api("/api/bed/check", { method: "POST" });
  $("#bed-out").innerHTML = r.clear ? `<span style="color:var(--ok)">✓ Looks clear</span> <span class="muted">(${r.score} / ${r.tol})</span>`
    : `<span class="err-t">Something's on the bed</span> <span class="muted">(${r.score} / ${r.tol})</span>`;
});

// ---------- lock-screen progress (Android app): a native ongoing notification that follows the print ----------
// no @capacitor/core here, so call the native "Progress" plugin through the bridge directly
const Progress = isApp && window.Capacitor.nativePromise ? {
  start: (o) => window.Capacitor.nativePromise("Progress", "start", o),
  stop: () => window.Capacitor.nativePromise("Progress", "stop", {}),
} : null;
function progressTick(busy) {
  if (!Progress || !store.get("lockprog", true)) return;
  if (busy && !progressTick.on) Progress.start({ server: SERVER, token: TOKEN }).catch(() => { progressTick.on = false; });
  progressTick.on = busy;  // the service stops itself when the print ends; the next print starts it again
}

// ---------- sheet ----------
function openSheet(html) { $("#sheet-c").innerHTML = html; $("#sheet").classList.remove("hidden"); $(".sheet-body").scrollTop = 0; }
function closeSheet() { $("#sheet").classList.add("hidden"); $("#sheet-c").innerHTML = ""; }
$("#sheet-x").onclick = closeSheet;
$("#sheet").onclick = (e) => { if (e.target.id === "sheet") closeSheet(); };

// ---------- models ----------
let src = store.get("src", "nexprint"), page = 1, query = "";
$$("#sources button").forEach((b) => {
  b.classList.toggle("on", b.dataset.s === src);
  b.onclick = () => { src = b.dataset.s; store.set("src", src); $$("#sources button").forEach((x) => x.classList.toggle("on", x === b)); renderPSorts(); loadModels(true); };
});
$("#q-go").onclick = () => { query = $("#q").value.trim(); loadModels(true); };
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#q-go").click(); });
$("#more").onclick = () => { page++; loadModels(false); };
// Printables: more ways to browse than "trending" (other sort orders, contest winners, the contests themselves)
const P_SORTS = [["trending", "Trending"], ["month", "This month"], ["alltime", "All-time"], ["downloads", "Most downloaded"],
  ["makes", "Most made"], ["rated", "Best rated"], ["newest", "Newest"], ["winners", "Contest winners"], ["contests", "Contests"]];
let psort = store.get("psort", "trending"), contest = null;
function renderPSorts() {
  const el = $("#p-sorts");
  el.classList.toggle("hidden", src !== "printables");
  el.innerHTML = P_SORTS.map(([k, t]) => `<button data-k="${k}" class="${k === psort ? "on" : ""}">${t}</button>`).join("");
  $$("#p-sorts button").forEach((b) => b.onclick = () => {
    psort = b.dataset.k; store.set("psort", psort); contest = null; query = ""; $("#q").value = ""; renderPSorts(); loadModels(true);
  });
}
renderPSorts();
async function loadContests() {
  const cs = await api("/api/printables/contests");
  $("#m-note").textContent = "Printables contests: open ones first. Tap one to see its winners.";
  $("#grid").classList.add("as-list");
  $("#grid").innerHTML = cs.map((c) => `<div class="list-item tap" data-c="${esc(c.id)}"><div class="grow"><div class="t">${esc(c.name)}</div>
    <div class="small ${c.open ? "stage-printing" : "muted"}">${c.open ? "Open now" + (c.ends ? " · until " + new Date(c.ends).toLocaleDateString([], { month: "short", day: "numeric" }) : "") : "Finished"} · ${c.entries} entries</div></div></div>`).join("");
  $$("#grid [data-c]").forEach((el) => el.onclick = () => { contest = cs.find((c) => String(c.id) === el.dataset.c); loadModels(true); });
}
// Sites that block automated searches (Cloudflare bot check): open their own search page in the browser instead
const SITE_SEARCH = {
  yeggi: (q) => `https://www.yeggi.com/q/${encodeURIComponent(q || "benchy")}/`,
  makerworld: (q) => q ? `https://makerworld.com/en/search/models?keyword=${encodeURIComponent(q)}` : "https://makerworld.com/en",
  thingiverse: (q) => q ? `https://www.thingiverse.com/search?q=${encodeURIComponent(q)}&type=things` : "https://www.thingiverse.com/",
  cults3d: (q) => q ? `https://cults3d.com/en/search?q=${encodeURIComponent(q)}` : "https://cults3d.com/en",
};
const openOnSite = (s, q) => `<a class="btn ghost small" href="${esc(SITE_SEARCH[s](q))}" target="_blank" rel="noopener">Search ${q ? "“" + esc(q) + "” " : ""}on ${s === "yeggi" ? "Yeggi" : esc(s)} ↗</a>`;
const ADULT = { cults3d: "cults_adult", printables: "printables_adult" };   // Settings: adult (NSFW) results, off by default, per device
[["#cults-adult", "cults3d"], ["#printables-adult", "printables"]].forEach(([id, site]) => {
  if (!$(id)) return;
  $(id).checked = !!store.get(ADULT[site], false);
  $(id).onchange = (e) => { store.set(ADULT[site], e.target.checked); if (src === site) loadModels(true); };
});
async function loadModels(reset) {
  if (reset) { page = 1; $("#grid").innerHTML = ""; }
  if (src === "yeggi") {   // Yeggi = a search engine over every model site; no API, so it opens in the browser
    $("#more").classList.add("hidden");
    $("#m-note").innerHTML = `Yeggi searches Thingiverse, Printables, MakerWorld, Cults3D and dozens more at once. It opens in your browser:
      download the file there, then use <b>Print a file from this phone</b>.<br>${openOnSite("yeggi", query)}`;
    return;
  }
  $("#grid").classList.remove("as-list");
  $("#m-note").textContent = "Loading…"; $("#more").classList.add("hidden");
  try {
    const pf = src === "printables" && !query;
    let items, note = query ? `Results for “${esc(query)}”` : "Trending";
    if (pf && psort === "contests" && !contest) return await loadContests();
    if (pf && contest) {
      const d = page > 1 ? { items: [] } : await api(`/api/printables/contests/${encodeURIComponent(contest.id)}`);
      items = d.items;
      note = `<button class="btn ghost small" id="c-back">← Contests</button> ${esc(contest.name)}: ${items.length ? "winners" : "no winners announced yet"}
        ${!items.length && page === 1 ? ` · <a href="${esc(d.url || contest.url)}" target="_blank" rel="noopener">see the entries on Printables</a>` : ""}`;
    } else if (pf) {
      items = await api(`/api/printables/feed?sort=${psort}&page=${page}`);
      note = (P_SORTS.find((s) => s[0] === psort) || P_SORTS[0])[1];
    } else items = await api(`/api/models/${src}/search?q=${encodeURIComponent(query)}&page=${page}${ADULT[src] && store.get(ADULT[src], false) ? "&adult=1" : ""}`);
    $("#m-note").innerHTML = note;
    if ($("#c-back")) $("#c-back").onclick = () => { contest = null; loadModels(true); };
    $("#grid").insertAdjacentHTML("beforeend", items.map((m) => `<div class="model" data-id="${esc(m.id)}">
      <img loading="lazy" src="${img(m.thumb)}" alt=""><div class="mi"><div class="mt">${esc(m.title)}</div>
      <div class="ms">${m.price ? `<b class="paid">${esc(m.price)}</b> · ` : ""}${esc(m.author)} · ♥ ${m.likes || 0} · ⤓ ${m.downloads || 0}</div></div></div>`).join(""));
    $$("#grid .model").forEach((el) => el.onclick = () => openModel(src, el.dataset.id));
    $("#more").classList.toggle("hidden", items.length < 10);
    if (!items.length && reset && !(pf && contest)) $("#m-note").textContent = "Nothing found";
  } catch (e) {
    $("#m-note").textContent = /cloudflare|challenge/i.test(e.message)
      ? `${src} blocks searches coming from the MyPrintCloud server (bot check), so search it in your browser:` : e.message;
    if (SITE_SEARCH[src]) $("#m-note").insertAdjacentHTML("beforeend", `<br>${openOnSite(src, query)}`);
  }
}

function trayPicker(id, n, chosen, palette) {
  const trays = (ST.printer.trays || []).filter((t) => t.loaded);
  if (!trays.length) return `<div class="muted small">No loaded Canvas slots reported.</div>`;
  let h = "";
  for (let i = 0; i < n; i++) {
    const sw = palette && palette[i] ? `<span class="dot" style="background:${esc(palette[i])}" title="the designer's colour"></span> → ` : "";
    h += `<div class="small muted">${n > 1 ? sw + "Colour " + (i + 1) : "Print with"}</div><div class="map" data-i="${i}">` +
      trays.map((t) => `<button data-tray="${t.id}" class="${chosen[i] === t.id ? "on" : ""}"><span class="dot" style="background:${esc(t.color)}"></span>${t.id + 1} · ${esc(slotMaterial(t.id) || t.name || t.type)}</button>`).join("") + `</div>`;
  }
  return `<div id="${id}">${h}</div>`;
}
function colourDist(a, b) {  // rough perceived colour difference ("redmean"), same as the PC's combine.py
  const p = (h) => { h = String(h || "#808080").replace("#", "").padEnd(6, "0"); return [0, 2, 4].map((k) => parseInt(h.slice(k, k + 2), 16) || 0); };
  const [r1, g1, b1] = p(a), [r2, g2, b2] = p(b), rm = (r1 + r2) / 2;
  return Math.sqrt((2 + rm / 256) * (r1 - r2) ** 2 + 4 * (g1 - g2) ** 2 + (2 + (255 - rm) / 256) * (b1 - b2) ** 2);
}
function nearestTrays(palette) {  // each designer colour -> the loaded slot closest in colour (one material only)
  const loaded = (ST.printer.trays || []).filter((t) => t.loaded);
  if (!loaded.length || !palette || !palette.length) return null;
  const ref = loaded.find((t) => t.active) || loaded[0];
  const same = loaded.filter((t) => (t.type || "") === (ref.type || ""));
  return palette.map((c) => (c ? same.reduce((a, t) => (colourDist(c, t.color) < colourDist(c, a.color) ? t : a)) : ref).id);
}
function defaultTrays(n) {
  const trays = (ST.printer.trays || []).filter((t) => t.loaded);
  const order = [...trays.filter((t) => t.active), ...trays.filter((t) => !t.active)].map((t) => t.id);
  return Array.from({ length: n }, (_, i) => order[i % Math.max(1, order.length)]);
}
function bindTrays(id, chosen) {
  $$(`#${id} .map`).forEach((row) => $$("button", row).forEach((b) => b.onclick = () => {
    chosen[+row.dataset.i] = +b.dataset.tray; $$("button", row).forEach((x) => x.classList.toggle("on", x === b));
  }));
}
const fmtB = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
const PRINTABLE = /\.(stl|3mf|gcode|obj|step|stp)$/i;

async function openModel(s, id) {
  openSheet(`<div class="muted">Loading…</div>`);
  let m;
  try { m = await api(`/api/models/${s}/detail/${encodeURIComponent(id)}`); } catch (e) { $("#sheet-c").innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  const files = (m.files || []).filter((f) => PRINTABLE.test(f.name || ""));
  const pick = files.find((f) => /\.3mf$/i.test(f.name) && f.profile) || files.find((f) => /\.(stl|3mf)$/i.test(f.name)) || files[0];
  let n = 1, chosen = defaultTrays(4);
  openSheet(`<h3 style="margin-right:40px">${esc(m.title)}</h3><div class="muted small">by ${esc(m.author)}${m.license && !/^licenseType/.test(m.license) ? " · " + esc(m.license) : ""}</div>
    <div class="gallery">${(m.images || []).slice(0, 12).map((u) => `<img loading="lazy" src="${img(u)}">`).join("")}</div>
    <div class="card-h">Files</div>
    ${files.length > 1 ? `<div class="muted small">Tick several to print them together on one plate (e.g. the frame in one colour, the armor in another).</div>` : ""}
    ${files.length ? files.map((f) => `<label class="file"><input type="checkbox" name="mf" value="${esc(f.id)}" data-name="${esc(f.name)}" ${f === pick ? "checked" : ""}>
      <span class="grow">${esc(f.name)}${f.profile ? `<br><span class="muted">Print profile: ${esc(f.profile)}</span>` : ""}</span><span class="muted small">${fmtB(f.size || 0)}</span></label>`).join("")
      : `<div class="muted">No STL/3MF files on this model.</div>`}
    <div class="card-h sub">Filament</div><div id="mt-wrap">${trayPicker("mt", n, chosen)}</div>
    <div class="row-between"><span>Colours in this model</span><div class="seg" id="ncol">${[1, 2, 3, 4].map((k) => `<button data-v="${k}" class="${k === n ? "on" : ""}">${k}</button>`).join("")}</div></div>
    <p class="muted small">Sliced on your PC with your slicing settings. “Prepare” lets you resize, cut it into parts and check every layer first.</p>
    <button class="btn primary wide" id="m-prep" ${files.length ? "" : "disabled"}>Prepare &amp; preview</button>
    <button class="btn wide" id="m-print" ${files.length ? "" : "disabled"}>Print now (skip preview)</button>
    <button class="btn ghost wide" id="m-upload" ${files.length ? "" : "disabled"}>Send to printer without starting</button>
    <div class="card-h sub">About</div><div class="desc">${sanitize(m.description_html)}</div>
    <a class="btn ghost wide" href="${esc(m.url)}" target="_blank" rel="noopener">Open on ${esc(s)}</a>`);
  bindTrays("mt", chosen);
  const ticked = () => $$('input[name="mf"]:checked');
  const syncPick = () => {
    const k = ticked().length;
    $("#m-prep").textContent = k > 1 ? `Prepare ${k} on one plate` : "Prepare & preview";
    $("#m-prep").disabled = !k;
    $("#m-print").disabled = $("#m-upload").disabled = k !== 1;
  };
  $$('input[name="mf"]').forEach((c) => c.onchange = syncPick);
  $$("#ncol button").forEach((b) => b.onclick = () => {
    n = +b.dataset.v; $$("#ncol button").forEach((x) => x.classList.toggle("on", x === b));
    $("#mt-wrap").innerHTML = trayPicker("mt", n, chosen); bindTrays("mt", chosen);
  });
  const send = (start, mode) => (e) => act(e.target, async () => {
    const all = ticked();
    if (!all.length) throw new Error("Pick a file");
    if (all.length > 1 && mode === "prepare") return prepareTogether(s, m, all, chosen.slice(0, n));
    const r = all[0];
    const job = await api("/api/print/model", { json: { source: s, id: m.id, file_id: r.value, file_name: r.dataset.name, title: m.title,
      thumb: m.thumb, trays: chosen.slice(0, n), start, mode } });
    upsertJob(job);
    if (mode === "prepare" && !/\.gcode$/i.test(r.dataset.name)) return openStudio(job);
    closeSheet(); $('nav button[data-tab="jobs"]').click();
  }, mode === "prepare" ? "" : start ? "Sent! Slicing on your PC…" : "Sending to the printer…");
  $("#m-prep").onclick = send(false, "prepare"); $("#m-print").onclick = send(true); $("#m-upload").onclick = send(false);
  syncPick();
}
async function prepareTogether(s, m, boxes, trays) {  // several files of one model -> measured -> one shared plate
  const files = boxes.map((b) => ({ id: b.value, name: b.dataset.name }));
  const st = $("#m-prep");
  const jobs = [];
  for (const [k, f] of files.entries()) {
    st.textContent = `Fetching ${k + 1} of ${files.length}…`;
    const job = await api("/api/print/model", { json: { source: s, id: m.id, file_id: f.id, file_name: f.name, title: `${m.title} · ${f.name.replace(/\.[^.]+$/, "")}`,
      thumb: m.thumb, trays: k === 0 ? trays : null, start: false, mode: "prepare" } });
    upsertJob(job); jobs.push(job.id);
  }
  for (let t = 0; t < 120; t++) {  // wait until every file has been measured (each becomes "Ready to prepare")
    const all = await api("/api/jobs");
    JOBS = all; renderJobs.loaded = true;
    const mine = jobs.map((id) => all.find((j) => j.id === id));
    const bad = mine.find((j) => j && ["failed", "cancelled"].includes(j.stage));
    if (bad) throw new Error(`${bad.filename}: ${bad.msg || bad.stage}`);
    const ready = mine.filter((j) => j && j.info && !WAIT_STAGES.includes(j.stage)).length;
    st.textContent = `Measuring ${ready} of ${jobs.length}…`;
    if (ready === jobs.length) return openCombined(jobs);
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error("Measuring is taking long: they're in Jobs; use “Put several on one plate” there when they're ready");
}
function sanitize(html) {
  const d = new DOMParser().parseFromString(String(html || ""), "text/html");
  d.querySelectorAll("script,style,iframe,object,embed,form,link,meta").forEach((x) => x.remove());
  d.querySelectorAll("*").forEach((el) => [...el.attributes].forEach((a) => {
    if (/^on/i.test(a.name) || (/^(href|src)$/i.test(a.name) && /^\s*javascript:/i.test(a.value))) el.removeAttribute(a.name);
  }));
  d.querySelectorAll("img").forEach((i) => { if (/^https:/.test(i.src)) i.src = img(i.getAttribute("src")); i.loading = "lazy"; });
  d.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  return d.body.innerHTML;
}

// upload from phone: one file -> the colour/slot sheet; several -> each becomes a job ready to prepare
const MODEL_RE = /\.(stl|3mf|gcode|obj|step|stp)$/i;
async function uploadMany(files) {
  const bad = files.filter((f) => !MODEL_RE.test(f.name));
  const good = files.filter((f) => MODEL_RE.test(f.name));
  if (bad.length) toast(`Skipped ${bad.map((f) => f.name).join(", ")}: not an STL, 3MF, OBJ, STEP or G-code file`, 6000);
  if (!good.length) return;
  if (good.length === 1) return uploadOne(good[0]);
  for (const [k, f] of good.entries()) {
    toast(`Uploading ${k + 1} of ${good.length}: ${f.name}`, 3000);
    const fd = new FormData(); fd.append("file", f, f.name); fd.append("options", JSON.stringify({ mode: "prepare" }));
    try { upsertJob(await api("/api/print/upload", { body: fd, method: "POST" })); } catch (e) { toast(`${f.name}: ${e.message}`, 6000); }
  }
  toast(`${good.length} files added: open them in Jobs to prepare`, 5000);
  $('nav button[data-tab="jobs"]').click();
}
$$(".upfile").forEach((inp) => inp.onchange = (e) => { const fs = [...e.target.files]; e.target.value = ""; uploadMany(fs); });
async function uploadOne(f) {
  let n = 1, chosen = defaultTrays(4);
  openSheet(`<h3>${esc(f.name)}</h3><div class="muted small">${fmtB(f.size)}</div>
    <div class="card-h sub">Filament</div><div id="ut-wrap">${trayPicker("ut", n, chosen)}</div>
    <div class="row-between"><span>Colours</span><div class="seg" id="ucol">${[1, 2, 3, 4].map((k) => `<button data-v="${k}" class="${k === n ? "on" : ""}">${k}</button>`).join("")}</div></div>
    <button class="btn primary wide" id="u-prep">Prepare &amp; preview</button>
    <button class="btn wide" id="u-go">Print now (skip preview)</button>`);
  bindTrays("ut", chosen);
  $$("#ucol button").forEach((b) => b.onclick = () => { n = +b.dataset.v; $$("#ucol button").forEach((x) => x.classList.toggle("on", x === b)); $("#ut-wrap").innerHTML = trayPicker("ut", n, chosen); bindTrays("ut", chosen); });
  const go = (mode) => (ev) => act(ev.target, async () => {
    const fd = new FormData(); fd.append("file", f, f.name); fd.append("options", JSON.stringify({ trays: chosen.slice(0, n), mode }));
    const job = await api("/api/print/upload", { body: fd, method: "POST" });
    upsertJob(job);
    if (mode === "prepare" && !/\.gcode$/i.test(f.name)) return openStudio(job);
    closeSheet(); $('nav button[data-tab="jobs"]').click();
  }, mode === "prepare" ? "" : "Uploaded! Slicing on your PC…");
  $("#u-go").onclick = go("print"); $("#u-prep").onclick = go("prepare");
}

// ---------- jobs ----------
let JOBS = [];
function upsertJob(j) {
  const i = JOBS.findIndex((x) => x.id === j.id), prev = i >= 0 ? JOBS[i] : null;
  if (i >= 0) JOBS[i] = j; else JOBS.unshift(j);
  const mine = STU && STU.job.id === j.id;
  if (mine) studioSync(j);
  const moved = !prev || prev.stage !== j.stage || prev.msg !== j.msg;  // not for a star / note / "Printed ✓" update
  if (moved && j.stage === "failed") toast("Print job failed: " + j.msg, 6000);
  if (moved && j.stage === "printing") toast("Printing " + ((j.origin && j.origin.title) || j.filename));
  if (moved && j.stage === "review" && !mine) toast("Sliced: open it in Jobs to check the plates", 5000);
  if (tab === "jobs") renderJobs();
}
const STAGES = { queued: "Queued", downloading: "Downloading", analyzing: "Measuring", prepare: "Ready to prepare", splitting: "Cutting",
  slicing: "Slicing", review: "Ready to check", uploading: "Uploading", starting: "Starting", printing: "Started", ready: "On printer",
  failed: "Failed", cancelled: "Cancelled" };
// 11/14 b: after the print ends the card says how it went (server fills job.result from the printer's final state / its own list)
const PRINT_ACTIVE = ["PRINTING", "PAUSED", "PAUSING", "RESUMING", "HEATING", "PREPARING", "HOMING", "LEVELING", "FILAMENT SWAP"];
const RESULT_LABEL = { complete: ["Printed ✓", "done"], stopped: ["Stopped", "stopped"], error: ["Failed", "failed"] };
function jobLabel(j) {
  if (["printing", "ready"].includes(j.stage) && RESULT_LABEL[j.result]) {
    const [w, c] = RESULT_LABEL[j.result];
    return `<div class="small stage-${c}">${w}${j.ended ? " · " + new Date(j.ended * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}</div>${j.why ? `<div class="small why-line">⚠ ${esc(j.why)}</div>` : ""}`;
  }
  const pr = ST.printer || {}, now = j.stage === "printing" && j.remote && pr.filename === j.remote && PRINT_ACTIVE.includes(pr.state);
  if (now) return `<div class="small stage-printing">Printing now</div>`;
  return `<div class="small stage-${j.stage}">${STAGES[j.stage] || j.stage}${j.msg && j.msg !== STAGES[j.stage] ? " · " + esc(j.msg) : ""}</div>`;
}
let JOBS_STAR = store.get("jobs_star", false);  // the "★ Starred" filter, remembered on this device
const jobWords = (j) => [(j.origin && j.origin.title) || "", j.filename, j.order || "", j.note || "", j.msg || ""].join(" ").toLowerCase();
function jobsShown() {
  const words = $("#jq").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return JOBS.filter((j) => (!JOBS_STAR || j.star) && words.every((w) => jobWords(j).includes(w)));
}
function metaSheet(j) {  // order no. / customer + note on a job (groundwork for an order queue)
  openSheet(`<h3>${esc((j.origin && j.origin.title) || j.filename)}</h3>
    <label class="meta-l">Order / customer<input id="mt-order" maxlength="80" placeholder="e.g. #3141 · Jane" value="${esc(j.order || "")}"></label>
    <label class="meta-l">Note<textarea id="mt-note" maxlength="500" rows="4" placeholder="Colours, size, gift note, deadline…">${esc(j.note || "")}</textarea></label>
    <label class="row-between"><span>★ Starred<br><span class="muted small">Starred jobs are never cleared or trimmed from the list.</span></span><input type="checkbox" id="mt-star" ${j.star ? "checked" : ""}></label>
    <button class="btn primary wide" id="mt-save">Save</button>`);
  $("#mt-save").onclick = (e) => act(e.target, async () => {
    upsertJob(await api(`/api/jobs/${j.id}/meta`, { json: { order: $("#mt-order").value, note: $("#mt-note").value, star: $("#mt-star").checked } }));
    closeSheet();
  }, "Saved");
}
const canOpen =(j) => (j.info || (j.plates && j.plates.length)) && !["cancelled", "failed"].includes(j.stage);
function scaleNote(x) {  // " · 150%" when something wasn't printed at its normal size
  const s = x && x.scale != null ? +x.scale : 1;
  const other = (x && x.extras || []).some((e) => e.scale != null && +e.scale !== 1);
  return Math.abs(s - 1) > 0.001 ? ` · ${Math.round(s * 100)}%` : other ? " · mixed sizes" : "";
}
async function renderStats() {
  const q = $("#hq").value.trim(), seq = (renderStats.seq = (renderStats.seq || 0) + 1);
  let d; try { d = await api("/api/stats" + (q ? "?q=" + encodeURIComponent(q) : "")); } catch (e) { return; }
  if (seq !== renderStats.seq) return;  // an older answer arriving after a newer search
  const money = (v) => v == null ? "—" : "$" + v.toFixed(2);
  const box = (k, s) => `<div class="stat"><div class="muted small">${k}</div><b>${s.prints}</b> prints · ${s.success == null ? "—" : s.success + "% ok"}<br>
    ${s.hours} h · ${s.grams ? (s.grams >= 1000 ? (s.grams / 1000).toFixed(2) + " kg" : s.grams + " g") : "— g"} · ${money(s.cost)}</div>`;
  const eta = d.factor && d.samples >= 2 ? `<div class="muted small">${Math.abs(d.factor - 1) < 0.03 ? "Finish times: the printer's own estimates have been spot on so far."
    : `Finish times: prints here take ${Math.round(Math.abs(d.factor - 1) * 100)}% ${d.factor >= 1 ? "longer" : "less time"} than the printer predicts, so the app corrects for that.`}</div>`
    : `<div class="muted small">Finish times get more accurate after a couple of prints.</div>`;
  const res = { complete: "✓", stopped: "■", error: "⚠", other: "·" };
  const shown = q ? d.recent : d.recent.slice(0, 12);  // a search shows every match (up to 100)
  $("#stats").innerHTML = `<div class="stats">${box("This month", d.month)}${box("All time", d.all)}</div>${eta}
    ${d.all.counted < d.all.prints ? `<div class="muted small">Filament and cost only count prints sent from this app; set spool prices in Settings.</div>` : ""}
    ${!q && d.recent.some((x) => x.photo) ? `<div class="gallery">${d.recent.filter((x) => x.photo).slice(0, 12).map((x) => `<button data-row="${x.t}" title="${esc(x.file)}">
      <img loading="lazy" src="${SERVER}/api/photo/${x.photo}?token=${encodeURIComponent(TOKEN)}" alt=""><span class="r-${x.result}">${res[x.result] || ""}</span></button>`).join("")}</div>` : ""}`;
  $("#hist-n").textContent = q ? (d.matches ? `${d.matches} print${d.matches === 1 ? "" : "s"} match${d.matches === 1 ? "es" : ""}${d.matches > shown.length ? ` (newest ${shown.length} shown; the CSV has all)` : ""}` : "No prints match.") : "";
  $("#hist-csv").textContent = q && d.matches ? `⤓ CSV (${d.matches})` : "⤓ CSV";
  $("#hist-rows").innerHTML = shown.map((x) => `<div class="hrow tap" data-row="${x.t}"><span class="r-${x.result}">${res[x.result] || "·"}</span>
      <span class="grow">${esc(x.file.replace(/\.gcode$/, ""))}${x.order ? ` <span class="order-tag">${esc(x.order)}</span>` : ""}<br><span class="muted small">${new Date(x.t * 1000).toLocaleDateString([], { month: "short", day: "numeric" })} · ${fmtT(x.secs)}${x.cost != null ? " · " + money(x.cost) : ""}${scaleNote(x)}</span></span>
      ${x.tl ? `<button class="btn ghost small" data-tl="${esc(x.tl)}">▶ Timelapse</button>` : ""}</div>`).join("");
  $$("#stats [data-tl], #hist-rows [data-tl]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); playTl(b.dataset.tl); });
  $$("#stats [data-row], #hist-rows [data-row]").forEach((el) => el.onclick = () => printSheet(d.recent.find((x) => String(x.t) === el.dataset.row), money));
}
$("#hq").addEventListener("input", () => { clearTimeout(renderStats.t); renderStats.t = setTimeout(renderStats, 300); });
// downloads (CSV, backup): a 5-minute link-only token, so the 1-year sign-in never lands in a URL or the phone's browser history
async function dlFile(path, params = {}) {
  const { token } = await api("/api/dl_token", { method: "POST" });
  const u = new URL(SERVER + path);
  Object.entries({ ...params, token }).forEach(([k, v]) => u.searchParams.set(k, v));
  const a = document.createElement("a");
  a.href = u.toString(); a.rel = "noopener";
  if (isApp) a.target = "_blank"; else a.download = "";  // the APK hands it to the phone's browser, which saves it
  document.body.appendChild(a); a.click(); a.remove();
}
$("#hist-csv").onclick = (e) => act(e.target, () => dlFile("/api/history.csv", { tz: new Date().getTimezoneOffset(), ...($("#hq").value.trim() ? { q: $("#hq").value.trim() } : {}) }), "Downloading the history (CSV)");
$("#backup-dl").onclick = (e) => act(e.target, () => dlFile("/api/backup.zip"), "Downloading the backup");
function playTl(name) {
  const src = `${SERVER}/api/timelapse/${encodeURIComponent(name)}?token=${encodeURIComponent(TOKEN)}`;
  $("#tl-video").src = src; $("#tl-dl").href = src; $("#tl-sheet").classList.remove("hidden"); $("#tl-video").play().catch(() => {});
}
function printSheet(x, money) {  // one finished print: how it came out, and print it again
  if (!x) return;
  const name = x.file.replace(/\.gcode$/, ""), g = Object.values(x.grams || {}).reduce((a, b) => a + b, 0);
  const job = x.job && JOBS.find((j) => j.id === x.job);
  const plate = job && (job.plates || []).find((p) => p.sent === x.file);
  const word = { complete: "Finished", stopped: "Stopped", error: "Failed" }[x.result] || "Ended";
  openSheet(`<h3>${esc(name)}</h3>
    ${x.photo ? `<img class="bed-shot" src="${SERVER}/api/photo/${x.photo}?token=${encodeURIComponent(TOKEN)}" alt="">` : ""}
    <p class="muted small">${word} ${new Date(x.end * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} · took ${fmtT(x.secs)}${g ? ` · ${Math.round(g)} g` : ""}${x.cost != null ? " · " + money(x.cost) : ""}</p>
    ${x.why ? `<p class="small why-line">⚠ ${esc(x.why)}</p>` : ""}
    ${x.scale != null ? `<p class="small"><b>Printed at ${Math.round(x.scale * 100)}%</b>${(x.extras || []).map((e) => `<br>${esc(e.name)} at ${Math.round(e.scale * 100)}%${e.copies > 1 ? ` (×${e.copies})` : ""}`).join("")}</p>` : ""}
    <button class="btn primary wide" id="ps-again">Print it again</button>
    ${x.tl ? `<button class="btn wide" id="ps-tl">▶ Timelapse</button>` : ""}`);
  if ($("#ps-tl")) $("#ps-tl").onclick = () => { closeSheet(); playTl(x.tl); };
  $("#ps-again").onclick = (e) => {
    const st = ST.printer.state;
    if (st && !["IDLE", "COMPLETE", "STOPPED"].includes(st)) return toast("The printer is busy (" + (NICE[st] || st) + ")", 4000);
    if (!job) return printStored(x.file, 1);  // started on the printer's screen: pick the slot, start it from the printer's storage
    act(e.target, () => startChecked(async (force) => {
      upsertJob(await api(`/api/jobs/${job.id}/reprint`, { json: { force, plate: plate ? plate.n : undefined } })); closeSheet();
    }, "Starting the print"));
  };
}
$("#tl-x").onclick = () => { $("#tl-video").pause(); $("#tl-video").removeAttribute("src"); $("#tl-sheet").classList.add("hidden"); };
async function renderJobs() {
  if (!renderJobs.skipStats) renderStats();  // typing in the jobs search doesn't refetch the history
  renderJobs.skipStats = false;
  if (!renderJobs.loaded) { try { JOBS = await api("/api/jobs"); renderJobs.loaded = true; } catch (e) { toast(e.message); } }
  const shown = jobsShown();
  $("#jobs-star").textContent = JOBS_STAR ? `★ Starred (${shown.length})` : "☆ Starred";
  $("#jobs-star").classList.toggle("on", JOBS_STAR); $("#jobs-star").setAttribute("aria-pressed", String(JOBS_STAR));
  $("#jobs").innerHTML = shown.map((j) => `<div class="list-item ${canOpen(j) ? "tap" : ""}" data-id="${j.id}">${j.origin && j.origin.thumb ? `<img src="${img(j.origin.thumb)}">` : ""}
    <div class="grow"><div class="t">${j.calib || (j.origin && j.origin.source === "calib") ? "🎯 " : ""}${esc((j.origin && j.origin.title) || j.filename)}</div>
    ${j.order ? `<div class="small order-line">🧾 ${esc(j.order)}</div>` : ""}${j.note ? `<div class="muted small note-line">${esc(j.note)}</div>` : ""}
    ${jobLabel(j)}
    ${j.stats && j.stats.time ? `<div class="muted small">${esc(j.stats.time)} · ${j.stats.grams ? j.stats.grams.toFixed(1) + " g" : ""}${scaleNote(j.request || {})}${j.worker ? ` · ${j.worker === "cloud" ? "☁ sliced in the cloud" : "sliced on the PC"}` : ""}</div>` : ""}
    ${["cancelled", "failed"].includes(j.stage) ? "" : useChips(jobUse(j), j.stats && j.stats.grams ? "" : "Planned")}
    ${["printing", "ready", "failed", "prepare", "review", "cancelled"].includes(j.stage) ? "" : `<div class="bar"><i style="width:${j.stage === "slicing" ? 50 : j.pct || 5}%"></i></div>`}
    ${j.calib && j.calib.answered ? `<div class="small" style="color:var(--ok)">✓ Saved: ${esc(j.calib.answered)}</div>` : ""}
    ${j.feedback ? `<div class="small" style="color:var(--ok)">✓ Tuned: ${esc(j.feedback.join(", "))}</div>` : ""}</div>
    <div class="acts"><div class="mini"><button class="btn small ghost star ${j.star ? "on" : ""}" data-star="${j.id}" title="${j.star ? "Starred: kept for good (tap to unstar)" : "Star: keep for good"}" aria-label="${j.star ? "Unstar" : "Star"}">${j.star ? "★" : "☆"}</button><button class="btn small ghost" data-meta="${j.id}" title="Order / note" aria-label="Order and note">✎</button></div>${WAIT_STAGES.includes(j.stage) ?`<button class="btn small ghost" data-cancel="${j.id}">Cancel</button>` : ""}${canOpen(j) ? `<button class="btn small">${j.stage === "prepare" ? "Prepare" : j.plates ? "Plates" : "Open"}</button>` : ""}${j.remote && ["printing", "ready"].includes(j.stage) && !j.calib ? `<button class="btn small ghost again" data-again="${j.id}">Print again</button>` : ""}
    ${j.calib && !j.calib.answered && j.stage !== "failed" ? `<button class="btn small primary" data-act="answer">Answer</button>` : ""}
    ${!j.calib && !j.feedback && (j.presets || []).length && ["printing", "ready"].includes(j.stage) ? `<button class="btn small" data-act="fb">How did it go?</button>` : ""}
    ${REMOVABLE.includes(j.stage) && !j.star ? `<button class="btn small ghost rm" data-rm="${j.id}" title="Remove from the list" aria-label="Remove from the list">✕</button>` : ""}</div></div>`).join("")
    || `<div class="muted pad">${JOBS.length ? (JOBS_STAR && !JOBS.some((j) => j.star) ? "No starred jobs yet: tap ☆ on a job to keep it for good." : "No jobs match.") : "Nothing sent from the app yet."}</div>`;
  $$("#jobs [data-star]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const j = JOBS.find((x) => x.id === b.dataset.star);
    if (j) act(b, async () => upsertJob(await api(`/api/jobs/${j.id}/meta`, { json: { star: !j.star } })), j.star ? "Unstarred" : "Starred: it stays in the list for good");
  });
  $$("#jobs [data-meta]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); const j = JOBS.find((x) => x.id === b.dataset.meta); if (j) metaSheet(j); });
  $("#jobs-combine").classList.toggle("hidden", combinable().length < 2);
  const waiting = JOBS.filter((j) => WAIT_STAGES.includes(j.stage)).length;
  $("#jobs-clear").classList.toggle("hidden", waiting < 2);
  $("#jobs-clear").textContent = `Cancel all waiting (${waiting})`;
  const dead = JOBS.filter((j) => ["cancelled", "failed"].includes(j.stage) && !j.star).length;
  $("#jobs-tidy").classList.toggle("hidden", dead < 1);
  $("#jobs-tidy").textContent = `Clear cancelled & failed (${dead})`;
  $$("#jobs [data-rm]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const j = JOBS.find((x) => x.id === b.dataset.rm), name = (j && j.origin && j.origin.title) || (j && j.filename) || "This job";
    confirmSheet("Remove from the list?", `${name} leaves the Jobs list with its plates and previews. Your print history and photos stay.`, "Remove",
      () => act(null, async () => { await api(`/api/jobs/${b.dataset.rm}`, { method: "DELETE" }); JOBS = JOBS.filter((x) => x.id !== b.dataset.rm); renderJobs(); }, "Removed"));
  });
  $$("#jobs [data-cancel]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const j = JOBS.find((x) => x.id === b.dataset.cancel);
    confirmSheet("Cancel this job?", `${(j && j.origin && j.origin.title) || (j && j.filename) || "It"} won't be sliced or printed.`, "Cancel job",
      () => act(null, async () => upsertJob(await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" })), "Cancelled"));
  });
  $$("#jobs .list-item.tap").forEach((el) => el.onclick = () => { const j = JOBS.find((x) => x.id === el.dataset.id); if (j) openStudio(j); });
  $$("#jobs [data-act]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const j = JOBS.find((x) => x.id === b.closest(".list-item").dataset.id);
    if (j) (b.dataset.act === "answer" ? answerSheet : feedbackSheet)(j);
  });
  $$("#jobs [data-again]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const st = ST.printer.state;
    if (st && !["IDLE", "COMPLETE", "STOPPED"].includes(st)) return toast("The printer is busy (" + (NICE[st] || st) + ")", 4000);
    if (!b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "Tap again to start"; setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = "Print again"; } }, 4000); return; }
    act(b, () => startChecked(async (force) => upsertJob(await api(`/api/jobs/${b.dataset.again}/reprint`, { json: { force } })), "Starting the print"));
  });
}
$("#files-load").onclick = (e) => act(e.target, async () => {
  const r = await cmd("files");
  $("#files").innerHTML = (r.data || []).map((f) => `<div class="list-item"><div class="grow"><div class="t">${esc(f.filename)}</div>
    <div class="muted small">${f.print_time ? fmtT(f.print_time) + " · " : ""}${f.total_filament_used ? f.total_filament_used.toFixed(1) + " g · " : ""}${(f.color_map || []).length || 1} colour(s)</div></div>
    <button class="btn small" data-f="${esc(f.filename)}" data-n="${(f.color_map || []).length || 1}">Print</button></div>`).join("") || `<div class="muted">No files.</div>`;
  $$("#files button[data-f]").forEach((b) => b.onclick = () => printStored(b.dataset.f, +b.dataset.n));
});
$("#jq").addEventListener("input", () => { clearTimeout(renderJobs.qt); renderJobs.qt = setTimeout(() => { renderJobs.skipStats = true; renderJobs(); }, 200); });
$("#jobs-star").onclick = () => { JOBS_STAR = !JOBS_STAR; store.set("jobs_star", JOBS_STAR); renderJobs.skipStats = true; renderJobs(); };
$("#jobs-tidy").onclick = (e) => act(e.target, async () => {
  const r = await api("/api/jobs/clear", { method: "POST" }); renderJobs.loaded = false; await renderJobs();
  toast(`Removed ${r.removed} job${r.removed === 1 ? "" : "s"}`);
});
$("#jobs-clear").onclick = () => confirmSheet("Cancel everything waiting?", "Every job that's still waiting, downloading, cutting or slicing is cancelled. Prints already on the printer aren't touched.", "Cancel all waiting",
  () => act(null, async () => { const r = await api("/api/jobs/cancel_waiting", { method: "POST" }); renderJobs.loaded = false; await renderJobs(); toast(`Cancelled ${r.cancelled} job${r.cancelled === 1 ? "" : "s"}`); }));
function printStored(name, n) {
  const chosen = defaultTrays(4);
  openSheet(`<h3>${esc(name)}</h3><div class="card-h sub">Filament</div>${trayPicker("st", n, chosen)}<button class="btn primary wide" id="st-go">Print</button>`);
  bindTrays("st", chosen);
  $("#st-go").onclick = (e) => act(e.target, () => startChecked(async (force) => { await cmd("print_file", { filename: name, trays: chosen.slice(0, n), force }); closeSheet(); $('nav button[data-tab="printer"]').click(); }, "Starting print"));
}
$("#hist-load").onclick = (e) => act(e.target, async () => {
  const r = await cmd("history");
  $("#history").innerHTML = (r.data || []).slice(0, 40).map((h) => `<div class="list-item"><div class="grow"><div class="t">${esc(h.task_name)}</div>
    <div class="muted small">${h.task_status === 1 ? "✓ Completed" : h.task_status === 2 ? "✕ Cancelled" : "Status " + h.task_status} · ${new Date(h.begin_time * 1000).toLocaleString()}
    ${h.end_time ? " · " + fmtT(h.end_time - h.begin_time) : ""}</div></div></div>`).join("") || `<div class="muted">No history.</div>`;
});

// ---------- prepare & review studio (3D) ----------
// Prepare: the model on the bed; size, cuts (with filament pin holes), filament, quality -> "Slice" (on the PC).
// Review: each plate's real toolpaths with a layer slider -> print one plate / send them all.
let STU = null, viewerMod = null;
const WAIT_STAGES = ["queued", "downloading", "analyzing", "splitting", "slicing"];
const REMOVABLE = ["prepare", "review", "printing", "ready", "failed", "cancelled"];  // the server still refuses queued / printing-now jobs
const loadViewer = async () => viewerMod || (viewerMod = await import("./viewer.js?v=202611052319"));
async function apiBuf(path) {
  const r = await fetch(SERVER + path, { headers: { Authorization: "Bearer " + TOKEN } });
  if (!r.ok) throw new Error(r.status === 404 ? "Preview not ready yet" : "HTTP " + r.status);
  return r.arrayBuffer();
}
const mm = (v) => (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + "";
const fmtSize = (s) => `${mm(s[0])} × ${mm(s[1])} × ${mm(s[2])} mm`;

async function openStudio(job) {
  closeSheet();
  $("#studio").classList.remove("hidden");
  $("#st-title").textContent = (job.origin && job.origin.title) || job.filename;
  $("#st-panel").innerHTML = `<div class="muted">Loading 3D view…</div>`;
  let V;
  try { V = await loadViewer(); } catch (e) { $("#st-panel").innerHTML = `<div class="err">3D view failed to load: ${esc(e.message)}</div>`; return; }
  if (STU && STU.viewer) STU.viewer.dispose();
  const req = job.request || {};
  // the upload sheet guesses colours BEFORE the file is analysed (default 1): until the job is sliced, trust the scan
  const upN = (req.trays && req.trays.length) || 0, scanN = (job.info && job.info.colors) || 1;
  const useScan = !(job.plates && job.plates.length) && scanN > upN;
  STU = { V, viewer: new V.Viewer($("#st-view")), job, shown: "", mode: "", back: false, plate: 1,
    scale: req.scale || 1, cutMode: req.cuts == null ? "auto" : req.cuts.length ? "custom" : "none", cuts: (req.cuts || []).map((c) => ({ ...c })),
    pins: req.pins !== false, chosen: req.trays && req.trays.length && !useScan ? [...req.trays, ...defaultTrays(8)].slice(0, 8)
      : [...(nearestTrays(job.info && job.info.palette) || []), ...defaultTrays(8)].slice(0, 8),
    // a file can use more colours than the 4 slots (two colours then share a slot)
    n: Math.min(8, useScan ? scanN : upN || scanN), quality: req.quality || (SET && SET.quality) || "standard",
    variable: req.variable || "", style: req.style || guessStyle(job),
    supports: req.supports != null ? req.supports : (((job.info || {}).supports || {}).advice === "plate" ? "plate" : ""),
    keepColours: req.keep_colours !== false, vase_wall: req.vase_wall || "0.8", walls: req.walls || "3",
    purge: (req.purge && req.purge.design) || "", catch: (req.purge && req.purge.catch) || 1,
    copies: req.copies || 1, extras: (req.extras || []).map((x) => ({ ...x, chosen: x.trays ? [...x.trays] : null })), geos: {}, layPlate: 1,
    together: req.together || "layers",
    fplan: req.fplan ? req.fplan.map((p) => ({ ...p, objects: [...p.objects] })) : null, fsel: 0, fgeo: {} };
  if (!renderJobs.loaded) { try { JOBS = await api("/api/jobs"); renderJobs.loaded = true; } catch {} }
  studioSync(job);
}
// colour-change purge catchers (bridge/purge_shapes.py makes them, sized to each print's purge)
const PURGE = [["", "None", "", "Colour changes purge out of the chute as usual."]]
  .concat((window.PURGE_CATALOG || []).map((d) => [d.id, d.title, d.kind, d.about]));
let purgeKind = "";
// vases and boxes: the title says so, or the author's own settings are shell-only (0 % infill, no top)
function guessStyle(job) {
  const title = (job.origin && job.origin.title) || job.filename || "", au = (job.info && job.info.author) || {};
  if (/vase|spiral/i.test(title)) return "vase";
  if (String(au.sparse_infill_density) === "0%" && +au.top_shell_layers === 0) return +au.spiral_mode ? "vase" : "hollow";
  return "normal";
}
function closeStudio() {
  $("#studio").classList.add("hidden");
  if (STU && STU.viewer) STU.viewer.dispose();
  STU = null;
}
$("#st-x").onclick = closeStudio;

function studioSync(job) {
  const prev = STU.job;
  STU.job = job;
  $("#st-status").textContent = (STAGES[job.stage] || job.stage) + (job.msg ? " · " + job.msg : "");
  if (prev.stage !== job.stage && job.stage === "review") { STU.back = false; if (STU.shown.startsWith("plate")) STU.shown = ""; }
  const mode = WAIT_STAGES.includes(job.stage) ? "wait"
    : job.plates && job.plates.length && !STU.back ? "review" : job.info ? "prepare" : job.stage === "cancelled" || job.stage === "failed" ? "dead" : "wait";
  if (mode === "wait") renderWait();
  else if (mode !== STU.mode || mode === "review") (mode === "prepare" ? renderPrepare : mode === "review" ? renderReview : renderDead)();
  STU.mode = mode;
  if (mode === "prepare" || (mode === "wait" && job.info && !STU.shown)) showModel();
}

function renderWait() {
  const j = STU.job;
  $("#st-panel").innerHTML = `<div class="card-h">${esc(STAGES[j.stage] || j.stage)}…</div><div class="muted small">${esc(j.msg || "")}</div>
    <div class="bar"><i style="width:${j.stage === "slicing" || j.stage === "splitting" ? Math.max(8, j.pct || 0) : j.pct || 5}%"></i></div>
    <p class="muted small">Big models can take a few minutes; you can close this and come back from the Jobs tab.</p>
    <button class="btn ghost wide" id="st-cancel">Cancel this job</button>`;
  $("#st-cancel").onclick = () => confirmSheet("Cancel this job?", "It won't be sliced or printed.", "Cancel job",
    () => act(null, async () => { upsertJob(await api(`/api/jobs/${j.id}/cancel`, { method: "POST" })); closeStudio(); }, "Cancelled"));
}
function renderDead() {
  $("#st-panel").innerHTML = `<div class="err">${esc(STU.job.msg || "This job ended.")}</div><button class="btn wide" id="st-close2">Close</button>`;
  $("#st-close2").onclick = closeStudio;
}

async function showModel() {
  if (STU.shown === "model") return;
  STU.shown = "model";
  const s = STU;
  try {
    if (!s.geos[s.job.id]) {
      const buf = await apiBuf(`/api/jobs/${s.job.id}/asset/model.stl`);
      if (STU !== s) return;
      s.geos[s.job.id] = s.viewer.parseSTL(buf);
    }
    if (s.shown !== "model") return;
    s.forceRedraw = true;
    drawPrepare();
  } catch (e) { s.shown = ""; toast(e.message); }
  s.extras.forEach(loadExtraGeo);
}
async function loadExtraGeo(x) {
  const s = STU;
  if (s.geos[x.job] || s.geos[x.job] === null) return;
  s.geos[x.job] = null;  // loading
  try {
    const buf = await apiBuf(`/api/jobs/${x.job}/asset/model.stl`);
    if (STU !== s) return;
    s.geos[x.job] = s.viewer.parseSTL(buf);
    drawPrepare();
  } catch { delete s.geos[x.job]; }
}

// copies + other models on the same print: a rough layout (same max-rects packing as the PC's splitter.pack_positions)
const PART_HEX = [0x4fa3ff, 0xffa53d, 0x63d48a, 0xe86fd0, 0xf2dd4a, 0x8f7cff, 0x45d9d0, 0xff6f6f];
function slotHex(slot, fallback) {  // a Canvas slot's colour as a number for the 3D preview
  const t = (ST.printer.trays || []).find((x) => x.id === slot);
  const v = t && t.color ? parseInt(String(t.color).replace("#", "").slice(0, 6), 16) : NaN;
  return Number.isFinite(v) ? v : fallback;
}
function layoutUnits() {
  const out = [];
  const add = (geo, scale, size, cuts, copies, color, owner = 0) => {
    let parts;
    if (cuts.length) {
      const n = { x: 1, y: 1, z: 1 };
      cuts.forEach((c) => n[c.axis]++);
      const ps = [size[0] / n.x, size[1] / n.y, size[2] / n.z];
      parts = Array.from({ length: n.x * n.y * n.z }, () => ({ geo: null, scale, size: ps, color, owner }));
    } else parts = [{ geo, scale, size, color, owner }];
    for (let c = 0; c < copies; c++) parts.forEach((pt) => out.push({ ...pt }));
  };
  add(STU.geos[STU.job.id] || null, STU.scale, scaledSize(), effectiveCuts(), STU.copies, slotHex(STU.chosen[0], PART_HEX[0]));
  STU.extras.forEach((x, k) => {
    const j = JOBS.find((q) => q.id === x.job);
    if (!j || !j.info) return;
    const sz = j.info.size.map((v) => v * (x.scale || 1));
    add(STU.geos[x.job] || null, x.scale || 1, sz, fitsBed(sz) ? [] : planCuts(sz), x.copies, slotHex((x.chosen || [])[0], PART_HEX[(k + 1) % PART_HEX.length]), k + 1);
  });
  return out;
}
function packLayout(units, gap = 16) {  // max-rects, biggest first (the PC's splitter.pack_positions); fills gaps beside short parts
  const U = viewerMod.USABLE, W = U[0] + gap, H = U[1] + gap, plates = [];
  const order = units.map((_, i) => i).sort((a, b) => (units[b].size[0] * units[b].size[1] - units[a].size[0] * units[a].size[1]) || (units[b].size[1] - units[a].size[1]));
  const place = (pl, w, h) => {
    let best = null;
    for (const [fx, fy, fw, fh] of pl.free) if (w <= fw + 1e-6 && h <= fh + 1e-6 && (!best || fy < best[1] || (fy === best[1] && fx < best[0]))) best = [fx, fy];
    if (!best) return null;
    const [x, y] = best, nf = [];
    for (const f of pl.free) {
      const [fx, fy, fw, fh] = f;
      if (x >= fx + fw || x + w <= fx || y >= fy + fh || y + h <= fy) { nf.push(f); continue; }
      if (x > fx) nf.push([fx, fy, x - fx, fh]);
      if (x + w < fx + fw) nf.push([x + w, fy, fx + fw - x - w, fh]);
      if (y > fy) nf.push([fx, fy, fw, y - fy]);
      if (y + h < fy + fh) nf.push([fx, y + h, fw, fy + fh - y - h]);
    }
    pl.free = nf.filter((a, k) => !nf.some((b, m) => k !== m && b[0] <= a[0] && b[1] <= a[1] && a[0] + a[2] <= b[0] + b[2] && a[1] + a[3] <= b[1] + b[3]
      && (a.join() !== b.join() || m < k)));
    return best;
  };
  for (const i of order) {
    const w = units[i].size[0] + gap, h = units[i].size[1] + gap;
    let done = false;
    for (const pl of plates) { const xy = place(pl, w, h); if (xy) { pl.items.push({ i, x: xy[0], y: xy[1] }); done = true; break; } }
    if (!done) { const pl = { items: [], free: [[0, 0, W, H]] }; const xy = place(pl, w, h) || [0, 0]; pl.items.push({ i, x: xy[0], y: xy[1] }); plates.push(pl); }
  }
  return plates;
}
function blockLayout(units) {  // "One at a time": each model packed tight, the blocks in opposite corners, tallest last
  const U = viewerMod.USABLE, groups = new Map();
  units.forEach((u, i) => { if (!groups.has(u.owner)) groups.set(u.owner, []); groups.get(u.owner).push(i); });
  const blocks = [...groups.values()].map((idx) => {
    const sub = idx.map((i) => units[i]), pl = packLayout(sub, 6)[0];
    const w = Math.max(...pl.items.map((it) => it.x + sub[it.i].size[0])), d = Math.max(...pl.items.map((it) => it.y + sub[it.i].size[1]));
    return { items: pl.items.map((it) => ({ i: idx[it.i], x: it.x, y: it.y })), w, d, h: Math.max(...sub.map((u) => u.size[2] || 0)) };
  }).sort((a, b) => a.h - b.h);
  const spots = [(b) => [0, 0], (b) => [U[0] - b.w, U[1] - b.d], (b) => [U[0] - b.w, 0], (b) => [0, U[1] - b.d]];
  return [{ items: blocks.slice(0, 4).flatMap((b, k) => { const [ox, oy] = spots[k](b); return b.items.map((it) => ({ i: it.i, x: ox + it.x, y: oy + it.y })); }) }];
}
function drawPrepare() {
  const s = STU, v = s.viewer;
  if (s && s.shown === "model" && fpPlan()) return drawFilePlate();
  if (!s || s.shown !== "model" || !s.geos[s.job.id]) return;
  if (s.copies === 1 && !s.extras.length) {
    if (v.layout || !v.mesh || s.forceRedraw) v.setModel(s.geos[s.job.id], s.job.info.size);
    v.update(s.scale, effectiveCuts());
  } else {
    const units = layoutUnits(), plates = s.together === "blocks" && s.extras.length ? blockLayout(units) : packLayout(units);
    s.layPlate = Math.max(1, Math.min(s.layPlate || 1, plates.length));
    v.setLayout(plates[s.layPlate - 1].items.map((it) => ({ ...units[it.i], x: it.x, y: it.y })));
  }
  s.forceRedraw = false;
}
async function showPlate(n) {
  const key = "plate" + n;
  if (STU.shown === key) return;
  STU.shown = key;
  const s = STU;
  try {
    const buf = await apiBuf(`/api/jobs/${s.job.id}/asset/plate${n}.tp.gz`);
    if (STU !== s || s.shown !== key) return;
    const r = await s.viewer.setToolpaths(buf);
    s.layers = r.layers; s.hmin = r.hmin; s.hmax = r.hmax;
    const lh = $("#ly-h");
    if (lh) lh.textContent = r.hmax - r.hmin > 0.01 ? `Layer heights ${r.hmin.toFixed(2)}–${r.hmax.toFixed(2)} mm (variable)` : `Layer height ${r.hmax.toFixed(2)} mm`;
    const ly = $("#ly");
    s.layer = r.layers;
    if (ly) { ly.max = r.layers; ly.value = r.layers; ly.disabled = false; setLayer(r.layers); }
  } catch (e) { s.shown = ""; toast(e.message); }
}

// sizes / cuts (positions are mm from the scaled model's min corner, same as the PC's splitter)
const scaledSize = () => STU.job.info.size.map((v) => v * STU.scale);
const fitsBed = (s) => s.every((v, i) => v <= viewerMod.USABLE[i] + 1e-6);
function planCuts(s) {
  const cuts = [];
  [["z", 2], ["x", 0], ["y", 1]].forEach(([a, i]) => {
    const n = Math.max(0, Math.ceil(s[i] / viewerMod.USABLE[i] - 1e-9) - 1);
    for (let k = 1; k <= n; k++) cuts.push({ axis: a, at: Math.round(s[i] * k / (n + 1) * 100) / 100 });
  });
  return cuts;
}
// ---- a multi-plate 3MF's plates, editable: tap a plate to see it, remove parts or move them to another plate.
// STU.fplan = [{name, from (the file's plate n, 0 = new), objects: [ids]}]; sent with the slice only when changed.
function fpParts() {  // {id: {name, pos, size, n, k}} from the PC's prepare info
  if (STU.fparts) return STU.fparts;
  const out = {};
  (STU.job.info.file_plates || []).forEach((p) => (p.parts || []).forEach((pt, k) => { out[pt.id] = { ...pt, n: p.n, k }; }));
  return (STU.fparts = out);
}
const fpOriginal = () => filePlates().map((p) => ({ name: p.name || "", from: p.n, objects: (p.parts || []).map((x) => x.id) }));
function fpPlan() {  // null for jobs prepared before parts were sent (old behaviour: plate pictures only)
  const fp = filePlates();
  if (!fp || !fp.every((p) => p.parts && p.parts.length)) return null;
  if (!STU.fplan) STU.fplan = fpOriginal();
  STU.fsel = Math.max(0, Math.min(STU.fsel, STU.fplan.length - 1));
  return STU.fplan;
}
const fpChanged = () => JSON.stringify(STU.fplan) !== JSON.stringify(fpOriginal());
const fpRemoved = () => { const on = new Set(STU.fplan.flatMap((p) => p.objects)); return Object.keys(fpParts()).filter((id) => !on.has(id)); };
function drawFilePlate() {
  const s = STU, plan = fpPlan(), pl = plan[s.fsel], parts = fpParts();
  const ids = pl.objects.filter((id) => parts[id]);
  if (!ids.length) { s.viewer.setLayout([]); return; }
  ids.forEach(loadPartGeo);
  const asLaid = pl.from && ids.every((id) => parts[id].n === pl.from);  // only removals: keep the designer's layout
  const units = ids.map((id, i) => ({ geo: s.fgeo[id] || null, scale: 1, size: parts[id].size, color: PART_HEX[i % PART_HEX.length],
    x: parts[id].pos[0], y: parts[id].pos[1] }));
  if (!asLaid) packLayout(units).forEach((p, k) => p.items.forEach((it) => {  // rough: the slicer arranges it for real
    units[it.i].x = it.x + k * (viewerMod.USABLE[0] + 20); units[it.i].y = it.y; }));
  s.viewer.setLayout(units);
}
async function loadPartGeo(id) {
  const s = STU, pt = fpParts()[id];
  if (s.fgeo[id] !== undefined) return;
  s.fgeo[id] = null;  // loading
  try {
    const buf = await apiBuf(`/api/jobs/${s.job.id}/asset/fo${pt.n}_${pt.k}.stl`);
    if (STU !== s) return;
    s.fgeo[id] = s.viewer.parseSTL(buf);
    clearTimeout(loadPartGeo.t); loadPartGeo.t = setTimeout(drawPrepare, 60);  // one redraw per batch
  } catch { delete s.fgeo[id]; }
}
function renderFilePlates() {
  const plan = fpPlan(), parts = fpParts(), pl = plan[STU.fsel], removed = fpRemoved();
  const pic = (p) => p.from ? `<img loading="lazy" src="${SERVER}/api/jobs/${STU.job.id}/asset/fplate${p.from}.png?token=${encodeURIComponent(TOKEN)}" alt="">` : `<div class="fp-new">New</div>`;
  const moved = (p) => p.from && (p.objects.length !== (filePlates().find((x) => x.n === p.from).parts || []).length || p.objects.some((id) => parts[id].n !== p.from));
  $("#lay-wrap").innerHTML = `<div class="muted small" style="margin:6px 0">This file comes laid out on ${filePlates().length} plates by its designer.
      Tap a plate to see it in 3D, then remove parts you don't need or move them to another plate.
      <b>Hold a plate</b> to skip it or print only that one.</div>
    <div class="fplates">${plan.map((p, i) => `<figure data-i="${i}" class="${i === STU.fsel ? "on" : ""}">${pic(p)}<figcaption>${esc(p.name || "Plate " + (i + 1))} · ${p.objects.length} part${p.objects.length === 1 ? "" : "s"}${moved(p) ? " · changed" : ""}</figcaption></figure>`).join("")}</div>
    <div class="card-h sub">On ${esc(pl.name || "plate " + (STU.fsel + 1))}</div>
    <div class="fp-list">${pl.objects.map((id, i) => `<div class="fp-row" data-id="${esc(id)}"><i style="background:#${PART_HEX[i % PART_HEX.length].toString(16).padStart(6, "0")}"></i>
      <span>${esc(parts[id].name)}<br><span class="muted small">${parts[id].size.map((v) => Math.round(v)).join(" × ")} mm</span></span>
      <select aria-label="Move to"><option value="">Move…</option>${plan.map((p, k) => k === STU.fsel ? "" : `<option value="${k}">to ${esc(p.name || "plate " + (k + 1))}</option>`).join("")}<option value="new">to a new plate</option></select>
      <button class="btn small ghost fp-x" title="Remove">✕</button></div>`).join("") || `<div class="muted small">Nothing left on this plate; it's skipped.</div>`}</div>
    ${removed.length ? `<div class="card-h sub">Removed</div><div class="fp-list">${removed.map((id) => `<div class="fp-row" data-id="${esc(id)}"><span class="muted">${esc(parts[id].name)}</span><button class="btn small ghost fp-back">Put back</button></div>`).join("")}</div>` : ""}
    ${fpChanged() ? `<button class="btn small ghost" id="fp-reset">Undo my plate changes</button>` : ""}`;
  const redo = () => { STU.fplan = STU.fplan.filter((p, i) => p.objects.length || p.from || i === STU.fsel); fpPlan(); prepRefresh(); drawPrepare(); };
  $$("#lay-wrap .fplates figure").forEach((f) => {
    let t = null, held = false;
    const hold = () => { held = true; t = null; if (navigator.vibrate) navigator.vibrate(15); plateSheet(+f.dataset.i, redo); };
    f.addEventListener("pointerdown", () => { held = false; clearTimeout(t); t = setTimeout(hold, 500); });
    ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => f.addEventListener(ev, () => { clearTimeout(t); t = null; }));
    f.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!held) { clearTimeout(t); hold(); } });  // Android's own long-press
    f.onclick = () => { if (held) { held = false; return; } STU.fsel = +f.dataset.i; STU.viewer._laidOut = false; redo(); };  // re-frame the camera
  });
  $$("#lay-wrap .fp-row").forEach((row) => {
    const id = row.dataset.id, take = () => STU.fplan.forEach((p) => { p.objects = p.objects.filter((x) => x !== id); });
    const sel = $("select", row), x = $(".fp-x", row), back = $(".fp-back", row);
    if (sel) sel.onchange = () => {
      if (!sel.value) return;
      take();
      if (sel.value === "new") STU.fplan.push({ name: "", from: 0, objects: [id] });
      else STU.fplan[+sel.value].objects.push(id);
      redo();
    };
    if (x) x.onclick = () => { take(); redo(); };
    if (back) back.onclick = () => {  // home to the file's plate it came from (or a new one if that plate went away)
      const home = STU.fplan.find((p) => p.from === fpParts()[id].n);
      if (home) home.objects.push(id); else STU.fplan.push({ name: "", from: 0, objects: [id] });
      redo();
    };
  });
  if ($("#fp-reset")) $("#fp-reset").onclick = () => { STU.fplan = fpOriginal(); STU.fsel = 0; redo(); };
}
// long-press a plate tile: skip the whole plate, print only it, or put its parts back
function plateSheet(i, redo) {
  const plan = STU.fplan, p = plan[i], parts = fpParts(), name = p.name || "Plate " + (i + 1);
  const others = plan.filter((q, k) => k !== i && q.objects.length).length;
  const home = (id) => parts[id] && parts[id].n === p.from;
  const missing = p.from ? Object.keys(parts).filter((id) => home(id) && !p.objects.includes(id)) : [];
  const btn = (id, label, cls, off) => `<button class="btn wide ${cls}" id="${id}" style="margin-top:8px" ${off ? "disabled" : ""}>${esc(label)}</button>`;
  openSheet(`<h3>${esc(name)}</h3><p class="muted small">${p.objects.length} part${p.objects.length === 1 ? "" : "s"} on this plate.</p>
    ${p.objects.length ? btn("ps-skip", "Skip this plate (don't print it)", "danger", !others) : ""}
    ${btn("ps-only", "Print only this plate", "primary", !p.objects.length && !missing.length)}
    ${missing.length ? btn("ps-back", `Put its ${missing.length} part${missing.length === 1 ? "" : "s"} back`, "", false) : ""}
    ${!others && p.objects.length ? `<p class="muted small">It's the only plate left, so it can't be skipped.</p>` : ""}`);
  const done = () => { closeSheet(); STU.fsel = Math.min(i, STU.fplan.length - 1); STU.viewer._laidOut = false; redo(); };
  const putBack = () => missing.forEach((id) => { plan.forEach((q) => { q.objects = q.objects.filter((x) => x !== id); }); p.objects.push(id); });
  if ($("#ps-skip")) $("#ps-skip").onclick = () => { p.objects = []; done(); };
  if ($("#ps-only")) $("#ps-only").onclick = () => { putBack(); plan.forEach((q) => { if (q !== p) q.objects = []; }); done(); };
  if ($("#ps-back")) $("#ps-back").onclick = () => { putBack(); done(); };
}
// a 3MF laid out on several plates by its designer: printed plate by plate unless resized / copied / cut by hand
const filePlates = () => { const fp = (STU.job.info || {}).file_plates;
  return fp && fp.length > 1 && STU.scale === 1 && STU.copies === 1 && !STU.extras.length && STU.cutMode !== "custom" ? fp : null; };
function effectiveCuts() {
  const s = scaledSize();
  if (STU.cutMode === "none" || filePlates()) return [];
  if (STU.cutMode === "auto") return fitsBed(s) ? [] : planCuts(s);
  return STU.cuts.filter((c) => c.at > 0 && c.at < s[{ x: 0, y: 1, z: 2 }[c.axis]]);
}
function partCount(cuts) {
  const per = { x: 1, y: 1, z: 1 };
  cuts.forEach((c) => per[c.axis]++);
  return per.x * per.y * per.z;
}

function renderPrepare() {
  const j = STU.job, info = j.info || {};
  const multi = (info.colors || 1) > 1;
  $("#st-panel").innerHTML = `
    <div class="st-size"><b id="sz"></b><span class="badge" id="fitb"></span></div>
    ${j.error ? `<div class="err">${esc(j.msg)}</div>` : ""}
    <div class="card-h sub">Size</div>
    <div class="range-row"><input type="range" id="sc" min="10" max="400" step="1" value="${Math.round(STU.scale * 100)}"><span class="val" id="sc-v"></span></div>
    <div class="seg" id="sc-q">${[50, 100, 150, 200].map((v) => `<button data-v="${v}">${v}%</button>`).join("")}<button data-v="fit">Fit the bed</button></div>
    <div class="card-h sub">How many</div>
    <div class="row-between"><span>Copies of this model</span><div class="stepper" id="cp"><button data-d="-1">−</button><b id="cp-n">${STU.copies}</b><button data-d="1">+</button></div></div>
    <div id="extras"></div>
    <button class="btn ghost small" id="add-model">+ Add another model to this print</button>
    <div id="lay-wrap"></div>
    <div class="card-h sub">Split into parts</div>
    <div class="seg" id="cm"><button data-v="auto">Only if too big</button><button data-v="custom">My own cuts</button><button data-v="none">Never</button></div>
    <div id="cutbox"></div>
    <div class="row-between"><span>Pin holes for 1.75 mm filament<br><span class="muted small">2 mm holes, 6 mm deep on each side of a cut. Glue in an 11 mm piece of filament to line parts up.</span></span>
      <label class="switch"><input type="checkbox" id="pins" ${STU.pins ? "checked" : ""}><span></span></label></div>
    <div class="card-h sub">Filament</div>
    ${multi ? `<div class="row-between"><span>Colours</span><div class="seg" id="stn">${[1, 2, 3, 4, ...((STU.job.info && STU.job.info.colors) > 4 ? [STU.job.info.colors] : [])].map((k) => `<button data-v="${k}" class="${k === STU.n ? "on" : ""}">${k}</button>`).join("")}</div></div>` : ""}
    <div id="stt-wrap"></div>
    <div id="pg-wrap"><div class="card-h sub">Catch the colour-change purge</div>
      <p class="muted small">Instead of pooping out of the chute, the old colour gets printed into a keeper next to your model, sized to what this print purges. It comes out multicoloured.</p>
      <div class="chips" id="pg-k">${["", "Statue", "Functional", "Fidget"].map((k) => `<button data-k="${k}">${k || "All"}</button>`).join("")}</div>
      <div id="pg-list" class="pg-grid"></div>
      <div class="muted small" id="pg-about"></div>
      <div class="row-between"><span>How much to catch</span><div class="seg" id="pg-c">${[[0.5, "Half"], [0.75, "Most"], [1, "All"]].map(([c, t]) => `<button data-v="${c}">${t}</button>`).join("")}</div></div>
    </div>
    <div class="card-h sub">Inserts (magnets, nuts)</div>
    <div id="ins-box"></div>
    <div class="card-h sub">Print style</div>
    <div class="seg" id="ps"><button data-v="">Normal · solid</button><button data-v="hollow">Hollow · open top</button><button data-v="vase">Vase · spiral</button></div>
    <div id="ps-extra"></div>
    <div class="muted small" id="ps-note"></div>
    <div id="vl-wrap"><div class="card-h sub">Layer height</div>
    <div class="seg" id="sv"><button data-v="">Fixed</button><button data-v="fast">Variable · fast</button><button data-v="balanced">Variable · balanced</button><button data-v="fine">Variable · fine</button></div>
    <div class="muted small" id="sv-note"></div></div>
    <div class="card-h sub">Supports</div>
    <div class="seg" id="su">${[["", "Auto"], ["plate", "From the plate only"], ["everywhere", "Everywhere"], ["off", "Off"]].map(([v, t]) => `<button data-v="${v}">${t}</button>`).join("")}</div>
    <div class="muted small" id="su-note"></div>
    <div class="card-h sub">Quality</div>
    <div class="seg" id="sq">${[["draft", "Draft 0.24"], ["standard", "Standard 0.20"], ["fine", "Fine 0.12"], ["strong", "Strong"], ["showpiece", "Show piece"], ["jewel", "Tiny & crisp"]].map(([v, t]) => `<button data-v="${v}" class="${v === STU.quality ? "on" : ""}">${t}</button>`).join("")}</div>
    <div class="muted small" id="sq-note"></div>
    <p class="muted small">Your slicing settings (elephant foot, supports, …) from the Settings tab apply too.</p>
    <button class="btn primary wide" id="st-slice">Slice and preview</button>
    <button class="btn ghost wide" id="st-discard">Discard</button>`;
  const sc = $("#sc");
  sc.oninput = () => { STU.scale = +sc.value / 100; prepRefresh(); };
  $$("#sc-q button").forEach((b) => b.onclick = () => {
    if (b.dataset.v === "fit") { const s = j.info.size; STU.scale = Math.floor(Math.min(...s.map((v, i) => viewerMod.USABLE[i] / v)) * 99) / 100; }
    else STU.scale = +b.dataset.v / 100;
    sc.value = Math.round(STU.scale * 100); prepRefresh();
  });
  $$("#cm button").forEach((b) => b.onclick = () => {
    STU.cutMode = b.dataset.v;
    if (STU.cutMode === "custom" && !STU.cuts.length) STU.cuts = planCuts(scaledSize()).length ? planCuts(scaledSize()) : [{ axis: "z", at: Math.round(scaledSize()[2] / 2) }];
    renderCuts(); prepRefresh();
  });
  $("#pins").onchange = (e) => (STU.pins = e.target.checked);
  $$("#cp button").forEach((b) => b.onclick = () => { STU.copies = Math.max(1, Math.min(50, STU.copies + +b.dataset.d)); $("#cp-n").textContent = STU.copies; prepRefresh(); });
  $("#add-model").onclick = addModelSheet;
  renderExtras();
  if ($("#stn")) $$("#stn button").forEach((b) => b.onclick = () => { STU.n = +b.dataset.v; $$("#stn button").forEach((x) => x.classList.toggle("on", x === b)); renderTrays(); });
  const sqNote = () => { $("#sq-note").textContent = STU.quality === "jewel" ? "Earrings, charms and tiny multi-colour parts: 0.12 mm layers, slow gentle walls, each layer cools before the next, and a purge tower so every colour starts clean. Slow, but crisp." : ""; };
  $$("#sq button").forEach((b) => b.onclick = () => { STU.quality = b.dataset.v; $$("#sq button").forEach((x) => x.classList.toggle("on", x === b)); svNote(); sqNote(); });
  sqNote();
  const svNote = () => {
    $$("#sv button").forEach((x) => x.classList.toggle("on", x.dataset.v === STU.variable));
    $("#sv-note").textContent = ({ "": "Same layer height all the way up, set by Quality below.",
      fast: "Thin layers only on shallow slopes (0.12–0.32 mm). About the same time as normal, smoother curves.",
      balanced: "0.08–0.28 mm. Smooth curves and slopes; typically 30–60 % longer.",
      fine: "0.08–0.20 mm. Smoothest result, the longest print." })[STU.variable] +
      (STU.variable ? " Quality only sets the first layer and speeds; tree supports switch to the slim style." : "");
  };
  $$("#sv button").forEach((b) => b.onclick = () => { STU.variable = b.dataset.v; svNote(); });
  const suNote = () => {
    const sp = (STU.job.info && STU.job.info.supports) || {};
    $$("#su button").forEach((x) => x.classList.toggle("on", x.dataset.v === STU.supports));
    const why = sp.advice === "plate" ? `This model has narrow channels inside (about ${Math.round(sp.trapped_mm2)} mm² of overhang): supports grown there can't be pulled out. ` : "";
    $("#su-note").textContent = why + ({ "": "Your Settings tab supports, or the designer's choice in a 3MF.",
      plate: "Supports only grow up from the bed, never on the model itself, so nothing ends up inside holes or channels. Parts that hang over the model itself print without support.",
      everywhere: "Supports can start anywhere, including on the model. Best for figures; can fill holes and channels.",
      off: "No supports at all." })[STU.supports];
  };
  $$("#su button").forEach((b) => b.onclick = () => { STU.supports = b.dataset.v; suNote(); });
  suNote();
  svNote();
  const psNote = () => {
    const au = (STU.job.info && STU.job.info.author) || {}, st = STU.style;
    $$("#ps button").forEach((x) => x.classList.toggle("on", x.dataset.v === (st === "normal" ? "" : st)));
    $("#vl-wrap").style.display = st === "vase" ? "none" : "";
    if (st === "vase") STU.variable = "";
    $("#ps-extra").innerHTML = st === "vase" ? `<div class="row-between"><span>Wall thickness<br><span class="muted small">Thin vases are fragile and can leak. Thick is sturdy.</span></span>
        <div class="seg" id="vw">${[["0.42", "Thin"], ["0.6", "Medium"], ["0.8", "Thick"]].map(([v, t]) => `<button data-v="${v}" class="${v === STU.vase_wall ? "on" : ""}">${t} ${v}</button>`).join("")}</div></div>`
      : st === "hollow" ? `<div class="row-between"><span>Walls</span><div class="seg" id="hw">${["2", "3", "4"].map((v) => `<button data-v="${v}" class="${v === STU.walls ? "on" : ""}">${v}</button>`).join("")}</div></div>` : "";
    $$("#vw button").forEach((b) => b.onclick = () => { STU.vase_wall = b.dataset.v; psNote(); });
    $$("#hw button").forEach((b) => b.onclick = () => { STU.walls = b.dataset.v; psNote(); });
    const mine = au.sparse_infill_density ? ` The author's own settings: ${au.sparse_infill_density} infill, ${au.top_shell_layers ?? "?"} top layers, ${au.wall_loops ?? "?"} walls.` : "";
    $("#ps-note").textContent = ({ normal: "A normal solid print: walls, infill and a closed top.",
      hollow: "Walls only: no infill and no top, so a vase or box prints hollow with an open top. Several walls for strength." + mine,
      vase: "One continuous spiral wall, no seams and no infill: the fastest, lightest way to print a vase. The wall is as thick as you pick." + mine })[st];
    svNote();
  };
  $$("#ps button").forEach((b) => b.onclick = () => { STU.style = b.dataset.v || "normal"; psNote(); renderTrays(); });
  const renderInserts = () => {   // pause-for-insert: hidden pockets found in the model + extra heights typed in
    const pk = (STU.job.info && STU.job.info.pockets) || [];
    STU.ins = STU.ins || { auto: true, heights: "" };
    const list = pk.map((p) => `<li>${p.size[0]} × ${p.size[1]} × ${p.size[2]} mm pocket, roof at ${p.top} mm</li>`).join("");
    $("#ins-box").innerHTML = (pk.length
      ? `<label class="row-between"><span>Pause so I can drop them in<br><span class="muted small">Found ${pk.length} hidden pocket${pk.length > 1 ? "s" : ""}:</span></span>
           <input type="checkbox" id="ins-auto" ${STU.ins.auto ? "checked" : ""}></label><ul class="muted small">${list}</ul>`
      : `<div class="muted small">No hidden pockets found in this model.</div>`) +
      `<label class="row-between"><span>Also pause at (mm)<br><span class="muted small">e.g. 4.2, 9.6 · the print stops before that height is covered</span></span>
         <input type="text" id="ins-h" inputmode="decimal" placeholder="none" value="${STU.ins.heights}" style="max-width:9em"></label>
       <div class="muted small">When it stops you get a phone alert with a photo and a Resume button.</div>`;
    if ($("#ins-auto")) $("#ins-auto").onchange = (e) => (STU.ins.auto = e.target.checked);
    $("#ins-h").oninput = (e) => (STU.ins.heights = e.target.value);
  };
  renderInserts();
  psNote();
  $("#st-slice").onclick = (e) => act(e.target, async () => {
    const cuts = STU.cutMode === "auto" ? null : STU.cutMode === "none" ? [] : effectiveCuts();
    const multi = STU.copies > 1 || STU.extras.length;  // several models / copies keep their own colours now
    const changed = (cuts && cuts.length) || (cuts === null && !fitsBed(scaledSize())) || (!multi && STU.scale !== 1);
    const keeps = !!paintedJob() && STU.keepColours && !multi;
    const n = changed && !keeps ? 1 : STU.n;
    const extras = STU.extras.map((x) => ({ job: x.job, copies: x.copies, scale: x.scale || 1,
      trays: (x.chosen || extraDefault(x)).slice(0, extraColours(x)) }));
    const job = await api(`/api/jobs/${j.id}/slice`, { json: { scale: STU.scale, cuts, pins: STU.pins, trays: STU.chosen.slice(0, n),
      quality: STU.quality, copies: STU.copies, extras, variable: STU.variable, together: STU.extras.length ? STU.together : null,
      style: STU.style === "normal" ? null : STU.style, vase_wall: STU.vase_wall, walls: STU.walls, supports: STU.supports, keep_colours: STU.keepColours,
      inserts: { auto: !STU.ins || STU.ins.auto, heights: ((STU.ins && STU.ins.heights) || "").split(/[,\s;]+/).map(parseFloat).filter((h) => h > 0) },
      purge: purgeOK() && STU.purge ? { design: STU.purge, catch: STU.catch } : null,
      fplan: fpPlan() && fpChanged() ? STU.fplan.filter((p) => p.objects.length) : null } });
    STU.back = false; studioSync(job);
  });
  $("#st-discard").onclick = () => confirmSheet("Discard this model?", "It's removed from your jobs and the PC.", "Discard", () =>
    act(null, async () => { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); closeStudio(); renderJobs.loaded = false; renderJobs(); }));
  const drawPurge = () => {
    $$("#pg-k button").forEach((b) => b.classList.toggle("on", b.dataset.k === purgeKind));
    $("#pg-list").innerHTML = PURGE.filter(([k, , kind]) => !k || !purgeKind || kind === purgeKind).map(([k, t, kind, about]) =>
      `<button class="pg" data-k="${k}" title="${esc(about)}">${k ? `<img src="img/purge/${k}.png" alt="">` : `<span class="pg-none">∅</span>`}<b>${esc(t)}</b><span class="muted small">${esc(kind || "chute")}</span></button>`).join("");
    $$("#pg-list .pg").forEach((b) => b.onclick = () => { STU.purge = b.dataset.k; purgeVis(); });
    purgeVis();
  };
  $$("#pg-k button").forEach((b) => b.onclick = () => { purgeKind = b.dataset.k; drawPurge(); });
  drawPurge();
  $$("#pg-c button").forEach((b) => b.onclick = () => { STU.catch = +b.dataset.v; purgeVis(); });
  renderCuts(); renderTrays(); prepRefresh();
}
function purgeOK() {  // only a whole, unscaled multi-colour model keeps its colours (and so has purges)
  return STU.n > 1 && (STU.job.info && STU.job.info.colors > 1) && STU.scale === 1 && !effectiveCuts().length && !STU.extras.length && STU.copies === 1;
}
function purgeVis() {
  const w = $("#pg-wrap");
  if (!w) return;
  w.style.display = purgeOK() ? "" : "none";
  $$("#pg-list .pg").forEach((b) => b.classList.toggle("on", b.dataset.k === STU.purge));
  const sel = PURGE.find(([k]) => k === STU.purge);
  if ($("#pg-about")) $("#pg-about").textContent = sel ? sel[3] : "";
  $$("#pg-c button").forEach((b) => b.classList.toggle("on", +b.dataset.v === STU.catch));
}
// painted / multi-colour 3MF (jobs prepared before the "painted" flag: any multi-colour 3MF counts)
const paintedJob = () => { const i = (STU.job && STU.job.info) || {}; return !!(i.painted || ((i.colors || 1) > 1 && /\.3mf$/i.test(STU.job.name || STU.job.filename || ""))); };
function renderTrays() {
  const multi = STU.copies > 1 || STU.extras.length;
  const s = scaledSize(), cut0 = effectiveCuts().length || (!multi && STU.scale !== 1);
  const keeps = !!paintedJob() && STU.keepColours && !multi;
  const cut = cut0 && !keeps;
  const n = cut ? 1 : STU.n;
  const pal = !cut && STU.job.info && STU.job.info.palette;
  const shared = !cut && n > 1 && new Set(STU.chosen.slice(0, n)).size < n;
  const kc = paintedJob() && !multi
    ? `<label class="row-between"><span>Keep colours when resized or cut<br><span class="muted small">On: every part keeps its painted colours. Off: one colour, quicker to slice.</span></span><input type="checkbox" id="kc" ${STU.keepColours ? "checked" : ""}></label>` : "";
  $("#stt-wrap").innerHTML = kc + trayPicker("stt", n, STU.chosen, pal) + (cut && STU.n > 1 ? `<div class="muted small">${keeps || !paintedJob() ? "Scaled or cut models print in one colour." : "Keep colours is off, so it prints in one colour."}</div>`
    : n > 4 ? `<div class="muted small">This model uses ${n} colours and the Canvas has 4 slots, so some colours share a slot (picked by the closest colour; change any of them).</div>`
    : shared ? `<div class="muted small">Some colours share a slot, so they'll print in the same filament.</div>` : "");
  bindTrays("stt", STU.chosen);
  if ($("#kc")) $("#kc").onchange = (e) => { STU.keepColours = e.target.checked; renderTrays(); };
  $$("#stt .map button").forEach((b) => b.addEventListener("click", purgeVis));
  $$("#stt .map button").forEach((b) => b.addEventListener("click", () => { if (STU.extras.length || STU.copies > 1) { STU.forceRedraw = true; drawPrepare(); } }));
  purgeVis();
  void s;
}
function renderCuts() {
  $$("#cm button").forEach((b) => b.classList.toggle("on", b.dataset.v === STU.cutMode));
  const box = $("#cutbox");
  if (STU.cutMode !== "custom") { box.innerHTML = ""; return; }
  const s = scaledSize();
  box.innerHTML = STU.cuts.map((c, i) => {
    const max = s[{ x: 0, y: 1, z: 2 }[c.axis]];
    return `<div class="cut-row" data-i="${i}"><select>${["z", "x", "y"].map((a) => `<option value="${a}" ${a === c.axis ? "selected" : ""}>${a.toUpperCase()}</option>`).join("")}</select>
      <input type="range" min="1" max="${Math.max(2, Math.floor(max) - 1)}" step="0.5" value="${c.at}"><span class="val small">${mm(c.at)} mm</span><button class="btn small">✕</button></div>`;
  }).join("") + `<button class="btn ghost small" id="cut-add">+ Add a cut</button>
    <div class="muted small">Z cuts are flat layers (best). X/Y cuts slice it side to side; those parts are laid cut-face down.</div>`;
  $$("#cutbox .cut-row").forEach((row) => {
    const c = STU.cuts[+row.dataset.i];
    $("select", row).onchange = (e) => { c.axis = e.target.value; c.at = Math.round(scaledSize()[{ x: 0, y: 1, z: 2 }[c.axis]] / 2); renderCuts(); prepRefresh(); };
    $("input", row).oninput = (e) => { c.at = +e.target.value; $(".val", row).textContent = mm(c.at) + " mm"; prepRefresh(); };
    $("button", row).onclick = () => { STU.cuts.splice(+row.dataset.i, 1); renderCuts(); prepRefresh(); };
  });
  $("#cut-add").onclick = () => { STU.cuts.push({ axis: "z", at: Math.round(scaledSize()[2] / 2) }); renderCuts(); prepRefresh(); };
}
function prepRefresh() {
  const s = scaledSize(), cuts = effectiveCuts(), fits = fitsBed(s);
  $("#sz").textContent = fmtSize(s);
  $("#sc-v").textContent = Math.round(STU.scale * 100) + "%";
  const fb = $("#fitb");
  const multi = STU.copies > 1 || STU.extras.length;
  const units = multi ? layoutUnits() : null, plates = multi ? packLayout(units) : null;
  if (multi) {
    fb.textContent = `${units.length} pieces · ${plates.length > 1 ? "about " : ""}${plates.length} plate${plates.length > 1 ? "s" : ""}`; fb.className = "badge " + (plates.length > 1 ? "warn" : "ok");
    STU.layPlate = Math.max(1, Math.min(STU.layPlate || 1, plates.length));
    $("#lay-wrap").innerHTML = `<div class="muted small" style="margin:6px 0">${plates.length > 1
      ? `Won't all fit on one plate: the rest goes on the next plate(s). Showing plate:` : "Looks like it all fits on one plate."} Rough layout: your PC checks exactly what fits when slicing and moves anything extra to another plate.</div>
      ${plates.length > 1 ? `<div class="seg" id="lay-pl">${plates.map((p, i) => `<button data-v="${i + 1}" class="${i + 1 === STU.layPlate ? "on" : ""}">${i + 1} (${p.items.length})</button>`).join("")}</div>` : ""}`;
    $$("#lay-pl button").forEach((b) => b.onclick = () => { STU.layPlate = +b.dataset.v; prepRefresh(); });
  } else if (fpPlan()) {
    renderFilePlates();
  } else if (filePlates()) {
    const fp = filePlates();
    $("#lay-wrap").innerHTML = `<div class="muted small" style="margin:6px 0">This file comes laid out on ${fp.length} plates by its designer. Each prints as laid out, one after another (the 3D view shows plate ${esc(fp[0].name || "1")}).</div>
      <div class="fplates">${fp.map((p) => `<figure><img loading="lazy" src="${SERVER}/api/jobs/${STU.job.id}/asset/fplate${p.n}.png?token=${encodeURIComponent(TOKEN)}" alt=""><figcaption>${esc(p.name || "Plate " + p.n)} · ${p.objects} part${p.objects > 1 ? "s" : ""}</figcaption></figure>`).join("")}</div>`;
  } else $("#lay-wrap").innerHTML = "";
  if (multi) { /* badge set above */ }
  else if (fpPlan()) { const n = fpPlan().filter((p) => p.objects.length).length; fb.textContent = `${n} plate${n === 1 ? "" : "s"}${fpChanged() ? " (changed)" : " from the file"}`; fb.className = "badge ok"; }
  else if (filePlates()) { fb.textContent = `${filePlates().length} plates from the file`; fb.className = "badge ok"; }
  else if (cuts.length) { fb.textContent = `${partCount(cuts)} parts`; fb.className = "badge warn"; }
  else if (fits) { fb.textContent = "Fits the bed"; fb.className = "badge ok"; }
  else { fb.textContent = "Too big for the bed"; fb.className = "badge bad"; }
  $$("#sc-q button").forEach((b) => b.classList.toggle("on", b.dataset.v !== "fit" && +b.dataset.v === Math.round(STU.scale * 100)));
  if (STU.cutMode === "custom") $$("#cutbox .cut-row").forEach((row) => { // keep slider ranges in step with the size
    const c = STU.cuts[+row.dataset.i], r = $("input", row); r.max = Math.max(2, Math.floor(s[{ x: 0, y: 1, z: 2 }[c.axis]]) - 1);
  });
  drawPrepare();
  renderTrays();
}
function renderExtras() {
  const box = $("#extras");
  if (!box) return;
  box.innerHTML = STU.extras.map((x, i) => {
    const j = JOBS.find((q) => q.id === x.job) || { filename: "Model", info: { size: [0, 0, 0] } };
    if (!x.chosen) x.chosen = extraDefault(x);
    return `<div class="extra" data-i="${i}">${j.origin && j.origin.thumb ? `<img src="${img(j.origin.thumb)}">` : ""}
      <div class="grow"><div class="t">${esc((j.origin && j.origin.title) || j.filename)}</div><div class="muted small">${fmtSize(j.info.size.map((v) => v * (x.scale || 1)))}</div></div>
      <div class="stepper"><button data-d="-1">−</button><b>${x.copies}</b><button data-d="1">+</button></div><button class="btn small" data-rm="1">✕</button></div>
      <div class="extra-col">${trayPicker("ext" + i, extraColours(x), x.chosen, j.info && j.info.palette)}</div>`;
  }).join("") + (STU.extras.length ? `<div class="row-between" style="margin-top:8px"><span class="small">Print them</span>
      <div class="seg" id="tog"><button data-v="layers" class="${STU.together !== "blocks" ? "on" : ""}">All together</button>
      <button data-v="blocks" class="${STU.together === "blocks" ? "on" : ""}">One at a time</button></div></div>
      <div class="muted small" id="tog-n">${STU.together === "blocks"
        ? "Each model is printed start to finish before the next one, so its colour only changes once per model. Models are kept apart in the corners for the print head; all but the tallest must be under 36 mm."
        : "Everything prints together layer by layer; each layer changes colour once for all the models (a small prime puck keeps the colours clean). Best when the models have several colours."}</div>` : "");
  $$("#tog button").forEach((b) => b.onclick = () => { STU.together = b.dataset.v; renderExtras(); drawPrepare(); });
  STU.extras.forEach((x, i) => bindTrays("ext" + i, x.chosen));
  $$("#extras .map button").forEach((b) => b.addEventListener("click", () => { STU.forceRedraw = true; drawPrepare(); }));
  $$("#extras .extra").forEach((row) => {
    const x = STU.extras[+row.dataset.i];
    $$(".stepper button", row).forEach((b) => b.onclick = () => { x.copies = Math.max(1, Math.min(50, x.copies + +b.dataset.d)); $(".stepper b", row).textContent = x.copies; prepRefresh(); });
    $("[data-rm]", row).onclick = () => { STU.extras.splice(+row.dataset.i, 1); renderExtras(); prepRefresh(); };
  });
}
function extraColours(x) {  // how many colours an added model has (its own colour layout is kept)
  const j = JOBS.find((q) => q.id === x.job);
  return Math.max(1, Math.min(8, (j && j.info && j.info.colors) || 1));
}
function extraDefault(x) {  // a one-colour model gets a different slot from the main model's, so they're told apart
  const n = extraColours(x), d = defaultTrays(8), j = JOBS.find((q) => q.id === x.job);
  if (n > 1) return [...(nearestTrays(j && j.info && j.info.palette) || []), ...d].slice(0, n);
  const free = d.find((t) => !STU.chosen.slice(0, STU.n).includes(t));
  return [free ?? d[0]];
}
function combinable() {  // jobs that can go on a shared plate: measured, not cancelled / failed / being worked on
  return JOBS.filter((j) => j.info && !["cancelled", "failed"].includes(j.stage) && !WAIT_STAGES.includes(j.stage) && !j.calib);
}
$("#jobs-combine").onclick = () => {
  const c = combinable();
  openSheet(`<h3>Put several on one plate</h3>
    <p class="muted small">Tick the models to print together. Each keeps its own colours; next you pick the slots and whether they print all together or one at a time.</p>
    ${c.map((j) => `<label class="pick-model"><input type="checkbox" data-id="${j.id}">${j.origin && j.origin.thumb ? `<img src="${img(j.origin.thumb)}">` : ""}
      <span class="grow"><b>${esc((j.origin && j.origin.title) || j.filename)}</b><br><span class="muted small">${fmtSize(j.info.size)}${j.info.colors > 1 ? ` · ${j.info.colors} colours` : ""}</span></span></label>`).join("")}
    <button class="btn primary wide" id="cmb-go" disabled>Lay them out</button>`);
  const boxes = $$("#sheet-c input[type=checkbox]");
  const sync = () => { const n = boxes.filter((b) => b.checked).length; $("#cmb-go").disabled = n < 2; $("#cmb-go").textContent = n >= 2 ? `Lay out ${n} models` : "Lay them out"; };
  boxes.forEach((b) => b.onchange = sync);
  $("#cmb-go").onclick = () => openCombined(boxes.filter((b) => b.checked).map((b) => b.dataset.id));
};
async function openCombined(ids) {  // the first job's prepare screen with the others added to the same plate
  const first = JOBS.find((j) => j.id === ids[0]);
  await openStudio(first);
  STU.back = true;  // the prepare screen, even if it was already sliced on its own
  STU.extras = ids.slice(1).map((id) => ({ job: id, copies: 1, scale: 1, chosen: null }));
  studioSync(STU.job);
  STU.extras.forEach(loadExtraGeo);
  renderExtras(); renderTrays(); prepRefresh();
}
function addModelSheet() {
  const cands = JOBS.filter((j) => j.id !== STU.job.id && j.info && !["cancelled", "failed"].includes(j.stage) && !STU.extras.find((x) => x.job === j.id));
  openSheet(`<h3>Add a model to this print</h3>
    ${cands.length ? `<p class="muted small">It's placed on the same plate when there's room, otherwise on the next one.</p>` + cands.map((j) => `<button class="pick-model" data-id="${j.id}">${j.origin && j.origin.thumb ? `<img src="${img(j.origin.thumb)}">` : ""}
      <span class="grow"><b>${esc((j.origin && j.origin.title) || j.filename)}</b><br><span class="muted small">${fmtSize(j.info.size)}</span></span></button>`).join("")
      : `<p class="muted">No other prepared models yet. In Models, open one and tap <b>Prepare &amp; preview</b>; it then shows up here.</p>`}`);
  $$(".pick-model").forEach((b) => b.onclick = () => {
    const x = { job: b.dataset.id, copies: 1, scale: 1 };
    STU.extras.push(x); closeSheet(); renderExtras(); loadExtraGeo(x); prepRefresh();
  });
}

function spoolWarn(j, st) {  // a slot that doesn't have enough filament left for this plate
  const each = st.grams_each || (st.grams ? [st.grams] : []), need = {};
  (j.trays || []).forEach((t, i) => { if (each[i]) need[t] = (need[t] || 0) + each[i]; });
  const short = Object.entries(need).filter(([t, g]) => spoolLeft(t) != null && spoolLeft(t) < g * 1.05);
  return short.length ? `<div class="err">${short.map(([t, g]) => `Slot ${+t + 1} has about ${Math.round(spoolLeft(t))} g left; this plate needs ${Math.round(g)} g.`).join(" ")} Swap in a fuller spool or turn on auto refill.</div>` : "";
}
function plateCost(j, st) {  // from the $/kg prices in Settings, per Canvas slot the plate uses
  const prices = (SET && SET.prices) || {}, each = st.grams_each || (st.grams ? [st.grams] : []);
  const slots = j.trays && j.trays.length ? j.trays : [((ST.printer.trays || []).find((t) => t.active) || {}).id ?? 0];
  if (!each.length || !slots.slice(0, each.length).every((t) => prices[t] != null)) return null;
  return each.reduce((a, g, i) => a + g / 1000 * prices[slots[i] ?? slots[0]], 0);
}
function renderReview() {
  const j = STU.job, plates = j.plates || [];
  const p = plates.find((x) => x.n === STU.plate) || plates[0];
  STU.plate = p.n;
  const busy = ["queued", "uploading", "starting"].includes(j.stage);
  const st = p.stats || {};
  $("#st-panel").innerHTML = `
    ${plates.length > 1 ? `<div class="seg" id="pl">${plates.map((x) => `<button data-v="${x.n}" class="${x.n === p.n ? "on" : ""}">Plate ${esc(x.name || String(x.n))}${x.sent ? " ✓" : ""}</button>`).join("")}</div>` : ""}
    <div class="stats3"><div><b>${esc(st.time || "—")}</b><span class="muted small">print time</span></div>
      <div><b>${st.grams ? st.grams.toFixed(1) + " g" : "—"}</b><span class="muted small">filament${plateCost(j, st) != null ? " · $" + plateCost(j, st).toFixed(2) : ""}</span></div>
      <div><b>${p.parts}</b><span class="muted small">part${p.parts > 1 ? "s" : ""}</span></div></div>
    <div class="range-row"><span class="small muted">Layer</span><input type="range" id="ly" min="1" max="${STU.layers || 1}" value="${STU.layers || 1}" ${STU.layers ? "" : "disabled"}><span class="val small" id="ly-v"></span></div>
    <div class="muted small" id="ly-h">${STU.hmax ? (STU.hmax - STU.hmin > 0.01 ? `Layer heights ${STU.hmin.toFixed(2)}–${STU.hmax.toFixed(2)} mm (variable)` : `Layer height ${STU.hmax.toFixed(2)} mm`) : ""}</div>
    <div class="legend">${viewerMod.LEGEND.map(([t, c]) => `<span><i style="background:${c}"></i>${t}</span>`).join("")}</div>
    ${(j.notes || []).length ? `<ul class="notes">${j.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${useChips(jobUse(j, st), "Uses")}
    ${spoolWarn(j, st)}
    ${j.error ? `<div class="err">${esc(j.msg)}</div>` : ""}
    ${p.sent ? `<div class="muted small">On the printer as “${esc(p.sent)}”.</div>` : ""}
    <button class="btn primary wide" id="rv-print" ${busy ? "disabled" : ""}>Print ${plates.length > 1 ? "plate " + esc(p.name || String(p.n)) : ""} now</button>
    ${plates.length > 1 ? `<button class="btn wide" id="rv-all" ${busy ? "disabled" : ""}>Send all ${plates.length} plates (don't start)</button>`
      : `<button class="btn wide" id="rv-send" ${busy ? "disabled" : ""}>Send to printer without starting</button>`}
    <button class="btn ghost wide" id="rv-q">Add ${plates.length > 1 ? "plate " + esc(p.name || String(p.n)) + " " : ""}to the queue</button>
    ${plates.length > 1 ? `<button class="btn ghost wide" id="rv-qall">Queue all ${plates.length} plates</button>` : ""}
    ${plates.length > 1 ? `<button class="btn ghost wide" id="rv-del" ${busy ? "disabled" : ""}>Remove plate ${esc(p.name || String(p.n))} (don't print it)</button>` : ""}
    <button class="btn ghost wide" id="rv-back">Change size / cuts</button>
    <button class="btn ghost wide" id="rv-discard">Discard this model</button>`;
  if (STU.layers && STU.shown === "plate" + p.n) { $("#ly").value = STU.layer || STU.layers; setLayer(STU.layer || STU.layers); }
  $("#ly").oninput = (e) => setLayer(+e.target.value);
  if ($("#pl")) $$("#pl button").forEach((b) => b.onclick = () => { STU.plate = +b.dataset.v; STU.layers = 0; renderReview(); });
  const send = (body, ok) => (e) => act(e.target, async () => { studioSync(await api(`/api/jobs/${j.id}/send`, { json: body })); }, ok);
  $("#rv-print").onclick = (e) => {
    const s = ST.printer.state;
    if (s && !["IDLE", "COMPLETE", "STOPPED"].includes(s)) return toast("The printer is busy (" + (NICE[s] || s) + ")", 4000);
    act(e.target, () => startChecked(async (force) => studioSync(await api(`/api/jobs/${j.id}/send`, { json: { plate: p.n, start: true, force } })), "Sending and starting…"));
  };
  if ($("#rv-all")) $("#rv-all").onclick = send({ plate: null }, "Sending all plates…");
  if ($("#rv-send")) $("#rv-send").onclick = send({ plate: p.n, start: false }, "Sending to the printer…");
  $("#rv-back").onclick = () => { STU.back = true; STU.mode = ""; studioSync(STU.job); };
  if ($("#rv-del")) $("#rv-del").onclick = () => confirmSheet(`Remove plate ${p.name || p.n}?`,
    `It won't be sent, queued or offered as the next plate. The other ${plates.length - 1} stay. To get it back, slice the model again.`, "Remove plate",
    () => act(null, async () => { const nj = await api(`/api/jobs/${j.id}/plates/${p.n}`, { method: "DELETE" }); STU.plate = nj.plates[0].n; STU.layers = 0; STU.mode = ""; upsertJob(nj); api("/api/queue").then(renderQueue).catch(() => {}); }, "Plate removed"));
  $("#rv-discard").onclick = () => confirmSheet("Discard this model?", "All its plates are removed from your jobs and the queue. Anything already printing keeps going.", "Discard",
    () => act(null, async () => { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); closeStudio(); renderJobs.loaded = false; renderJobs(); }));
  $("#rv-q").onclick = (e) => act(e.target, async () => renderQueue(await api("/api/queue", { json: { job: j.id, plate: p.n } })), "Added to the queue");
  if ($("#rv-qall")) $("#rv-qall").onclick = (e) => act(e.target, async () => renderQueue(await api("/api/queue", { json: { job: j.id } })), "All plates queued");
  showPlate(p.n);
}
function setLayer(l) {
  STU.layer = l;
  const z = STU.viewer.setLayer(l);
  const v = $("#ly-v");
  if (v) v.textContent = `${l} / ${STU.layers} · ${z != null ? z.toFixed(2) : "?"} mm`;
}


// ---------- calibration ----------
let CAL = null, calSlot = null;
const CAL_ROWS = [["nozzle_temperature", "Nozzle temperature", " °C"], ["filament_flow_ratio", "Flow ratio", ""],
  ["pressure_advance", "Pressure advance", ""], ["filament_retraction_length", "Retraction", " mm"],
  ["elefant_foot_compensation", "Elephant foot", " mm"], ["xy_contour_compensation", "XY size", " mm"]];
const shortPreset = (p) => String(p || "").replace(/ @ECC2$/, "").replace(/^Elegoo /, "");
async function loadCalib() {
  try { CAL = await api("/api/calib"); } catch (e) { $("#cal-vals").innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  renderCalib();
}
function renderCalib() {
  if (!CAL || !$("#cal-slots")) return;
  const trays = (ST.printer.trays || []).filter((t) => t.loaded && t.preset);
  if (!trays.length) {
    $("#cal-slots").innerHTML = `<div class="muted small">Waiting for the printer's Canvas slots (is the bridge on?)</div>`;
    $("#cal-vals").innerHTML = $("#cal-tests").innerHTML = ""; return;
  }
  if (calSlot == null || !trays.find((t) => t.id === calSlot)) calSlot = (trays.find((t) => t.active) || trays[0]).id;
  $("#cal-slots").innerHTML = trays.map((t) => `<button data-slot="${t.id}" class="${t.id === calSlot ? "on" : ""}"><span class="dot" style="background:${esc(t.color)}"></span>${t.id + 1} · ${esc(slotMaterial(t.id) || t.name || t.type)}</button>`).join("");
  $$("#cal-slots button").forEach((b) => b.onclick = () => { calSlot = +b.dataset.slot; renderCalib(); });
  const tr = trays.find((t) => t.id === calSlot), preset = tr.preset;
  const base = (ST.printer.presets || {})[preset] || {}, saved = CAL.calib[preset] || {};
  const tuned = { ...(saved.filament || {}), ...(saved.process || {}) };
  $("#cal-vals").innerHTML = `<div class="muted small" style="margin-top:8px">Profile: <b>${esc(shortPreset(preset))}</b>${Object.keys(tuned).length ? "" : " · Elegoo defaults"}</div>` +
    CAL_ROWS.map(([k, label, unit]) => `<div class="cal-val"><span>${label}${k in tuned ? ` <span class="badge ok">tuned</span>` : ""}</span>
      <span><b>${base[k] != null ? +(+base[k]).toFixed(4) : "—"}${unit}</b>${k in tuned ? ` <button class="btn small" data-reset="${k}">Reset</button>` : ""}</span></div>`).join("");
  $$("#cal-vals [data-reset]").forEach((b) => b.onclick = (e) => act(e.target, async () => {
    await api("/api/calib/reset", { json: { preset, key: b.dataset.reset } }); await loadCalib();
  }, "Back to Elegoo's value"));
  $("#cal-tests").innerHTML = Object.entries(CAL.tests).map(([k, t]) => `<div class="cal-test"><div class="grow"><div class="t">${esc(t.title)}</div>
      <div class="muted small">Sets: ${esc(t.sets)} · ~${t.minutes} min</div><div class="muted small">${esc(t.why)}</div></div>
      <button class="btn small" data-test="${k}">Print</button></div>`).join("");
  $$("#cal-tests [data-test]").forEach((b) => b.onclick = () => {
    const t = CAL.tests[b.dataset.test];
    openSheet(`<h3>${esc(t.title)}</h3><p class="muted">Prints now with slot ${calSlot + 1} (${esc(slotMaterial(calSlot) || tr.name || tr.type)}), about ${t.minutes} minutes.
      Make sure the bed is clear. When it's done, open Jobs and tap <b>Answer</b>.</p>
      <button class="btn primary wide" id="cal-go">Start test print</button>`);
    $("#cal-go").onclick = (e) => act(e.target, async () => {
      const job = await api("/api/calib/start", { json: { test: b.dataset.test, slot: calSlot } });
      upsertJob(job); closeSheet(); $('nav button[data-tab="jobs"]').click();
    }, "Calibration print sent");
  });
  const groups = [];
  (CAL.log || []).forEach((e) => { let g = groups.find((x) => x.g === e.g); if (!g) groups.push(g = { g: e.g, items: [] }); g.items.push(e); });
  $("#cal-log").innerHTML = groups.slice(0, 6).map((g, i) => `<div class="cal-log"><div class="grow">
      <div>${esc(shortPreset(g.items[0].preset))}: ${g.items.filter((e) => !/initial_layer/.test(e.key)).map((e) => `${esc(e.key.replace(/_/g, " "))} ${e.old ?? "default"} → ${e.new ?? "default"}`).join(", ")}</div>
      <div class="muted small">${esc(g.items[0].why)} · ${new Date(g.items[0].t * 1000).toLocaleDateString()}</div></div>
      ${i === 0 ? `<button class="btn small" id="cal-undo">Undo</button>` : ""}</div>`).join("") || `<div class="muted small">Nothing tuned yet.</div>`;
  if ($("#cal-undo")) $("#cal-undo").onclick = (e) => act(e.target, async () => { await api("/api/calib/undo", { json: {} }); await loadCalib(); }, "Undone");
}

function answerSheet(j) {
  const c = j.calib, q = c.question;
  let body = "";
  if (q.kind === "pick") body = q.options.map((o, i) => `<label class="opt"><input type="radio" name="ans" value="${o.value}" ${i === Math.floor(q.options.length / 2) ? "" : ""}><span>${esc(o.label)}</span></label>`).join("");
  else if (q.kind === "height") body = `<div class="range-row"><input type="range" id="ans-h" min="0" max="${q.max}" step="0.5" value="${q.max / 2}"><span class="val" id="ans-hv"></span></div>
      <div class="muted small">Measure from the bed side of the tower up to where the corners look best.</div>`;
  else body = `<div class="dims">${[["x_mid", "X, middle"], ["y_mid", "Y, middle"], ["x_bot", "X, bottom edge"], ["y_bot", "Y, bottom edge"]].map(([k, t]) =>
      `<label>${t}<input type="number" step="0.01" inputmode="decimal" id="ans-${k}" placeholder="20.00"></label>`).join("")}</div>`;
  openSheet(`<h3>${esc(c.title)}</h3><div class="muted small">${esc(shortPreset(c.preset))} · slot ${c.slot + 1}</div>
    <p>${esc(q.prompt)}</p>${body}<button class="btn primary wide" id="ans-go">Save</button>
    <button class="btn ghost wide" id="ans-keep">Can't tell a difference: keep current</button>`);
  if (q.kind === "height") {
    const show = () => { const h = +$("#ans-h").value; $("#ans-hv").textContent = `${h} mm → ${Math.max(0, (h - q.z0) * q.step).toFixed(3)}`; };
    $("#ans-h").oninput = show; show();
  }
  $("#ans-keep").onclick = (e) => act(e.target, async () => {
    const r = await api("/api/calib/answer", { json: { job: j.id, answer: { keep: true } } });
    upsertJob(r.job); closeSheet();
  }, "Kept the current value: that's fine, it means it was already close");
  $("#ans-go").onclick = (e) => act(e.target, async () => {
    let answer;
    if (q.kind === "pick") { const r = $('input[name="ans"]:checked'); if (!r) throw new Error("Pick one"); answer = { value: +r.value }; }
    else if (q.kind === "height") answer = { height: +$("#ans-h").value };
    else { answer = {}; ["x_mid", "y_mid", "x_bot", "y_bot"].forEach((k) => (answer[k] = parseFloat(($("#ans-" + k).value || "").replace(",", ".")))); }
    const r = await api("/api/calib/answer", { json: { job: j.id, answer } });
    upsertJob(r.job);
    openSheet(`<h3>Saved</h3><p>${esc(shortPreset(r.preset))}: ${esc(r.summary)}</p><p class="muted small">Every print with this filament now uses it. Undo or reset any time in Settings → Calibrate.</p>
      <button class="btn wide" id="ans-ok">OK</button>`);
    $("#ans-ok").onclick = closeSheet;
    if (CAL) loadCalib();
  });
}

async function feedbackSheet(j) {
  if (!CAL) { try { CAL = await api("/api/calib"); } catch (e) { return toast(e.message); } }
  const picked = new Set();
  openSheet(`<h3>How did it turn out?</h3><div class="muted small">${esc((j.origin && j.origin.title) || j.filename)} · ${esc(shortPreset((j.presets || [])[0]))}</div>
    <p class="muted small">Tap anything that went wrong. Each one makes a small change to this filament's settings for next time (you can undo it).</p>
    <div class="chipset">${Object.entries(CAL.feedback).map(([k, t]) => `<button data-k="${k}">${esc(t)}</button>`).join("")}</div>
    <button class="btn primary wide" id="fb-go">Tune next prints</button><button class="btn ghost wide" id="fb-good">It came out great</button>`);
  $$(".chipset button").forEach((b) => b.onclick = () => { picked.has(b.dataset.k) ? picked.delete(b.dataset.k) : picked.add(b.dataset.k); b.classList.toggle("on"); });
  $("#fb-good").onclick = () => { closeSheet(); toast("Nice! Nothing changed."); };
  $("#fb-go").onclick = (e) => act(e.target, async () => {
    if (!picked.size) throw new Error("Tap what went wrong first");
    const r = await api("/api/calib/feedback", { json: { job: j.id, issues: [...picked] } });
    openSheet(`<h3>Tuned ${esc(shortPreset(r.preset))}</h3><p>${r.changes.map(esc).join("<br>")}</p>
      <button class="btn wide" id="fb-ok">OK</button><button class="btn ghost wide" id="fb-undo">Undo</button>`);
    $("#fb-ok").onclick = closeSheet;
    $("#fb-undo").onclick = (ev) => act(ev.target, async () => { await api("/api/calib/undo", { json: {} }); closeSheet(); }, "Undone");
  });
}

// ---------- slicing settings ----------
const FIELDS = [
  ["curr_bed_type", "Build plate", "", [["", "Default (Textured PEI)"], ["Textured PEI Plate", "Textured PEI"], ["High Temp Plate", "Smooth PEI / High temp"], ["Cool Plate", "Cool plate"], ["Engineering Plate", "Engineering"]]],
  ["elefant_foot_compensation", "Elephant foot compensation", "mm · Elegoo default 0.1"],
  ["layer_height", "Layer height", "mm · overrides quality"],
  ["wall_loops", "Walls", "count · default 2"],
  ["top_shell_layers", "Top layers", "default 5"],
  ["bottom_shell_layers", "Bottom layers", "default 3"],
  ["sparse_infill_density", "Infill density", "e.g. 15%"],
  ["sparse_infill_pattern", "Infill pattern", "", ["", "rectilinear", "grid", "gyroid", "cubic", "honeycomb", "adaptivecubic", "lightning", "crosshatch"]],
  ["enable_support", "Supports", "", [["", "Default"], ["1", "On"], ["0", "Off"]]],
  ["support_type", "Support type", "", [["", "Default"], ["tree(auto)", "Tree (auto)"], ["normal(auto)", "Normal (auto)"]]],
  ["support_threshold_angle", "Support angle", "° · default 30"],
  ["support_on_build_plate_only", "Supports from plate only", "", [["", "Default"], ["1", "Yes"], ["0", "No"]]],
  ["brim_type", "Brim", "", [["", "Default"], ["auto_brim", "Auto"], ["outer_only", "Outer"], ["no_brim", "None"]]],
  ["brim_width", "Brim width", "mm · default 5"],
  ["seam_position", "Seam", "", [["", "Default"], ["aligned", "Aligned"], ["back", "Back"], ["nearest", "Nearest"], ["random", "Random"]]],
  ["ironing_type", "Ironing", "", [["", "Default"], ["no ironing", "Off"], ["top", "Top surfaces"], ["topmost", "Topmost only"]]],
  ["xy_hole_compensation", "Hole compensation", "mm · default 0"],
  ["xy_contour_compensation", "Contour compensation", "mm · default 0"],
  ["initial_layer_speed", "First layer speed", "mm/s · default 50"],
  ["outer_wall_speed", "Outer wall speed", "mm/s · default 160"],
];
let SET = null;
// Elegoo's filament list for Canvas slots (name, type, code, min/max temp, sold by Elegoo) as its own web UI has it
const FILAMENTS = [["PLA","PLA","0x0000",190,230,true],["PLA+","PLA","0x0001",190,230,true],["PLA PRO","PLA","0x0002",190,230,true],["PLA Silk","PLA","0x0003",190,230,true],["PLA-CF","PLA","0x0004",210,240,true],["PLA Carbon","PLA","0x0005",190,230,false],["PLA Matte","PLA","0x0006",190,230,true],["PLA Fluo","PLA","0x0007",190,230,false],["PLA Wood","PLA","0x0008",190,230,true],["PLA Basic","PLA","0x0009",190,230,true],["RAPID PLA+","PLA","0x000A",190,230,true],["PLA Marble","PLA","0x000B",190,230,true],["PLA Galaxy","PLA","0x000C",190,230,true],["PLA Red Copper","PLA","0x000D",190,230,true],["PLA Sparkle","PLA","0x000E",190,230,false],["PETG","PETG","0x0100",230,260,true],["PETG-CF","PETG","0x0101",240,270,true],["PETG-GF","PETG","0x0102",240,270,true],["PETG PRO","PETG","0x0103",230,260,true],["PETG Translucent","PETG","0x0104",230,260,true],["RAPID PETG","PETG","0x0105",230,260,true],["ABS","ABS","0x0200",240,280,true],["ABS-GF","ABS","0x0201",240,280,false],["TPU","TPU","0x0300",220,240,false],["TPU 95A","TPU","0x0301",220,240,true],["RAPID TPU 95A","TPU","0x0302",220,240,true],["PA","PA","0x0400",260,290,false],["PA-CF","PA","0x0401",260,300,false],["PAHT-CF","PA","0x0402",280,320,true],["PA6","PA","0x0403",260,290,false],["PA6-CF","PA","0x0404",270,310,false],["PA12","PA","0x0405",240,270,false],["PA12-CF","PA","0x0406",260,290,false],["CPE","CPE","0x0500",220,250,false],["PC","PC","0x0600",260,290,true],["PCTG","PC","0x0601",260,290,false],["PC-FR","PC","0x0602",260,290,true],["PVA","PVA","0x0700",180,210,false],["ASA","ASA","0x0800",240,280,true],["BVOH","BVOH","0x0900",190,210,false],["EVA","EVA","0x0A00",180,220,false],["HIPS","HIPS","0x0B00",220,250,false],["PP","PP","0x0C00",210,250,false],["PP-CF","PP","0x0C01",220,260,false],["PP-GF","PP","0x0C02",230,250,false],["PPA","PPA","0x0D00",290,310,false],["PPA-CF","PPA","0x0D01",300,320,false],["PPA-GF","PPA","0x0D02",290,310,false],["PPS","PPS","0x0E00",330,340,false],["PPS-CF","PPS","0x0E01",340,360,false]];
const MATERIALS = ["", "PLA", "PLA Wood", "PLA Silk", "PLA Matte", "PLA+", "PETG", "ABS", "ASA", "TPU 95A"];
function slotMaterial(id) { return SET && SET.materials ? SET.materials[id] || "" : ""; }
const SITES = ["nexprint", "makerworld", "thingiverse", "cults3d", "printables"];
function showTokens(t) { SITES.forEach((k) => { $("#tk-" + k).textContent = t[k] ? "✓ saved" : "not set"; $("#ti-" + k).value = ""; }); }
$("#tk-save").onclick = (e) => act(e.target, async () => {
  const body = {};
  SITES.forEach((k) => { const v = $("#ti-" + k).value.trim(); if (v) body[k] = v; });
  showTokens(await api("/api/tokens", { method: "PUT", json: body }));
}, "Logins saved");
async function loadSettings() {
  api("/api/tokens").then(showTokens).catch(() => {});
  api("/api/alerts").then(showAlerts).catch(() => {});
  loadCalib();
  try { SET = await api("/api/settings"); } catch (e) { return toast(e.message); }
  $$("#quality button").forEach((b) => { b.classList.toggle("on", b.dataset.v === SET.quality); b.onclick = () => { SET.quality = b.dataset.v; $$("#quality button").forEach((x) => x.classList.toggle("on", x === b)); }; });
  const known = new Set(FIELDS.map((f) => f[0]));
  $("#fields").innerHTML = FIELDS.map(([k, label, hint, opts]) => {
    const v = SET.overrides[k] ?? "";
    const ctl = opts ? `<select data-k="${k}">${opts.map((o) => { const [val, txt] = Array.isArray(o) ? o : [o, o || "Default"]; return `<option value="${esc(val)}" ${val === v ? "selected" : ""}>${esc(txt)}</option>`; }).join("")}</select>`
      : `<input data-k="${k}" value="${esc(v)}" placeholder="default">`;
    return `<div class="field"><div>${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ""}</div>${ctl}</div>`;
  }).join("");
  $("#materials").innerHTML = [0, 1, 2, 3].map((i) => {
    const tr = (ST.printer.trays || []).find((t) => t.id === i) || {}; const v = slotMaterial(i);
    return `<div class="field"><div><span class="dot" style="background:${esc(tr.color || "#444")}"></span> Slot ${i + 1}<small>printer says: ${esc(tr.loaded ? tr.name || tr.type : "empty")}</small></div>
      <select data-slot="${i}">${MATERIALS.map((m) => `<option value="${esc(m)}" ${m === v ? "selected" : ""}>${m ? esc(m) : "As reported"}</option>`).join("")}</select></div>
      <div class="field spool"><div class="muted small">Filament left on slot ${i + 1}</div><div class="spool-ctl"><input type="number" min="0" max="5000" step="10" data-left="${i}" value="${spoolLeft(i) != null ? Math.round(spoolLeft(i)) : ""}" placeholder="—"><span class="muted small">g</span><button class="btn ghost small" data-full="${i}">New 1 kg</button></div></div>
      <div class="field spool"><div class="muted small">Price of a 1 kg spool</div><div class="spool-ctl"><input type="number" min="0" max="500" step="0.5" data-price="${i}" value="${(SET.prices || {})[i] ?? ""}" placeholder="—"><span class="muted small">$ per kg</span></div></div>`;
  }).join("");
  $$("#materials [data-full]").forEach((b) => b.onclick = () => { $(`#materials [data-left="${b.dataset.full}"]`).value = 1000; });
  $("#custom").innerHTML = "";
  Object.entries(SET.overrides).filter(([k]) => !known.has(k)).forEach(([k, v]) => addCustom(k, v));
  $("#s-level").checked = !!SET.level; $("#s-tl").checked = !!SET.timelapse;
  $("#s-ctl").checked = SET.cam_timelapse !== false; $("#s-watch").value = SET.watch || "alert";
  $("#s-lock").checked = store.get("lockprog", true); $("#s-lock-row").classList.toggle("hidden", !Progress);
}
function addCustom(k = "", v = "") {
  $("#custom").insertAdjacentHTML("beforeend", `<div class="crow"><input placeholder="setting_key" value="${esc(k)}"><input placeholder="value" value="${esc(v)}"><button class="btn small">✕</button></div>`);
  const row = $("#custom").lastElementChild; $("button", row).onclick = () => row.remove();
}
$("#add-custom").onclick = () => addCustom();
$("#s-save").onclick = (e) => act(e.target, async () => {
  const o = {};
  $$("#fields [data-k]").forEach((el) => { if (el.value.trim() !== "") o[el.dataset.k] = el.value.trim(); });
  $$("#custom .crow").forEach((r) => { const [k, v] = $$("input", r).map((i) => i.value.trim()); if (k && v) o[k] = v; });
  const mats = {}; $$("#materials [data-slot]").forEach((el) => { if (el.value) mats[el.dataset.slot] = el.value; });
  const prices = {}; $$("#materials [data-price]").forEach((el) => { if (el.value.trim() !== "") prices[el.dataset.price] = +el.value; });
  SET = await api("/api/settings", { method: "PUT", json: { quality: SET.quality, overrides: o, level: $("#s-level").checked, timelapse: $("#s-tl").checked, materials: mats,
    prices, cam_timelapse: $("#s-ctl").checked, watch: $("#s-watch").value } });
  const sp = {}; $$("#materials [data-left]").forEach((el) => { if (el.value.trim() !== "") { const k = el.dataset.left, old = ST.spools[k] || {}; sp[k] = { left: +el.value, full: +el.value === 1000 ? 1000 : old.full || 1000 }; } });
  ST.spools = await api("/api/spools", { method: "PUT", json: sp });
  $("#s-msg").textContent = "Saved. Next print uses these.";
}, "Slicing settings saved");

// ---------- phone alerts (ntfy) ----------
const AL_KEYS = ["done", "failed", "paused", "first_layers", "almost", "photo"];
function showAlerts(a) {
  $("#al-url").value = a.url || "";
  AL_KEYS.forEach((k) => { $("#al-" + k).checked = !!a[k]; });
  $("#al-msg").textContent = a.url ? "On" : "Off: no topic set";
}
$("#al-gen").onclick = () => {
  const r = Array.from(crypto.getRandomValues(new Uint8Array(9)), (b) => "abcdefghijkmnpqrstuvwxyz23456789"[b % 32]).join("");
  $("#al-url").value = "https://ntfy.sh/mpc-" + r;
  $("#al-msg").textContent = "Subscribe to mpc-" + r + " in the ntfy app, then Save.";
};
$("#al-save").onclick = (e) => act(e.target, async () => {
  const body = { url: $("#al-url").value.trim() };
  AL_KEYS.forEach((k) => { body[k] = $("#al-" + k).checked; });
  showAlerts(await api("/api/alerts", { method: "PUT", json: body }));
}, "Alerts saved");
$("#al-test").onclick = (e) => act(e.target, async () => {
  await api("/api/alerts/test", { method: "POST" });
  $("#al-msg").textContent = "Test sent: it should pop up on your phone within a few seconds.";
}, "Test sent");

$("#s-lock").onchange = (e) => {
  store.set("lockprog", e.target.checked);
  if (!e.target.checked && Progress) Progress.stop().catch(() => {});
  progressTick.on = false; render();
};

// ---------- boot ----------

function start() {
  $("#login").classList.add("hidden"); $("#main").classList.remove("hidden");
  $("#srv").textContent = "Server: " + SERVER;
  renderJobs.loaded = false;
  connectWS(); render(); camTick();
  api("/api/settings").then((s) => { SET = SET || s; }).catch(() => {});
  api("/api/spools").then((s) => { ST.spools = s || {}; render(); }).catch(() => {});
  api("/api/queue").then(renderQueue).catch(() => {});
  renderService();
  renderBed();
}
if (TOKEN && SERVER) start(); else showLogin();
