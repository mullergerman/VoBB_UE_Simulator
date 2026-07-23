"use strict";

// ---------------- Estado en memoria ----------------
let token = localStorage.getItem("vobb_token") || null;
let currentUser = null;       // { id, username, is_admin, permissions, ... }
let users = [];               // lista de usuarios (solo admin)
let abonados = [];
let profiles = [];            // perfiles (parámetros compartidos)
const SHARED_FIELDS = ["domain","pcscf_addr","pcscf_port","transport","auth_realm","registrar_uri","codec_pref","alerting_delay_s","echo_enabled","reg_expires","reg_event_enabled","reg_event_expires","hdr_register","hdr_invite","hdr_subscribe"];
const regState = {};          // abonado_id -> {active, code, reason}
const calls = {};             // call_id -> {line, remote, state, last_code, abonado_id}
const sipAll = [];            // log global de mensajes SIP (se filtra por abonado al render)
const sipByCallId = {};       // call_id -> abonado_id (atribución por llamada activa)
const rtpByAbonado = {};      // abonado_id -> { call_id -> stats }
let detailAbonado = null;
let lastNet = null;             // último status.net (solo admin)
let engineMode = "";            // "ims" | "local" (del status del motor)
let callHistory = [];          // histórico de llamadas cargado (vista Llamadas)
let histDir = "all", histRes = "all", histSearch = "";
let statusSearch = "", provSearch = "";  // búsquedas de las tablas
const MAX_SIP = 300;
let sipFilterText = "";
let sipFilterDir = "all";
let sipAutoscroll = true;
const sipOpen = new Set();     // ids de mensajes SIP expandidos (persisten al re-render)
let sipSeq = 0;

// ---------------- Helpers ----------------
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
// Notificaciones no bloqueantes (reemplazan alert() para feedback).
function toast(msg, type = "info") {
  const box = $("#toasts"); if (!box) return;
  const t = el("div", "toast " + type, msg);
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, type === "error" ? 6000 : 3200);
}
async function api(method, url, body) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const opt = { method, headers };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  if (r.status === 401) { showLogin(); throw new Error("No autenticado"); }
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (e) {}
    toast(msg, "error"); throw new Error(msg);
  }
  return r.status === 204 ? null : r.json().catch(() => null);
}
const can = (perm) => currentUser && (currentUser.is_admin || (currentUser.permissions || []).includes(perm));
const fmtTime = (ms) => new Date(ms).toLocaleTimeString("es-AR", { hour12: false }) + "." + String(ms % 1000).padStart(3, "0");
const now = () => fmtTime(Date.now());

// Orden natural ascendente por línea (trata los dígitos como números, así
// "1002" < "1010" y "+5411..." se ordena por su parte numérica).
const _natKey = (s) => String(s || "").replace(/\D+/g, "").padStart(24, "0") + "|" + String(s || "");
function sortedAbonados() {
  return [...abonados].sort((a, b) => _natKey(a.line_number).localeCompare(_natKey(b.line_number)));
}

// ---------------- Carga inicial ----------------
async function loadAll() {
  profiles = await api("GET", "/api/profiles");
  abonados = await api("GET", "/api/abonados");
  if (currentUser && currentUser.is_admin) users = await api("GET", "/api/users");
  applyGating();
  renderProfiles();
  renderStatus();
  renderProvisioning();
  if (currentUser && currentUser.is_admin) renderUsers();
  renderCallSelectors();
  renderDetailSelector();
  refreshCallStats();
}
async function loadAbonados() { await loadAll(); }

// Muestra/oculta navegación y botones según permisos del usuario.
function applyGating() {
  const admin = currentUser && currentUser.is_admin;
  // Navegación: la vista Usuarios es solo para admin.
  $("#nav-usuarios").style.display = admin ? "" : "none";
  // Botones de alta gateados por permiso.
  $("#btn-new-profile").style.display = can("manage_profiles") ? "" : "none";
  $("#btn-new").style.display = can("edit_abonados") ? "" : "none";
  const callable = can("control_calls");
  for (const id of ["#btn-call", "#btn-hangup-all", "#btn-reg-all", "#btn-unreg-all", "#btn-hangup-all2"])
    $(id).style.display = callable ? "" : "none";
  $("#btn-clear-history").style.display = admin ? "" : "none";
  $("#net-panel").style.display = admin ? "" : "none";
  // Chip de usuario en la sidebar.
  const uc = $("#current-user");
  if (currentUser) {
    uc.textContent = currentUser.display_name || currentUser.username;
    uc.appendChild(el("span", "role", admin ? "admin" : "usuario"));
  }
  // Si un no-admin estaba en la vista Usuarios, mandarlo a Abonados.
  if (!admin && currentView === "usuarios") showView("abonados");
}

// ---------------- Router de vistas ----------------
const VIEWS = ["dashboard", "abonados", "llamadas", "trazas", "perfiles", "usuarios"];
let currentView = "dashboard";
function showView(name) {
  if (!VIEWS.includes(name)) name = "dashboard";
  currentView = name;
  $("#view-perfil-edit").classList.add("hidden");   // el editor es un overlay aparte
  for (const v of VIEWS) {
    $("#view-" + v).classList.toggle("hidden", v !== name);
    $("#nav-" + v).classList.toggle("active", v === name);
  }
  if (location.hash !== "#/" + name) history.replaceState(null, "", "#/" + name);
  $("#app").classList.remove("nav-open");   // cerrar sidebar en móvil
  if (name === "dashboard") { renderNetPanel(); refreshCallStats(); }
  if (name === "llamadas") loadHistory();
}

// ---------------- Stat tiles ----------------
function updateStats() {
  const reg = abonados.filter((a) => (regState[a.id] || {}).active).length;
  const active = Object.values(calls).filter((c) => c.state && c.state !== "DISCONNECTED").length;
  const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  set("#stat-total", abonados.length);
  set("#stat-reg", reg);
  set("#stat-unreg", abonados.length - reg);
  set("#stat-calls", active);
}

const profileById = (id) => profiles.find((p) => String(p.id) === String(id));

// Valores efectivos del abonado: si tiene perfil, hereda los campos compartidos.
function effective(a) {
  const p = a.profile_id != null ? profileById(a.profile_id) : null;
  if (!p) return a;
  const e = { ...a };
  for (const f of SHARED_FIELDS) e[f] = p[f];
  return e;
}

// Llamada activa (no desconectada) de un abonado, si la hay.
function activeCallOf(abId) {
  return Object.values(calls).find((c) => String(c.abonado_id) === String(abId) && c.state && c.state !== "DISCONNECTED");
}

// --- Dashboard: estado operativo de los abonados (live) ---
function renderStatus() {
  const tb = $("#status-body");
  if (!tb) return;
  const q = statusSearch;
  let list = sortedAbonados();
  if (q) list = list.filter((a) => (a.line_number + " " + (a.display_name || "") + " " + (profileById(a.profile_id) || {}).name).toLowerCase().includes(q));
  const cnt = $("#status-count"); if (cnt) cnt.textContent = list.length;
  tb.innerHTML = "";
  if (!list.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">Sin abonados que coincidan</td></tr>'; return; }
  for (const a of list) {
    const tr = el("tr");
    const rs = regState[a.id] || {};
    const dotCls = rs.active ? "ok" : (rs.code >= 400 ? "fail" : "pending");
    const dotTd = el("td"); const dot = el("span", "reg-dot " + dotCls);
    dot.title = rs.active ? "Registrado" : (rs.reason ? `${rs.code} ${rs.reason}` : "No registrado");
    dotTd.appendChild(dot); tr.appendChild(dotTd);
    tr.appendChild(el("td", "mono", a.line_number));
    const pf = profileById(a.profile_id);
    const pfTd = el("td"); pfTd.appendChild(el("span", "profile-pill" + (pf ? "" : " none"), pf ? pf.name : "personalizado")); tr.appendChild(pfTd);
    const regTd = el("td", "mono");
    regTd.textContent = rs.active ? "activo" : (rs.code ? `${rs.code} ${rs.reason || ""}`.trim() : "—");
    tr.appendChild(regTd);
    const ac = activeCallOf(a.id);
    const cTd = el("td");
    if (ac) cTd.appendChild(el("span", "state-badge state-" + ac.state, ac.state)); else cTd.appendChild(el("span", "muted", "—"));
    tr.appendChild(cTd);
    const act = el("td", "col-actions"); const grp = el("div", "row-actions");
    if (can("edit_abonados") || can("control_calls")) {
      const bReg = el("button", "small" + (rs.active ? " danger" : ""), rs.active ? "Unreg" : "Reg");
      bReg.onclick = () => api("POST", `/api/abonados/${a.id}/${rs.active ? "unregister" : "register"}`)
        .then(() => toast((rs.active ? "Desregistrando " : "Registrando ") + a.line_number));
      grp.appendChild(bReg);
    }
    if (can("control_calls")) {
      const bCall = el("button", "small", "☏ Llamar desde");
      bCall.onclick = () => callFrom(a.id);
      grp.appendChild(bCall);
    }
    if (!grp.children.length) grp.appendChild(el("span", "muted", "—"));
    act.appendChild(grp); tr.appendChild(act);
    tb.appendChild(tr);
  }
  updateStats();
}

// --- Abonados: provisión / configuración (sin estado en vivo) ---
function renderProvisioning() {
  const tb = $("#abonados-body");
  if (!tb) return;
  const q = provSearch;
  let list = sortedAbonados();
  if (q) list = list.filter((a) => (a.line_number + " " + (a.short_number || "") + " " + (a.display_name || "") + " " + (profileById(a.profile_id) || {}).name).toLowerCase().includes(q));
  const cnt = $("#prov-count"); if (cnt) cnt.textContent = list.length;
  tb.innerHTML = "";
  if (!list.length) {
    tb.innerHTML = abonados.length
      ? '<tr><td colspan="9" class="empty">Sin abonados que coincidan</td></tr>'
      : '<tr><td colspan="9" class="empty">Sin abonados. Creá uno con «＋ Nuevo abonado».</td></tr>';
    return;
  }
  for (const a of list) {
    const e = effective(a);
    const tr = el("tr");
    tr.appendChild(el("td", "mono", a.line_number));
    tr.appendChild(el("td", "mono", a.short_number || "—"));
    tr.appendChild(el("td", null, a.display_name || "—"));
    const pf = profileById(a.profile_id);
    const pfTd = el("td"); pfTd.appendChild(el("span", "profile-pill" + (pf ? "" : " none"), pf ? pf.name : "personalizado")); tr.appendChild(pfTd);
    tr.appendChild(el("td", "mono", a.auth_user || a.line_number));
    tr.appendChild(el("td", "mono", e.domain));
    tr.appendChild(el("td", "mono", `${e.pcscf_addr}:${e.pcscf_port}/${e.transport}`));
    const enTd = el("td"); enTd.appendChild(el("span", "chip-flag " + (a.enabled ? "on" : "off"), a.enabled ? "sí" : "no")); tr.appendChild(enTd);
    const act = el("td", "col-actions"); const grp = el("div", "row-actions");
    if (can("edit_abonados")) {
      const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openModal(a);
      const bDel = el("button", "small danger", "Eliminar");
      bDel.onclick = () => { if (confirm("¿Borrar abonado " + a.line_number + "?")) api("DELETE", `/api/abonados/${a.id}`).then(() => { toast("Abonado " + a.line_number + " eliminado"); loadAll(); }); };
      grp.append(bEdit, bDel);
    } else grp.appendChild(el("span", "muted", "—"));
    act.appendChild(grp); tr.appendChild(act);
    tb.appendChild(tr);
  }
}

function renderProfiles() {
  const tb = $("#profiles-body");
  tb.innerHTML = "";
  if (!profiles.length) { tb.innerHTML = '<tr><td colspan="10" class="empty">Sin perfiles</td></tr>'; return; }
  for (const p of profiles) {
    const used = abonados.filter((a) => String(a.profile_id) === String(p.id)).length;
    const tr = el("tr");
    tr.appendChild(el("td", null, p.name));
    tr.appendChild(el("td", "mono", p.domain));
    tr.appendChild(el("td", "mono", `${p.pcscf_addr}:${p.pcscf_port}/${p.transport}`));
    tr.appendChild(el("td", "mono", p.auth_realm || "*"));
    tr.appendChild(el("td", "mono", p.registrar_uri || "(dominio)"));
    tr.appendChild(el("td", "mono", p.codec_pref));
    tr.appendChild(el("td", null, p.alerting_delay_s + "s"));
    tr.appendChild(el("td", null, p.echo_enabled ? "sí" : "no"));
    tr.appendChild(el("td", null, String(used)));
    const act = el("td", "col-actions"); const grp = el("div", "row-actions");
    if (can("manage_profiles")) {
      const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openProfileEditor(p);
      const bDel = el("button", "small danger", "Eliminar");
      bDel.onclick = () => {
        const msg = used ? `El perfil "${p.name}" lo usan ${used} abonado(s). Se desvincularán (conservando esta config). ¿Continuar?` : `¿Borrar perfil "${p.name}"?`;
        if (confirm(msg)) api("DELETE", `/api/profiles/${p.id}`).then(() => { toast(`Perfil "${p.name}" eliminado`); loadAll(); });
      };
      grp.append(bEdit, bDel);
    } else grp.appendChild(el("span", "muted", "—"));
    act.appendChild(grp); tr.appendChild(act);
    tb.appendChild(tr);
  }
}

// ---------------- Dashboard ----------------
function renderNetPanel() {
  const box = $("#net-body"); if (!box) return;
  const n = lastNet;
  if (!n) { box.innerHTML = '<div class="net-row"><span class="k">—</span><span class="v muted">solo admin / sin datos</span></div>'; return; }
  const ifaces = (n.local_addrs || []).map((a) => `${a.iface}=${a.ip}`).join(", ") || "—";
  const relay = n.relay_running ? "activo" : (n.relay_enabled ? "no arrancó" : "off");
  const rows = [
    ["IP local", n.bind_ip || "(0.0.0.0)"],
    ["Interfaces", ifaces],
    ["SIP público", n.sip_public || "—"],
    ["RTP público", n.rtp_public || "—"],
    ["Relay", `${relay}${n.relay ? " · EXT " + (n.relay.ext || "?") : ""}`],
    ["Upstream P-CSCF", (n.relay && n.relay.upstream) || "—"],
  ];
  box.innerHTML = "";
  for (const [k, v] of rows) {
    const row = el("div", "net-row");
    row.appendChild(el("span", "k", k));
    row.appendChild(el("span", "v mono", String(v)));
    box.appendChild(row);
  }
}

async function refreshCallStats() {
  try {
    const st = await api("GET", "/api/call-stats");
    const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
    // Dashboard
    set("#stat-asr", st.total ? st.asr + "%" : "—");
    set("#d-total", st.total); set("#d-answered", st.answered);
    set("#d-failed", st.failed); set("#d-acd", st.acd + "s");
    // Vista Llamadas
    set("#cs-total", st.total); set("#cs-answered", st.answered);
    set("#cs-failed", st.failed); set("#cs-asr", st.total ? st.asr + "%" : "—");
    set("#cs-acd", st.acd + "s"); set("#cs-active", st.active);
  } catch (e) { /* no autenticado / sin permiso: ignorar */ }
}

// ---------------- Llamadas (histórico) ----------------
async function loadHistory() {
  const qs = new URLSearchParams({ limit: "500" });
  if (histDir !== "all") qs.set("direction", histDir);
  if (histRes !== "all") qs.set("result", histRes);
  try { callHistory = await api("GET", "/api/call-history?" + qs.toString()); }
  catch (e) { callHistory = []; }
  renderHistory();
  refreshCallStats();
}
const fmtDateTime = (ms) => { const d = new Date(ms); return d.toLocaleDateString("es-AR") + " " + d.toLocaleTimeString("es-AR", { hour12: false }); };
function codeClass(code) {
  if (code >= 200 && code < 300) return "b-2xx";
  if (code >= 300 && code < 400) return "b-3xx";
  if (code >= 400 && code < 500) return "b-4xx";
  if (code >= 500 && code < 600) return "b-5xx";
  if (code >= 600) return "b-6xx";
  return "b-req";
}
const fmtDur = (s) => { s = s || 0; const m = Math.floor(s / 60); return m ? `${m}m ${s % 60}s` : `${s}s`; };
function renderHistory() {
  const tb = $("#history-body"); if (!tb) return;
  tb.innerHTML = "";
  const q = histSearch;
  const rows = callHistory.filter((r) => !q ||
    (r.local_line || "").toLowerCase().includes(q) || (r.remote || "").toLowerCase().includes(q));
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="9" class="empty">Sin llamadas</td></tr>'; return; }
  for (const r of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", fmtDateTime(r.start_ms)));
    const dirTd = el("td");
    dirTd.appendChild(Object.assign(el("span", "dir-chip " + (r.direction === "MO" ? "tx" : "rx"), r.direction), {}));
    tr.appendChild(dirTd);
    tr.appendChild(el("td", "mono", r.local_line || "—"));
    tr.appendChild(el("td", "mono", r.remote || "—"));
    const resTd = el("td");
    const badge = el("span", "badge-sip " + codeClass(r.last_code), (r.last_code || "—") + (r.answered ? "" : "") );
    badge.title = r.reason || (r.answered ? "atendida" : "no atendida");
    resTd.appendChild(badge);
    tr.appendChild(resTd);
    tr.appendChild(el("td", null, fmtDur(r.duration_s)));
    tr.appendChild(el("td", "mono", `${r.tx_pkt || 0}/${r.rx_pkt || 0}`));
    tr.appendChild(el("td", "mono", String(r.rx_loss || 0)));
    tr.appendChild(el("td", "mono", ((r.rx_jitter_us || 0) / 1000).toFixed(1) + "ms"));
    tb.appendChild(tr);
  }
}

function renderCallSelectors() {
  const list = sortedAbonados();
  // Origen: cualquier abonado (id como valor); se marca si no está registrado.
  const from = $("#call-from"); const prevFrom = from.value; from.innerHTML = "";
  for (const a of list) {
    const reg = (regState[a.id] || {}).active ? "" : " (sin registrar)";
    const o = el("option", null, `${a.line_number} — ${a.display_name || a.domain}${reg}`);
    o.value = a.id; from.appendChild(o);
  }
  if (prevFrom) from.value = prevFrom;
  // Destino: campo libre con sugerencias (número corto de cada abonado). Permite
  // todas las combinaciones: cualquier origen → cualquier destino, num@dominio o
  // sip:URI a mano.
  const dl = $("#call-to-list"); dl.innerHTML = "";
  for (const a of list) {
    const dial = (a.short_number || "").trim() || a.line_number;
    const o = el("option");
    o.value = dial;
    o.label = `${a.line_number} — ${a.display_name || a.domain}`;
    dl.appendChild(o);
  }
  // Prefill del destino con el primer corto distinto del origen (si está vacío).
  const to = $("#call-to");
  if (to && !to.value) {
    const fromId = from.value;
    const cand = list.find((a) => String(a.id) !== String(fromId));
    if (cand) to.value = (cand.short_number || "").trim() || cand.line_number;
  }
}

// Atajo «Llamar desde» del dashboard: va a Llamadas con ese origen preseleccionado.
function callFrom(abId) {
  showView("llamadas");
  const from = $("#call-from");
  if (from) from.value = String(abId);
  const to = $("#call-to");
  if (to) { to.value = ""; renderCallSelectors(); to.focus(); }
}

function renderDetailSelector() {
  const sel = $("#detail-select"); const prev = sel.value; sel.innerHTML = "";
  for (const a of sortedAbonados()) {
    const o = el("option", null, `${a.line_number} — ${a.display_name || a.domain}`);
    o.value = a.id; sel.appendChild(o);
  }
  if (prev) sel.value = prev;
  if (!detailAbonado && abonados.length) detailAbonado = String(abonados[0].id);
  if (detailAbonado) sel.value = detailAbonado;
  renderDetail();
}

// ---------------- Llamadas ----------------
function renderCalls() {
  const tb = $("#calls-body");
  tb.innerHTML = "";
  const entries = Object.entries(calls).filter(([, c]) => c.state !== "DISCONNECTED");
  if (!entries.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">Sin llamadas activas</td></tr>'; return; }
  for (const [cid, c] of entries) {
    const tr = el("tr");
    const st = el("td"); const b = el("span", "state-badge state-" + c.state, c.state); st.appendChild(b); tr.appendChild(st);
    tr.appendChild(el("td", "mono", c.line || ""));
    tr.appendChild(el("td", "mono", (c.remote || "").replace(/^sip:/, "")));
    tr.appendChild(el("td", "mono", cid.slice(0, 18) + "…"));
    tr.appendChild(el("td", "mono", c.last_code || ""));
    const act = el("td");
    const bh = el("button", "small danger", "Colgar");
    bh.onclick = () => api("DELETE", "/api/calls/" + encodeURIComponent(cid));
    act.appendChild(bh); tr.appendChild(act);
    tb.appendChild(tr);
  }
  updateStats();
}

// ---------------- Detalle SIP + RTP ----------------
function renderDetail() {
  renderSip();
  renderRtp();
}

// Parsea el evento SIP crudo en una estructura para render enriquecido.
function parseSip(e) {
  const raw = e.raw || "";
  const first = raw.split(/\r?\n/, 1)[0] || "";
  let kind = "req", method = e.summary || "SIP", code = null, reason = "", detail = "";
  let mResp = first.match(/^SIP\/2\.0\s+(\d{3})\s+(.*)$/);
  let mReq = first.match(/^([A-Z]+)\s+(\S+)\s+SIP\/2\.0/);
  if (mResp) {
    kind = "resp"; code = parseInt(mResp[1], 10); reason = mResp[2].trim();
    method = code + ""; detail = reason;
    // CSeq revela a qué método responde
    const cs = raw.match(/^CSeq:\s*\d+\s+(\w+)/im);
    if (cs) detail = `${reason}  ·  ${cs[1]}`;
  } else if (mReq) {
    kind = "req"; method = mReq[1]; detail = mReq[2].replace(/^sip:/, "");
  }
  const callid = (raw.match(/^Call-ID:\s*(.+)$/im) || [])[1] || e.call_id || "";
  const t = e.ts_ms ? fmtTime(e.ts_ms) : now();
  return { id: ++sipSeq, dir: e.direction, kind, method, code, reason, detail,
           callid: callid.trim(), raw, t };
}

function sipBadgeClass(r) {
  if (r.kind === "resp") return "b-" + Math.floor(r.code / 100) + "xx";
  const m = r.method.toUpperCase();
  if (m === "INVITE") return "b-invite";
  if (m === "BYE" || m === "CANCEL") return "b-bye";
  return "b-req";
}

function sipMatchesFilter(r) {
  if (sipFilterDir !== "all" && r.dir !== sipFilterDir) return false;
  if (sipFilterText) {
    const hay = (r.method + " " + r.detail + " " + r.callid + " " + r.raw).toLowerCase();
    if (!hay.includes(sipFilterText)) return false;
  }
  return true;
}

// Construye el bloque raw con resaltado de start-line, headers y body.
function renderRaw(raw) {
  const wrap = el("div", "sipraw");
  const actions = el("div", "raw-actions");
  const copyBtn = el("button", "small", "Copiar");
  copyBtn.onclick = (ev) => { ev.stopPropagation(); navigator.clipboard?.writeText(raw); copyBtn.textContent = "✓"; setTimeout(() => copyBtn.textContent = "Copiar", 1000); };
  actions.appendChild(copyBtn);
  wrap.appendChild(actions);
  const lines = raw.split(/\r?\n/);
  let inBody = false;
  lines.forEach((ln, i) => {
    if (ln === "") { inBody = true; }
    const div = el("div", "rawline");
    if (i === 0) { div.classList.add("start"); div.textContent = ln; }
    else if (inBody) { div.classList.add("body"); div.textContent = ln; }
    else {
      const idx = ln.indexOf(":");
      if (idx > 0) {
        div.append(
          Object.assign(el("span", "hname"), { textContent: ln.slice(0, idx + 1) }),
          Object.assign(el("span", "hval"), { textContent: ln.slice(idx + 1) }),
        );
      } else { div.textContent = ln; }
    }
    wrap.appendChild(div);
  });
  return wrap;
}

function renderSip() {
  const box = $("#sip-console");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = "";
  const log = sipAll.filter((r) => sipRelevant(r, detailAbonado) && sipMatchesFilter(r));
  const cnt = $("#sip-count");
  if (cnt) cnt.textContent = log.length;
  if (!log.length) { box.innerHTML = '<div class="empty">Sin mensajes SIP' + (sipFilterText || sipFilterDir !== "all" ? " (filtro activo)" : " todavía") + '</div>'; return; }
  for (const r of log) {
    const msg = el("div", "sipmsg");
    if (sipOpen.has(r.id)) msg.classList.add("open");
    const line = el("div", "sipline");
    const dir = el("span", "dir-chip " + r.dir, r.dir === "tx" ? "↑" : "↓");
    const badge = el("span", "badge-sip " + sipBadgeClass(r), r.kind === "resp" ? r.method : r.method);
    line.append(
      dir,
      Object.assign(el("span", "t"), { textContent: r.t }),
      badge,
      Object.assign(el("span", "sip-detail"), { textContent: r.detail, title: r.detail }),
      Object.assign(el("span", "sip-cid"), { textContent: r.callid ? r.callid.slice(0, 10) + "…" : "" }),
    );
    const caret = el("span", "sip-caret", "▶");
    line.appendChild(caret);
    const raw = renderRaw(r.raw);
    line.onclick = () => {
      if (sipOpen.has(r.id)) sipOpen.delete(r.id); else sipOpen.add(r.id);
      msg.classList.toggle("open");
    };
    msg.append(line, raw);
    box.appendChild(msg);
  }
  if (sipAutoscroll && atBottom) box.scrollTop = box.scrollHeight;
}

function renderRtp() {
  const box = $("#rtp-stats");
  box.innerHTML = "";
  const byCall = rtpByAbonado[detailAbonado] || {};
  const ids = Object.keys(byCall);
  if (!ids.length) { box.innerHTML = '<div class="empty">Sin flujos RTP activos</div>'; return; }
  for (const cid of ids) {
    const s = byCall[cid];
    const card = el("div", "rtp-call");
    card.appendChild(el("h4", null, "call " + cid.slice(0, 16) + "…  (" + s.duration_s + "s)"));
    const grid = el("div", "rtp-metrics");
    const metric = (v, l) => { const m = el("div", "metric"); m.append(el("div", "v", v), el("div", "l", l)); return m; };
    grid.append(
      metric(s.tx_pkt, "tx pkt"), metric(s.rx_pkt, "rx pkt"), metric(s.rx_loss, "rx loss"),
      metric(s.tx_bytes, "tx bytes"), metric(s.rx_bytes, "rx bytes"),
      metric((s.rx_jitter_us / 1000).toFixed(1) + "ms", "jitter"),
    );
    card.appendChild(grid);
    box.appendChild(card);
  }
}

// ---------------- WebSocket ----------------
let ws = null;
function connectWs() {
  if (!token) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => { $("#ws-dot").className = "dot on"; };
  ws.onclose = () => { $("#ws-dot").className = "dot off"; if (token) setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
}

function handleEvent(e) {
  switch (e.type) {
    case "status":
      $("#engine-status").textContent = e.available ? "motor SIP activo" : (e.reason || "motor SIP inactivo");
      if (e.mode) engineMode = e.mode;
      $("#mode-badge").textContent = "modo: " + (e.mode || "?");
      { const mb2 = $("#mode-badge2"); if (mb2) mb2.textContent = "modo: " + (e.mode || "?"); }
      $("#stat-engine").textContent = e.available ? "activo" : "inactivo";
      $("#stat-engine").classList.toggle("ok", !!e.available);
      if (e.net) { lastNet = e.net; if (currentView === "dashboard") renderNetPanel(); }
      if (e.registrations) {
        for (const [aid, st] of Object.entries(e.registrations)) regState[aid] = st;
        renderStatus(); renderCallSelectors();
      }
      if (e.calls) {
        for (const c of e.calls) {
          if (c.call_id) { sipByCallId[c.call_id] = c.abonado_id; calls[c.call_id] = c; }
        }
        renderCalls();
      }
      break;
    case "register":
      regState[e.abonado_id] = { active: e.active, code: e.code, reason: e.reason };
      renderStatus(); renderCallSelectors();
      break;
    case "sip": {
      pushSip(parseSip(e));
      renderSip();
      break;
    }
    case "call": {
      if (e.event === "state") {
        if (e.call_id) sipByCallId[e.call_id] = e.abonado_id;
        calls[e.call_id] = { line: e.line, remote: e.remote, state: e.state, last_code: e.last_code, abonado_id: e.abonado_id };
        if (e.state === "DISCONNECTED") {
          setTimeout(() => { delete calls[e.call_id]; renderCalls(); renderStatus(); }, 4000);
          if (rtpByAbonado[e.abonado_id]) delete rtpByAbonado[e.abonado_id][e.call_id];
        }
        renderCalls(); renderRtp(); renderStatus();
      }
      break;
    }
    case "rtp": {
      (rtpByAbonado[e.abonado_id] = rtpByAbonado[e.abonado_id] || {})[e.call_id] = e;
      if (String(e.abonado_id) === String(detailAbonado)) renderRtp();
      break;
    }
    case "call_record": {
      if (!e.replay) {
        callHistory.unshift(e);
        if (callHistory.length > 500) callHistory.pop();
        if (currentView === "llamadas") renderHistory();
        refreshCallStats();
      }
      break;
    }
    case "log":
      if (e.level === "warn") console.warn("[engine]", e.msg); else console.log("[engine]", e.msg);
      break;
  }
}

function pushSip(rec) {
  sipAll.push(rec);
  if (sipAll.length > MAX_SIP) { const old = sipAll.shift(); sipOpen.delete(old.id); }
}

// Un mensaje es relevante a un abonado si su línea aparece en los URIs
// (From/To/Contact) o si su Call-ID está mapeado a ese abonado (llamada activa).
function sipRelevant(r, abId) {
  const ab = abonados.find((a) => String(a.id) === String(abId));
  if (!ab) return false;
  if (r.callid && String(sipByCallId[r.callid]) === String(abId)) return true;
  const line = (ab.line_number || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (line && new RegExp("[:<]" + line + "@").test(r.raw)) return true;
  const user = (ab.auth_user || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (user && new RegExp("[:<\"]" + user + "@").test(r.raw)) return true;
  return false;
}

// ---------------- Modal ABONADO (solo identidad + perfil) ----------------
const USER_FIELDS = ["display_name", "line_number", "short_number", "auth_user", "auth_password", "enabled"];

function fillProfileSelect() {
  // El abonado DEBE asociarse a un perfil: toda la config vive en el perfil.
  const sel = $("#ab-profile"); sel.innerHTML = "";
  if (!profiles.length) {
    sel.appendChild(Object.assign(el("option", null, "— No hay perfiles: creá uno primero —"), { value: "" }));
    return;
  }
  for (const p of profiles) sel.appendChild(Object.assign(el("option", null, p.name), { value: String(p.id) }));
}
function openModal(a) {
  const f = $("#abonado-form");
  f.reset();
  f.elements.id.value = a ? a.id : "";   // clave: limpiar el id en alta (reset no lo limpia)
  for (const inp of f.querySelectorAll("input,select")) inp.disabled = false;
  fillProfileSelect();
  const noProfiles = !profiles.length;
  $("#ab-save").disabled = noProfiles;
  $("#ab-profile-note").innerHTML = noProfiles
    ? "No hay perfiles cargados. Creá un perfil en la vista <b>Perfiles</b> antes de dar de alta abonados."
    : "Toda la config de red, comportamiento, reg-event y mensajes SIP la aporta el perfil (se edita en la vista <b>Perfiles</b>). Acá solo se define la identidad del abonado y a qué perfil pertenece.";
  $("#modal-title").textContent = a ? "Editar abonado " + a.line_number : "Nuevo abonado";
  if (a) {
    f.elements.id.value = a.id;
    for (const k of USER_FIELDS) {
      const inp = f.elements[k]; if (!inp) continue;
      if (inp.type === "checkbox") inp.checked = !!a[k];
      else inp.value = a[k] == null ? "" : a[k];
    }
    $("#ab-profile").value = a.profile_id != null ? String(a.profile_id) : (profiles[0] ? String(profiles[0].id) : "");
  } else if (profiles.length) {
    $("#ab-profile").value = String(profiles[0].id);   // por defecto, primer perfil
  }
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

$("#abonado-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target; const data = {};
  for (const inp of f.elements) {
    if (!inp.name) continue;
    if (inp.type === "checkbox") data[inp.name] = inp.checked;
    else if (inp.type === "number") data[inp.name] = inp.value === "" ? null : Number(inp.value);
    else data[inp.name] = inp.value;
  }
  const id = data.id; delete data.id;
  data.profile_id = data.profile_id ? Number(data.profile_id) : null;
  if (data.profile_id == null) { toast("Elegí un perfil para el abonado.", "error"); return; }
  if (id) await api("PUT", "/api/abonados/" + id, data);
  else await api("POST", "/api/abonados", data);
  toast("Abonado " + (data.line_number || "") + " guardado", "ok");
  closeModal();
  await loadAll();
};

// ---------------- Editor de PERFIL (vista dedicada) ----------------
// Campos simples del perfil (todo salvo los hdr_* de mensajes SIP).
const PROFILE_SIMPLE = ["name","domain","pcscf_addr","pcscf_port","transport","auth_realm","registrar_uri","codec_pref","alerting_delay_s","echo_enabled","reg_expires","reg_event_enabled","reg_event_expires"];
const PROC_LABELS = { hdr_register: "REGISTER", hdr_invite: "INVITE", hdr_subscribe: "SUBSCRIBE" };
const OP_LABELS = { set: "reemplaza", add: "agrega", del: "quita" };
let peRules = { hdr_register: [], hdr_invite: [], hdr_subscribe: [] };
let peActiveProc = "hdr_register";

// Mini-DSL <-> filas: espeja app/sip_headers.py (parse_rules / serialize).
function parseDsl(text) {
  const rows = [];
  for (const raw of (text || "").replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("-")) { const name = line.slice(1).split(":")[0].trim(); if (name) rows.push({ op: "del", name, value: "" }); continue; }
    let op = "set", body = line;
    if (line.startsWith("+")) { op = "add"; body = line.slice(1).trim(); }
    const i = body.indexOf(":");
    if (i < 0) continue;
    const name = body.slice(0, i).trim(), value = body.slice(i + 1).trim();
    if (name) rows.push({ op, name, value });
  }
  return rows;
}
function serializeDsl(rows) {
  return rows.filter((r) => r.name && r.name.trim()).map((r) => {
    if (r.op === "del") return "-" + r.name.trim();
    if (r.op === "add") return "+" + r.name.trim() + ": " + (r.value || "");
    return r.name.trim() + ": " + (r.value || "");
  }).join("\n");
}
// Aplica filas sobre pares base (espeja apply_to_pairs) para el preview.
function applyRulesPreview(base, rows) {
  const out = base.map((p) => [p[0], p[1]]);
  const idxOf = (name) => out.findIndex(([n]) => n.toLowerCase() === name.toLowerCase());
  for (const r of rows) {
    if (!r.name || !r.name.trim()) continue;
    const name = r.name.trim(), low = name.toLowerCase();
    if (r.op === "del") { for (let i = out.length - 1; i >= 0; i--) if (out[i][0].toLowerCase() === low) out.splice(i, 1); }
    else if (r.op === "add") out.push([name, r.value || ""]);
    else { const i = idxOf(name); if (i >= 0) { out[i] = [name, r.value || ""]; for (let j = out.length - 1; j > i; j--) if (out[j][0].toLowerCase() === low) out.splice(j, 1); } else out.push([name, r.value || ""]); }
  }
  return out;
}
// Headers por defecto (aproximados) para el preview, según el backend.
function previewBase(proc, f) {
  const dom = (f.elements.domain.value || "dominio").trim();
  const pcscf = (f.elements.pcscf_addr.value || "pcscf").trim();
  const port = f.elements.pcscf_port.value || "5060";
  const tr = (f.elements.transport.value || "udp").toUpperCase();
  const exp = f.elements.reg_event_expires.value || "600";
  if (proc === "hdr_register") return { line: `REGISTER sip:${dom} SIP/2.0`, core: ["Via","Max-Forwards","From","To","Call-ID","CSeq","Contact","Expires","Authorization"], base: [["Supported","100rel,replaces,timer,privacy,in-dialog"],["Accept","application/sdp,application/simservs+xml"]] };
  if (proc === "hdr_invite") { const rl = engineMode === "local" ? `INVITE sip:<destino>@${dom} SIP/2.0` : `INVITE tel:<destino> SIP/2.0`; return { line: rl, core: ["Via","Max-Forwards","From","To","Call-ID","CSeq","Contact","Content-Type","Content-Length"], base: [] }; }
  return { line: `SUBSCRIBE sip:<usuario>@${dom} SIP/2.0`, core: [], base: [["Via",`SIP/2.0/${tr} <ip>:5060;rport;branch=...`],["Max-Forwards","70"],["Route",`<sip:${pcscf}:${port};lr>`],["From","<sip:<usuario>@"+dom+">;tag=..."],["To","<sip:<usuario>@"+dom+">"],["Call-ID","...@vobb-reg"],["CSeq","1 SUBSCRIBE"],["Contact","<sip:<usuario>@<ip>:5060;transport="+(tr.toLowerCase())+">"],["Event","reg"],["Accept","application/reginfo+xml"],["Expires",exp],["User-Agent","VoBB-UE-Simulator"]] };
}
function renderPreview() {
  const f = $("#profile-edit-form");
  const { line, core, base } = previewBase(peActiveProc, f);
  const rows = peRules[peActiveProc];
  const applied = applyRulesPreview(base, rows);
  const parts = [line];
  for (const name of core) parts.push(name + ": …  ⟨pjsip⟩");
  for (const [n, v] of applied) parts.push(n + ": " + v);
  $("#pe-preview-body").textContent = parts.join("\n");
}
function renderRules() {
  const box = $("#pe-rules"); box.innerHTML = "";
  const rows = peRules[peActiveProc];
  if (!rows.length) box.appendChild(el("div", "rules-empty", "Sin reglas: el mensaje sale con los headers por defecto."));
  rows.forEach((r, idx) => {
    const row = el("div", "rule-row");
    const opSel = el("select", "r-op");
    for (const op of ["set", "add", "del"]) opSel.appendChild(Object.assign(el("option", null, OP_LABELS[op]), { value: op, selected: r.op === op }));
    opSel.onchange = () => { r.op = opSel.value; renderRules(); };
    const name = Object.assign(el("input", "r-name"), { value: r.name, placeholder: "Header (ej. P-Access-Network-Info)" });
    name.oninput = () => { r.name = name.value; renderPreview(); };
    const val = Object.assign(el("input", "r-val"), { value: r.value, placeholder: r.op === "del" ? "(no aplica)" : "Valor" });
    val.disabled = r.op === "del";
    val.oninput = () => { r.value = val.value; renderPreview(); };
    const del = el("button", "small danger r-del", "✕"); del.type = "button";
    del.onclick = () => { rows.splice(idx, 1); renderRules(); };
    row.append(opSel, name, val, del);
    box.appendChild(row);
  });
  renderPreview();
}
function setProc(proc) {
  peActiveProc = proc;
  for (const b of $("#pe-proc-tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.proc === proc);
  $("#pe-proc-hint").innerHTML = proc === "hdr_subscribe"
    ? "SUBSCRIBE (builder propio): se puede reemplazar/agregar/quitar <b>cualquier</b> header, incluido Expires, Event o User-Agent."
    : PROC_LABELS[proc] + ": solo headers de <b>extensión</b> (los core Via/From/To/Call-ID/CSeq/Contact los genera pjsip).";
  renderRules();
}
function openProfileEditor(p) {
  const f = $("#profile-edit-form");
  f.reset();
  f.elements.id.value = p ? p.id : "";
  $("#pe-title").textContent = p ? "Perfil: " + p.name : "Nuevo perfil";
  if (p) for (const k of PROFILE_SIMPLE) {
    const inp = f.elements[k]; if (!inp) continue;
    if (inp.type === "checkbox") inp.checked = !!p[k]; else inp.value = p[k] == null ? "" : p[k];
  }
  peRules = {
    hdr_register: parseDsl(p ? p.hdr_register : ""),
    hdr_invite: parseDsl(p ? p.hdr_invite : ""),
    hdr_subscribe: parseDsl(p ? p.hdr_subscribe : ""),
  };
  // Mostrar la vista (overlay): ocultar las 4 vistas base y el editor visible.
  for (const v of VIEWS) $("#view-" + v).classList.add("hidden");
  $("#nav-perfiles").classList.add("active");
  $("#view-perfil-edit").classList.remove("hidden");
  $("#app").classList.remove("nav-open");
  setProc("hdr_register");
}
function closeProfileEditor() { showView("perfiles"); }

async function saveProfile() {
  const f = $("#profile-edit-form");
  if (!f.reportValidity()) return;
  const data = {};
  for (const k of PROFILE_SIMPLE) {
    const inp = f.elements[k]; if (!inp) continue;
    if (inp.type === "checkbox") data[k] = inp.checked;
    else if (inp.type === "number") data[k] = inp.value === "" ? null : Number(inp.value);
    else data[k] = inp.value;
  }
  data.hdr_register = serializeDsl(peRules.hdr_register);
  data.hdr_invite = serializeDsl(peRules.hdr_invite);
  data.hdr_subscribe = serializeDsl(peRules.hdr_subscribe);
  const id = f.elements.id.value;
  if (id) await api("PUT", "/api/profiles/" + id, data);
  else await api("POST", "/api/profiles", data);
  toast("Perfil guardado", "ok");
  closeProfileEditor();
  await loadAll();
}

$("#pe-proc-tabs").onclick = (e) => { const b = e.target.closest("button[data-proc]"); if (b) setProc(b.dataset.proc); };
$("#pe-add-rule").onclick = () => { peRules[peActiveProc].push({ op: "set", name: "", value: "" }); renderRules(); };
$("#pe-save").onclick = saveProfile;
$("#pe-back").onclick = closeProfileEditor;
$("#profile-edit-form").addEventListener("submit", (e) => { e.preventDefault(); saveProfile(); });
// Reflejar en el preview los cambios de los campos que participan (dominio/pcscf/expires).
$("#profile-edit-form").addEventListener("input", (e) => { if (["domain","pcscf_addr","pcscf_port","transport","reg_event_expires"].includes(e.target.name)) renderPreview(); });

// ---------------- Usuarios (admin) ----------------
const ALL_PERMS = { edit_abonados: "Abonados", control_calls: "Llamadas", manage_profiles: "Perfiles" };

function renderUsers() {
  const tb = $("#users-body");
  tb.innerHTML = "";
  if (!users.length) { tb.innerHTML = '<tr><td colspan="7" class="empty">Sin usuarios</td></tr>'; return; }
  for (const u of users) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", u.username));
    tr.appendChild(el("td", null, u.display_name || ""));
    const roleTd = el("td");
    roleTd.appendChild(el("span", "role-badge " + (u.is_admin ? "admin" : "user"), u.is_admin ? "admin" : "usuario"));
    tr.appendChild(roleTd);
    const permTd = el("td");
    if (u.is_admin) permTd.appendChild(el("span", "perm-tag", "todos"));
    else for (const p of (u.permissions || [])) permTd.appendChild(el("span", "perm-tag", ALL_PERMS[p] || p));
    tr.appendChild(permTd);
    tr.appendChild(el("td", "mono", (u.numbers || []).map((n) => n.end && n.end !== n.start ? `${n.start}-${n.end}` : n.start).join(", ") || "—"));
    tr.appendChild(el("td", null, u.enabled ? "activo" : "deshabilitado"));
    const act = el("td", "col-actions"); const grp = el("div", "row-actions");
    const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openUserModal(u);
    const bDel = el("button", "small danger", "Eliminar");
    bDel.onclick = () => { if (confirm(`¿Borrar usuario "${u.username}"?`)) api("DELETE", `/api/users/${u.id}`).then(loadAll); };
    grp.append(bEdit, bDel); act.appendChild(grp); tr.appendChild(act);
    tb.appendChild(tr);
  }
}

function openUserModal(u) {
  const f = $("#user-form");
  f.reset();
  $("#user-modal-title").textContent = u ? "Editar usuario: " + u.username : "Nuevo usuario";
  f.elements.id.value = u ? u.id : "";
  if (u) {
    f.elements.username.value = u.username;
    f.elements.display_name.value = u.display_name || "";
    f.elements.is_admin.checked = !!u.is_admin;
    f.elements.enabled.checked = !!u.enabled;
    $("#user-numbers").value = (u.numbers || []).map((n) => n.end && n.end !== n.start ? `${n.start}-${n.end}` : n.start).join("\n");
  }
  const perms = u ? (u.permissions || []) : [];
  for (const chk of f.querySelectorAll(".perm")) chk.checked = perms.includes(chk.value);
  $("#user-modal").classList.remove("hidden");
}
function closeUserModal() { $("#user-modal").classList.add("hidden"); }

function parseNumbers(text) {
  const out = [];
  for (const raw of text.split(/\n+/)) {
    const line = raw.trim();
    if (!line) continue;
    // Rango "a-b" respetando el "+" inicial (split por guion que no sea el signo).
    const m = line.match(/^(\+?[^-\s]+)\s*-\s*(\+?[^-\s]+)$/);
    if (m) out.push({ start: m[1], end: m[2] });
    else out.push({ start: line, end: "" });
  }
  return out;
}

$("#user-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const perms = [...f.querySelectorAll(".perm")].filter((c) => c.checked).map((c) => c.value);
  const data = {
    username: f.elements.username.value.trim(),
    display_name: f.elements.display_name.value,
    is_admin: f.elements.is_admin.checked,
    enabled: f.elements.enabled.checked,
    permissions: perms,
    numbers: parseNumbers($("#user-numbers").value),
  };
  const pwd = f.elements.password.value;
  if (pwd) data.password = pwd;
  const id = f.elements.id.value;
  if (id) await api("PUT", "/api/users/" + id, data);
  else {
    if (!pwd) { toast("La contraseña es obligatoria para un usuario nuevo", "error"); return; }
    await api("POST", "/api/users", data);
  }
  toast("Usuario guardado", "ok");
  closeUserModal();
  await loadAll();
};

// ---------------- Autenticación ----------------
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
}
function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
}
async function bootstrap() {
  // Deep-link opcional: #token=... (se guarda y se limpia del address bar).
  const m = location.hash.match(/token=([^&]+)/);
  if (m) { token = decodeURIComponent(m[1]); localStorage.setItem("vobb_token", token); history.replaceState(null, "", location.pathname); }
  if (!token) { showLogin(); return; }
  try {
    currentUser = await api("GET", "/api/auth/me");
  } catch (e) { showLogin(); return; }
  showApp();
  await loadAll();
  connectWs();
}
function logout() {
  token = null; localStorage.removeItem("vobb_token"); currentUser = null;
  if (ws) { try { ws.close(); } catch (e) {} }
  showLogin();
}

$("#login-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const errBox = $("#login-error"); errBox.classList.add("hidden");
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: f.username.value, password: f.password.value }),
    });
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || "Error de login"); }
    const d = await r.json();
    token = d.token; localStorage.setItem("vobb_token", token); currentUser = d.user;
    f.reset();
    showApp();
    await loadAll();
    connectWs();
  } catch (e) {
    errBox.textContent = e.message; errBox.classList.remove("hidden");
  }
};
$("#btn-logout").onclick = logout;

// ---------------- Eventos UI ----------------
$("#btn-new").onclick = () => openModal(null);
$("#btn-cancel").onclick = closeModal;
$("#btn-new-user").onclick = () => openUserModal(null);
$("#btn-cancel-user").onclick = closeUserModal;
$("#btn-new-profile").onclick = () => openProfileEditor(null);
$("#btn-call").onclick = async () => {
  const to = $("#call-to").value.trim();
  if (!to) { toast("Ingresá un destino (número, num@dominio o sip:/tel: URI)", "error"); return; }
  const from = $("#call-from");
  await api("POST", "/api/calls", { from_id: Number(from.value), to_number: to });
  const fromLine = (abonados.find((a) => String(a.id) === String(from.value)) || {}).line_number || "";
  toast(`Llamando ${fromLine} → ${to}`, "ok");
};
$("#btn-hangup-all").onclick = () => api("POST", "/api/calls/hangup_all").then(() => toast("Colgando todas las llamadas"));
$("#detail-select").onchange = (e) => { detailAbonado = e.target.value; renderDetail(); };

// --- Dashboard: control general ---
async function bulkReg(url, label) {
  const st = $("#ctrl-status"); if (st) st.textContent = label + "…";
  try {
    const r = await api("POST", url);
    if (st) st.textContent = `${label}: ${r.count} cuentas (escalonado)`;
    toast(`${label}: ${r.count} cuentas (escalonado)`, "ok");
  } catch (e) { if (st) st.textContent = ""; }
}
$("#btn-reg-all").onclick = () => bulkReg("/api/registrations/register_all", "Registrando todos");
$("#btn-unreg-all").onclick = () => bulkReg("/api/registrations/unregister_all", "Desregistrando todos");
$("#btn-hangup-all2").onclick = () => api("POST", "/api/calls/hangup_all").then(() => toast("Colgando todas las llamadas"));

// --- Búsquedas de las tablas ---
$("#status-search").oninput = (e) => { statusSearch = e.target.value.trim().toLowerCase(); renderStatus(); };
$("#prov-search").oninput = (e) => { provSearch = e.target.value.trim().toLowerCase(); renderProvisioning(); };

// --- Vista Llamadas: filtros e histórico ---
$("#btn-clear-history").onclick = async () => {
  if (!confirm("¿Borrar todo el histórico de llamadas?")) return;
  await api("DELETE", "/api/call-history");
  callHistory = []; renderHistory(); refreshCallStats();
  toast("Histórico borrado", "ok");
};
$("#hist-search").oninput = (e) => { histSearch = e.target.value.trim().toLowerCase(); renderHistory(); };
$("#hist-dir-filter").onclick = (e) => {
  const b = e.target.closest("button[data-dir]"); if (!b) return;
  histDir = b.dataset.dir;
  for (const btn of e.currentTarget.querySelectorAll("button")) btn.classList.toggle("active", btn === b);
  loadHistory();
};
$("#hist-res-filter").onclick = (e) => {
  const b = e.target.closest("button[data-res]"); if (!b) return;
  histRes = b.dataset.res;
  for (const btn of e.currentTarget.querySelectorAll("button")) btn.classList.toggle("active", btn === b);
  loadHistory();
};

// --- Toolbar de señalización ---
$("#sip-search").oninput = (e) => { sipFilterText = e.target.value.trim().toLowerCase(); renderSip(); };
$("#sip-dir-filter").onclick = (e) => {
  const b = e.target.closest("button[data-dir]"); if (!b) return;
  sipFilterDir = b.dataset.dir;
  for (const btn of e.currentTarget.querySelectorAll("button")) btn.classList.toggle("active", btn === b);
  renderSip();
};
$("#sip-autoscroll").onchange = (e) => { sipAutoscroll = e.target.checked; if (sipAutoscroll) { const box = $("#sip-console"); box.scrollTop = box.scrollHeight; } };
$("#sip-clear").onclick = () => { sipAll.length = 0; sipOpen.clear(); renderSip(); };

// --- Navegación (router de vistas) ---
$("#nav").addEventListener("click", (e) => {
  const item = e.target.closest(".nav-item[data-view]");
  if (!item) return;
  showView(item.dataset.view);
});
$("#btn-menu").onclick = () => $("#app").classList.toggle("nav-open");
$("#scrim").onclick = () => $("#app").classList.remove("nav-open");
window.addEventListener("hashchange", () => {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  if (name && VIEWS.includes(name) && name !== currentView) showView(name);
});

// ---------------- Init ----------------
// bootstrap() valida el token (o muestra login), y recién ahí carga datos y
// conecta el WS (para que el replay de señalización se atribuya correctamente).
(function initRoute() {
  if (location.hash.startsWith("#token")) return;   // deep-link token: lo maneja bootstrap
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  showView(name && VIEWS.includes(name) ? name : "dashboard");
})();
bootstrap();
setInterval(() => { if (currentUser) renderCalls(); }, 2000);
