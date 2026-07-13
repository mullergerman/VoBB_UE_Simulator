"use strict";

// ---------------- Estado en memoria ----------------
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
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  if (!r.ok) { const t = await r.text(); alert("Error: " + t); throw new Error(t); }
  return r.status === 204 ? null : r.json().catch(() => null);
}
const fmtTime = (ms) => new Date(ms).toLocaleTimeString("es-AR", { hour12: false }) + "." + String(ms % 1000).padStart(3, "0");
const now = () => fmtTime(Date.now());

// ---------------- Carga inicial ----------------
async function loadAll() {
  profiles = await api("GET", "/api/profiles");
  abonados = await api("GET", "/api/abonados");
  renderProfiles();
  renderAbonados();
  renderCallSelectors();
  renderDetailSelector();
}
async function loadAbonados() { await loadAll(); }

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

    const act = el("td");
    const bReg = el("button", "small", rs.active ? "Unreg" : "Reg");
    bReg.onclick = () => api("POST", `/api/abonados/${a.id}/${rs.active ? "unregister" : "register"}`);
    const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openModal(a);
    const bDel = el("button", "small danger", "✕"); bDel.onclick = () => { if (confirm("¿Borrar abonado " + a.line_number + "?")) api("DELETE", `/api/abonados/${a.id}`).then(loadAbonados); };
    act.append(bReg, bEdit, bDel);
    tr.appendChild(act);
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
    const act = el("td");
    const bEdit = el("button", "small", "Editar"); bEdit.onclick = () => openProfileModal(p);
    const bDel = el("button", "small danger", "✕");
    bDel.onclick = () => {
      const msg = used ? `El perfil "${p.name}" lo usan ${used} abonado(s). Se desvincularán (conservando esta config). ¿Continuar?` : `¿Borrar perfil "${p.name}"?`;
      if (confirm(msg)) api("DELETE", `/api/profiles/${p.id}`).then(loadAll);
    };
    act.append(bEdit, bDel);
    tr.appendChild(act);
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
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { $("#ws-dot").className = "dot on"; };
  ws.onclose = () => { $("#ws-dot").className = "dot off"; setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
}

function handleEvent(e) {
  switch (e.type) {
    case "status":
      $("#engine-status").textContent = e.available ? "motor SIP activo" : (e.reason || "motor SIP inactivo");
      $("#mode-badge").textContent = "modo: " + (e.mode || "?");
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
function fillProfileSelect() {
  const sel = $("#ab-profile"); sel.innerHTML = "";
  sel.appendChild(Object.assign(el("option", null, "— Personalizado (sin perfil) —"), { value: "" }));
  for (const p of profiles) sel.appendChild(Object.assign(el("option", null, p.name), { value: String(p.id) }));
}
// Muestra u oculta los campos compartidos según haya perfil seleccionado.
function syncSharedVisibility() {
  const hasProfile = !!$("#ab-profile").value;
  $("#ab-shared").classList.toggle("hidden", hasProfile);
}
function openModal(a) {
  const f = $("#abonado-form");
  f.reset();
  fillProfileSelect();
  $("#modal-title").textContent = a ? "Editar abonado " + a.line_number : "Nuevo abonado";
  if (a) {
    for (const k of Object.keys(a)) {
      const inp = f.elements[k];
      if (!inp) continue;
      if (inp.type === "checkbox") inp.checked = !!a[k];
      else inp.value = a[k] == null ? "" : a[k];
    }
    $("#ab-profile").value = a.profile_id != null ? String(a.profile_id) : "";
  } else if (profiles.length) {
    $("#ab-profile").value = String(profiles[0].id);   // por defecto, primer perfil
  }
  syncSharedVisibility();
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

$("#ab-profile").onchange = syncSharedVisibility;

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

// ---------------- Eventos UI ----------------
$("#btn-new").onclick = () => openModal(null);
$("#btn-cancel").onclick = closeModal;
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

// ---------------- Init ----------------
// Cargar abonados ANTES de conectar el WS para que el replay de señalización
// (que llega apenas conecta) pueda atribuirse correctamente.
(async () => {
  await loadAll();
  connectWs();
})();
setInterval(() => { renderCalls(); }, 2000);
