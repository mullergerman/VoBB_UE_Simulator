"use strict";

// ---------------- Estado en memoria ----------------
let abonados = [];
const regState = {};          // abonado_id -> {active, code, reason}
const calls = {};             // call_id -> {line, remote, state, last_code, abonado_id}
const sipLog = {};            // abonado_id -> [ {dir, summary, raw, t} ]  (por call attribution best-effort)
const sipByCallId = {};       // call_id -> abonado_id (para atribuir SIP)
const rtpByAbonado = {};      // abonado_id -> { call_id -> stats }
let detailAbonado = null;
const MAX_SIP = 200;

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
const now = () => new Date().toLocaleTimeString("es-AR", { hour12: false }) + "." + String(Date.now() % 1000).padStart(3, "0");

// ---------------- Carga inicial ----------------
async function loadAbonados() {
  abonados = await api("GET", "/api/abonados");
  renderAbonados();
  renderCallSelectors();
  renderDetailSelector();
}

function renderAbonados() {
  const tb = $("#abonados-body");
  tb.innerHTML = "";
  if (!abonados.length) { tb.innerHTML = '<tr><td colspan="9" class="empty">Sin abonados</td></tr>'; return; }
  for (const a of abonados) {
    const tr = el("tr");
    const rs = regState[a.id] || {};
    const dotCls = rs.active ? "ok" : (rs.code >= 400 ? "fail" : "pending");
    const dotTitle = rs.active ? "Registrado" : (rs.reason ? `${rs.code} ${rs.reason}` : "No registrado");

    const dotTd = el("td"); const dot = el("span", "reg-dot " + dotCls); dot.title = dotTitle; dotTd.appendChild(dot);
    tr.appendChild(dotTd);
    tr.appendChild(el("td", "mono", a.line_number));
    tr.appendChild(el("td", "mono", a.domain));
    tr.appendChild(el("td", "mono", `${a.pcscf_addr}:${a.pcscf_port}/${a.transport}`));
    tr.appendChild(el("td", "mono", a.auth_user || a.line_number));
    tr.appendChild(el("td", "mono", a.auth_password));
    tr.appendChild(el("td", null, a.alerting_delay_s + "s"));
    tr.appendChild(el("td", null, a.echo_enabled ? "sí" : "no"));

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

function renderSip() {
  const box = $("#sip-console");
  box.innerHTML = "";
  const log = sipLog[detailAbonado] || [];
  for (const e of log) {
    const line = el("div", "sipline " + e.dir);
    line.append(
      Object.assign(el("span", "t", e.t), {}),
      Object.assign(el("span", "d", (e.dir === "tx" ? "→ " : "← ") + e.summary), {}),
    );
    const raw = el("div", "sipraw", e.raw);
    line.onclick = () => line.classList.toggle("open");
    box.append(line, raw);
  }
  box.scrollTop = box.scrollHeight;
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
      const aid = e.call_id ? sipByCallId[e.call_id] : null;
      const rec = { dir: e.direction, summary: e.summary, raw: e.raw, t: now() };
      // Atribuir a un abonado si conocemos el call_id; si no, a todos (best-effort).
      if (aid != null) pushSip(aid, rec);
      else for (const a of abonados) pushSip(a.id, rec);
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

function pushSip(aid, rec) {
  const arr = (sipLog[aid] = sipLog[aid] || []);
  arr.push(rec);
  if (arr.length > MAX_SIP) arr.shift();
}

// ---------------- Modal alta/edición ----------------
function openModal(a) {
  const f = $("#abonado-form");
  f.reset();
  $("#modal-title").textContent = a ? "Editar abonado " + a.line_number : "Nuevo abonado";
  if (a) {
    for (const k of Object.keys(a)) {
      const inp = f.elements[k];
      if (!inp) continue;
      if (inp.type === "checkbox") inp.checked = !!a[k]; else inp.value = a[k];
    }
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
  if (id) await api("PUT", "/api/abonados/" + id, data);
  else await api("POST", "/api/abonados", data);
  closeModal();
  await loadAbonados();
};

// ---------------- Eventos UI ----------------
$("#btn-new").onclick = () => openModal(null);
$("#btn-cancel").onclick = closeModal;
$("#btn-call").onclick = () => api("POST", "/api/calls", { from_id: Number($("#call-from").value), to_number: $("#call-to").value });
$("#btn-hangup-all").onclick = () => api("POST", "/api/calls/hangup_all");
$("#detail-select").onchange = (e) => { detailAbonado = e.target.value; renderDetail(); };

// ---------------- Init ----------------
loadAbonados();
connectWs();
setInterval(() => { renderCalls(); }, 2000);
