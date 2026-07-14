"use strict";

// ---------------- Estado en memoria ----------------
let token = localStorage.getItem("vobb_token") || null;
let currentUser = null;       // { id, username, is_admin, permissions, ... }
let users = [];               // lista de usuarios (solo admin)
let abonados = [];
let profiles = [];            // perfiles (parámetros compartidos)
const SHARED_FIELDS = ["domain","pcscf_addr","pcscf_port","transport","auth_realm","registrar_uri","codec_pref","alerting_delay_s","echo_enabled","reg_expires"];
const regState = {};          // abonado_id -> {active, code, reason}
const calls = {};             // call_id -> {line, remote, state, last_code, abonado_id}
const sipAll = [];            // log global de mensajes SIP (se filtra por abonado al render)
const sipByCallId = {};       // call_id -> abonado_id (atribución por llamada activa)
const rtpByAbonado = {};      // abonado_id -> { call_id -> stats }
let detailAbonado = null;
const MAX_SIP = 300;
let sipFilterText = "";
let sipFilterDir = "all";
let sipAutoscroll = true;
const sipOpen = new Set();     // ids de mensajes SIP expandidos (persisten al re-render)
let sipSeq = 0;

// ---------------- Helpers ----------------
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
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
    alert("Error: " + msg); throw new Error(msg);
  }
  return r.status === 204 ? null : r.json().catch(() => null);
}
const can = (perm) => currentUser && (currentUser.is_admin || (currentUser.permissions || []).includes(perm));
const fmtTime = (ms) => new Date(ms).toLocaleTimeString("es-AR", { hour12: false }) + "." + String(ms % 1000).padStart(3, "0");
const now = () => fmtTime(Date.now());

// ---------------- Carga inicial ----------------
async function loadAll() {
  profiles = await api("GET", "/api/profiles");
  abonados = await api("GET", "/api/abonados");
  if (currentUser && currentUser.is_admin) users = await api("GET", "/api/users");
  applyGating();
  renderProfiles();
  renderAbonados();
  if (currentUser && currentUser.is_admin) renderUsers();
  renderCallSelectors();
  renderDetailSelector();
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
  for (const id of ["#btn-call", "#btn-hangup-all"]) $(id).style.display = callable ? "" : "none";
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
const VIEWS = ["abonados", "monitor", "perfiles", "usuarios"];
let currentView = "abonados";
function showView(name) {
  if (!VIEWS.includes(name)) name = "abonados";
  currentView = name;
  for (const v of VIEWS) {
    $("#view-" + v).classList.toggle("hidden", v !== name);
    $("#nav-" + v).classList.toggle("active", v === name);
  }
  if (location.hash !== "#/" + name) history.replaceState(null, "", "#/" + name);
  $("#app").classList.remove("nav-open");   // cerrar sidebar en móvil
}

// ---------------- Stat tiles ----------------
function updateStats() {
  const reg = abonados.filter((a) => (regState[a.id] || {}).active).length;
  const active = Object.values(calls).filter((c) => c.state && c.state !== "DISCONNECTED").length;
  $("#stat-total").textContent = abonados.length;
  $("#stat-reg").textContent = reg;
  $("#stat-calls").textContent = active;
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

function renderAbonados() {
  const tb = $("#abonados-body");
  tb.innerHTML = "";
  if (!abonados.length) { tb.innerHTML = '<tr><td colspan="10" class="empty">Sin abonados</td></tr>'; return; }
  for (const a of abonados) {
    const e = effective(a);
    const tr = el("tr");
    const rs = regState[a.id] || {};
    const dotCls = rs.active ? "ok" : (rs.code >= 400 ? "fail" : "pending");
    const dotTitle = rs.active ? "Registrado" : (rs.reason ? `${rs.code} ${rs.reason}` : "No registrado");

    const dotTd = el("td"); const dot = el("span", "reg-dot " + dotCls); dot.title = dotTitle; dotTd.appendChild(dot);
    tr.appendChild(dotTd);
    tr.appendChild(el("td", "mono", a.line_number));
    const pf = a.profile_id != null ? profileById(a.profile_id) : null;
    const pfTd = el("td");
    pfTd.appendChild(el("span", "profile-pill" + (pf ? "" : " none"), pf ? pf.name : "personalizado"));
    tr.appendChild(pfTd);
    tr.appendChild(el("td", "mono", e.domain));
    tr.appendChild(el("td", "mono", `${e.pcscf_addr}:${e.pcscf_port}/${e.transport}`));
    tr.appendChild(el("td", "mono", a.auth_user || a.line_number));
    tr.appendChild(el("td", "mono", a.auth_password));
    tr.appendChild(el("td", null, e.alerting_delay_s + "s"));
    tr.appendChild(el("td", null, e.echo_enabled ? "sí" : "no"));

    const act = el("td", "col-actions");
    const grp = el("div", "row-actions");
    if (can("edit_abonados") || can("control_calls")) {
      const bReg = el("button", "small" + (rs.active ? " danger" : ""), rs.active ? "Unreg" : "Reg");
      bReg.onclick = () => api("POST", `/api/abonados/${a.id}/${rs.active ? "unregister" : "register"}`);
      grp.appendChild(bReg);
    }
    if (can("edit_abonados")) {
      const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openModal(a);
      const bDel = el("button", "small danger", "Eliminar"); bDel.onclick = () => { if (confirm("¿Borrar abonado " + a.line_number + "?")) api("DELETE", `/api/abonados/${a.id}`).then(loadAll); };
      grp.append(bEdit, bDel);
    }
    if (!grp.children.length) grp.appendChild(el("span", "muted", "—"));
    act.appendChild(grp);
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  updateStats();
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
      const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openProfileModal(p);
      const bDel = el("button", "small danger", "Eliminar");
      bDel.onclick = () => {
        const msg = used ? `El perfil "${p.name}" lo usan ${used} abonado(s). Se desvincularán (conservando esta config). ¿Continuar?` : `¿Borrar perfil "${p.name}"?`;
        if (confirm(msg)) api("DELETE", `/api/profiles/${p.id}`).then(loadAll);
      };
      grp.append(bEdit, bDel);
    } else grp.appendChild(el("span", "muted", "—"));
    act.appendChild(grp); tr.appendChild(act);
    tb.appendChild(tr);
  }
}

function renderCallSelectors() {
  for (const id of ["#call-from", "#call-to"]) {
    const sel = $(id); const prev = sel.value; sel.innerHTML = "";
    for (const a of abonados) {
      const o = el("option", null, `${a.line_number} — ${a.display_name || a.domain}`);
      o.value = id === "#call-from" ? a.id : a.line_number;
      sel.appendChild(o);
    }
    if (prev) sel.value = prev;
  }
}

function renderDetailSelector() {
  const sel = $("#detail-select"); const prev = sel.value; sel.innerHTML = "";
  for (const a of abonados) {
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
      $("#mode-badge").textContent = "modo: " + (e.mode || "?");
      $("#stat-engine").textContent = e.available ? "activo" : "inactivo";
      $("#stat-engine").classList.toggle("ok", !!e.available);
      if (e.registrations) {
        for (const [aid, st] of Object.entries(e.registrations)) regState[aid] = st;
        renderAbonados();
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
      renderAbonados();
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
          setTimeout(() => { delete calls[e.call_id]; renderCalls(); }, 4000);
          if (rtpByAbonado[e.abonado_id]) delete rtpByAbonado[e.abonado_id][e.call_id];
        }
        renderCalls(); renderRtp();
      }
      break;
    }
    case "rtp": {
      (rtpByAbonado[e.abonado_id] = rtpByAbonado[e.abonado_id] || {})[e.call_id] = e;
      if (String(e.abonado_id) === String(detailAbonado)) renderRtp();
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

// ---------------- Modal ABONADO ----------------
const USER_FIELDS = ["display_name", "line_number", "auth_user", "auth_password", "enabled"];

function fillProfileSelect() {
  const sel = $("#ab-profile"); sel.innerHTML = "";
  sel.appendChild(Object.assign(el("option", null, "— Personalizado (sin perfil) —"), { value: "" }));
  for (const p of profiles) sel.appendChild(Object.assign(el("option", null, p.name), { value: String(p.id) }));
}
function fillShared(src, f) {
  for (const fld of SHARED_FIELDS) {
    const inp = f.elements[fld];
    if (!inp) continue;
    if (inp.type === "checkbox") inp.checked = !!src[fld];
    else inp.value = src[fld] == null ? "" : src[fld];
  }
}
// Con perfil: los campos compartidos muestran los valores heredados (editables).
// Si el usuario edita cualquiera, el abonado pasa a «Personalizado» automáticamente.
function onProfileChange() {
  const f = $("#abonado-form");
  const pid = $("#ab-profile").value;
  const p = pid ? profileById(pid) : null;
  if (p) fillShared(p, f);                 // previsualizar los valores heredados
  $("#ab-shared").classList.toggle("inherited", !!pid);
  $("#ab-shared-note").textContent = pid
    ? "Se heredan del perfil. Si editás cualquiera, el abonado pasa a «Personalizado» automáticamente."
    : "Configuración propia de este abonado.";
}
// Editar un campo compartido con un perfil activo => desvincular (Personalizado),
// conservando el valor que se está escribiendo.
function onSharedEdit() {
  if ($("#ab-profile").value) { $("#ab-profile").value = ""; onProfileChange(); }
}
function openModal(a) {
  const f = $("#abonado-form");
  f.reset();
  f.elements.id.value = a ? a.id : "";   // clave: limpiar el id en alta (reset no lo limpia)
  for (const inp of f.querySelectorAll("input,select")) inp.disabled = false;
  fillProfileSelect();
  $("#modal-title").textContent = a ? "Editar abonado " + a.line_number : "Nuevo abonado";
  if (a) {
    f.elements.id.value = a.id;
    for (const k of USER_FIELDS) {
      const inp = f.elements[k]; if (!inp) continue;
      if (inp.type === "checkbox") inp.checked = !!a[k];
      else inp.value = a[k] == null ? "" : a[k];
    }
    // Campos de config: mostrar los valores EFECTIVOS (heredados del perfil o propios).
    fillShared(effective(a), f);
    $("#ab-profile").value = a.profile_id != null ? String(a.profile_id) : "";
  } else if (profiles.length) {
    $("#ab-profile").value = String(profiles[0].id);   // por defecto, primer perfil
  }
  onProfileChange();
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

$("#ab-profile").onchange = onProfileChange;
// Editar cualquier campo de config con un perfil activo => pasar a Personalizado.
$("#ab-shared").addEventListener("input", onSharedEdit);
$("#ab-shared").addEventListener("change", onSharedEdit);

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
  // profile_id: "" => sin perfil (null); si hay perfil, no mandamos los campos
  // compartidos (los aporta el perfil).
  data.profile_id = data.profile_id ? Number(data.profile_id) : null;
  if (data.profile_id != null) for (const fld of SHARED_FIELDS) delete data[fld];
  if (id) await api("PUT", "/api/abonados/" + id, data);
  else await api("POST", "/api/abonados", data);
  closeModal();
  await loadAll();
};

// ---------------- Modal PERFIL ----------------
function openProfileModal(p) {
  const f = $("#profile-form");
  f.reset();
  f.elements.id.value = p ? p.id : "";   // clave: limpiar el id en alta (reset no lo limpia)
  $("#profile-modal-title").textContent = p ? "Editar perfil: " + p.name : "Nuevo perfil";
  if (p) for (const k of Object.keys(p)) {
    const inp = f.elements[k];
    if (!inp) continue;
    if (inp.type === "checkbox") inp.checked = !!p[k]; else inp.value = p[k] == null ? "" : p[k];
  }
  $("#profile-modal").classList.remove("hidden");
}
function closeProfileModal() { $("#profile-modal").classList.add("hidden"); }

$("#profile-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target; const data = {};
  for (const inp of f.elements) {
    if (!inp.name) continue;
    if (inp.type === "checkbox") data[inp.name] = inp.checked;
    else if (inp.type === "number") data[inp.name] = inp.value === "" ? null : Number(inp.value);
    else data[inp.name] = inp.value;
  }
  const id = data.id; delete data.id;
  if (id) await api("PUT", "/api/profiles/" + id, data);
  else await api("POST", "/api/profiles", data);
  closeProfileModal();
  await loadAll();
};

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
    if (!pwd) { alert("La contraseña es obligatoria para un usuario nuevo"); return; }
    await api("POST", "/api/users", data);
  }
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
$("#btn-new-profile").onclick = () => openProfileModal(null);
$("#btn-cancel-profile").onclick = closeProfileModal;
$("#btn-call").onclick = () => api("POST", "/api/calls", { from_id: Number($("#call-from").value), to_number: $("#call-to").value });
$("#btn-hangup-all").onclick = () => api("POST", "/api/calls/hangup_all");
$("#detail-select").onchange = (e) => { detailAbonado = e.target.value; renderDetail(); };

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
  showView(name && VIEWS.includes(name) ? name : "abonados");
})();
bootstrap();
setInterval(() => { if (currentUser) renderCalls(); }, 2000);
