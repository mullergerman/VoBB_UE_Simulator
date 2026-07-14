"""Suscripción al *reg event package* (RFC 3680 / 3GPP TS 24.229).

Un UE IMS real, tras el REGISTER, envía un `SUBSCRIBE Event: reg` para
enterarse de cambios en su estado de registro (de-registro iniciado por red,
re-sync, etc.). PJSUA2 (bindings Python) NO expone esta suscripción, así que la
implementamos con un stack SIP mínimo propio sobre un socket UDP dedicado:

  - reusa las credenciales Digest del abonado (maneja el 401/407 challenge),
  - rutea como el INVITE del motor: Request-URI = AOR, Route = P-CSCF (que en
    este despliegue basta para llegar al S-CSCF, igual que los INVITE de pjsua),
  - refresca la suscripción antes de expirar,
  - responde 200 OK a los NOTIFY entrantes,
  - publica cada mensaje (TX/RX) en el mismo EventBus, para que aparezca en el
    Monitor junto a la señalización de pjsua.

Corre en threads propios y NO toca ninguna API de pjsua2 (solo sockets + bus),
por lo que es seguro respecto del motor SIP.

Nota de ruteo: usamos Route = P-CSCF (sin Service-Route). En este despliegue los
INVITE de pjsua llegan al core ruteados solo por el P-CSCF, así que replicamos
ese camino. Si un core exigiera el Service-Route del S-CSCF, habría que
capturarlo del 200 OK del REGISTER y anteponerlo al Route.
"""
import hashlib
import re
import secrets
import socket
import threading
import time
from typing import Dict, List, Optional

from . import config
from .events import bus

_CALLID_RE = re.compile(r"^Call-ID:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_CSEQ_RE = re.compile(r"^CSeq:\s*(\d+)\s+(\w+)", re.IGNORECASE | re.MULTILINE)
_STATUS_RE = re.compile(r"^SIP/2\.0\s+(\d{3})\s+(.*)$")
_TO_TAG_RE = re.compile(r"^To:.*;tag=([^;\s>]+)", re.IGNORECASE | re.MULTILINE)
_EXPIRES_RE = re.compile(r"^Expires:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_MIN_EXPIRES_RE = re.compile(r"^Min-Expires:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_WWW_AUTH_RE = re.compile(r"^WWW-Authenticate:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_PROXY_AUTH_RE = re.compile(r"^Proxy-Authenticate:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_CHALLENGE_KV = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _parse_challenge(header: str) -> dict:
    h = header.strip()
    if h[:6].lower() == "digest":
        h = h[6:]
    out = {}
    for m in _CHALLENGE_KV.finditer(h):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _digest_authorization(challenge: dict, method: str, uri: str, user: str, pwd: str) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop")
    opaque = challenge.get("opaque")
    ha1 = _md5(f"{user}:{realm}:{pwd}")
    ha2 = _md5(f"{method}:{uri}")
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"']
    if qop and "auth" in qop.split(","):
        nc = "00000001"
        cnonce = secrets.token_hex(8)
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += [f'response="{resp}"', "algorithm=MD5", f'cnonce="{cnonce}"',
                  "qop=auth", f"nc={nc}"]
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
        parts += [f'response="{resp}"', "algorithm=MD5"]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def _summarize(raw: str):
    """Método/estado + Call-ID para el resumen del evento (igual criterio que
    sip_capture)."""
    method = None
    call_id = None
    for line in raw.splitlines():
        m = _STATUS_RE.match(line)
        if m and method is None:
            method = f"{m.group(1)} {m.group(2)}".strip()
        elif method is None and re.match(r"^[A-Z]+\s+\S+\s+SIP/2\.0", line):
            method = line.split(None, 1)[0]
        if line.lower().startswith("call-id:"):
            call_id = line.split(":", 1)[1].strip()
    return (method or "SIP", call_id)


class _Sub:
    """Estado de una suscripción reg-event para un abonado."""
    def __init__(self, abonado):
        self.abonado_id = abonado.id
        self.line = abonado.line_number
        self.domain = abonado.domain
        self.aor = f"sip:{abonado.line_number}@{abonado.domain}"
        self.user = abonado.auth_user or abonado.line_number
        self.password = abonado.auth_password
        self.pcscf_addr = abonado.pcscf_addr
        self.pcscf_port = int(abonado.pcscf_port)
        self.transport = (abonado.transport or "udp").lower()
        self.expires = int(config.REG_EVENT_EXPIRES or abonado.reg_expires or 600)
        self.call_id = secrets.token_hex(12) + "@vobb-reg"
        self.from_tag = secrets.token_hex(6)
        self.to_tag: Optional[str] = None
        self.cseq = 0
        self.local_ip = "0.0.0.0"
        self.local_port = 0
        self.auth_tries = 0
        self.active = False
        self.terminated = False
        self.timer: Optional[threading.Timer] = None


class RegEventSubscriber:
    """Gestor de suscripciones reg-event sobre un único socket UDP."""

    def __init__(self) -> None:
        self._subs: Dict[int, _Sub] = {}
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._port = 0

    # ---- ciclo de vida ----
    def start(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", int(config.REG_EVENT_PORT or 0)))
            s.settimeout(1.0)
            self._sock = s
            self._port = s.getsockname()[1]
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            bus.emit("log", level="info",
                     msg=f"reg-event subscriber en UDP :{self._port}")
        except Exception as e:  # pragma: no cover
            self._running = False
            bus.emit("log", level="warn", msg=f"reg-event subscriber no arrancó: {e}")

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for sub in self._subs.values():
                if sub.timer:
                    sub.timer.cancel()
            self._subs.clear()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ---- API pública (llamada desde el manager en onRegState) ----
    def ensure(self, abonado) -> None:
        """Arranca la suscripción del abonado si aún no está activa (idempotente)."""
        if not self._running or self._sock is None:
            return
        with self._lock:
            existing = self._subs.get(abonado.id)
            if existing and not existing.terminated:
                return
            sub = _Sub(abonado)
            sub.local_ip = self._local_ip_for(sub.pcscf_addr)
            sub.local_port = self._port
            self._subs[abonado.id] = sub
        self._send_subscribe(sub)

    def stop_for(self, abonado_id: int) -> None:
        with self._lock:
            sub = self._subs.pop(abonado_id, None)
        if sub is None:
            return
        if sub.timer:
            sub.timer.cancel()
        if sub.active:
            # SUBSCRIBE Expires: 0 para cerrar la suscripción con gracia.
            sub.expires = 0
            self._send_subscribe(sub)

    # ---- envío ----
    def _local_ip_for(self, dst_addr: str) -> str:
        """IP local que el SO usaría para alcanzar el P-CSCF (para Via/Contact)."""
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((dst_addr, 5060))
            ip = probe.getsockname()[0]
            probe.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _send_subscribe(self, sub: _Sub, auth: Optional[str] = None,
                        auth_header_name: str = "Authorization") -> None:
        sub.cseq += 1
        branch = "z9hG4bK" + secrets.token_hex(8)
        to = f"<{sub.aor}>" + (f";tag={sub.to_tag}" if sub.to_tag else "")
        headers = [
            f"SUBSCRIBE {sub.aor} SIP/2.0",
            f"Via: SIP/2.0/{sub.transport.upper()} {sub.local_ip}:{sub.local_port};rport;branch={branch}",
            "Max-Forwards: 70",
            f"Route: <sip:{sub.pcscf_addr}:{sub.pcscf_port};lr>",
            f"From: <{sub.aor}>;tag={sub.from_tag}",
            f"To: {to}",
            f"Call-ID: {sub.call_id}",
            f"CSeq: {sub.cseq} SUBSCRIBE",
            f"Contact: <sip:{sub.line}@{sub.local_ip}:{sub.local_port};transport={sub.transport}>",
            "Event: reg",
            "Accept: application/reginfo+xml",
            f"Expires: {sub.expires}",
            "User-Agent: VoBB-UE-Simulator",
        ]
        if auth:
            headers.append(f"{auth_header_name}: {auth}")
        headers.append("Content-Length: 0")
        raw = "\r\n".join(headers) + "\r\n\r\n"
        self._send(raw, (sub.pcscf_addr, sub.pcscf_port))

    def _send(self, raw: str, dst) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(raw.encode("utf-8"), dst)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"reg-event TX error: {e}")
            return
        self._emit(raw, "tx")

    def _emit(self, raw: str, direction: str) -> None:
        summary, call_id = _summarize(raw)
        bus.emit("sip", direction=direction, summary=summary, call_id=call_id,
                 raw=raw.replace("\r\n", "\n").strip(), ts_ms=int(time.time() * 1000))

    # ---- recepción ----
    def _rx_loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._dispatch(data.decode("utf-8", errors="replace"), addr)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"reg-event RX error: {e}")

    def _find_sub(self, call_id: Optional[str]) -> Optional[_Sub]:
        if not call_id:
            return None
        with self._lock:
            for sub in self._subs.values():
                if sub.call_id == call_id:
                    return sub
        return None

    def _dispatch(self, raw: str, addr) -> None:
        self._emit(raw, "rx")
        first = raw.split("\r\n", 1)[0].split("\n", 1)[0]
        cid_m = _CALLID_RE.search(raw)
        call_id = cid_m.group(1).strip() if cid_m else None

        m = _STATUS_RE.match(first)
        if m:
            self._handle_response(raw, int(m.group(1)), self._find_sub(call_id))
            return
        if first.upper().startswith("NOTIFY"):
            self._handle_notify(raw, addr, self._find_sub(call_id))

    def _handle_response(self, raw: str, code: int, sub: Optional[_Sub]) -> None:
        if sub is None:
            return
        cseq_m = _CSEQ_RE.search(raw)
        if not cseq_m or cseq_m.group(2).upper() != "SUBSCRIBE":
            return

        if code in (401, 407) and sub.auth_tries < 2:
            sub.auth_tries += 1
            if code == 401:
                ch = _WWW_AUTH_RE.search(raw)
                hdr_name = "Authorization"
            else:
                ch = _PROXY_AUTH_RE.search(raw)
                hdr_name = "Proxy-Authorization"
            if not ch:
                return
            auth = _digest_authorization(_parse_challenge(ch.group(1)),
                                         "SUBSCRIBE", sub.aor, sub.user, sub.password)
            self._send_subscribe(sub, auth=auth, auth_header_name=hdr_name)
            return

        if code == 423:  # Interval Too Brief
            mm = _MIN_EXPIRES_RE.search(raw)
            if mm:
                sub.expires = int(mm.group(1))
                sub.auth_tries = 0
                self._send_subscribe(sub)
            return

        if 200 <= code < 300:
            sub.auth_tries = 0
            sub.active = True
            tt = _TO_TAG_RE.search(raw)
            if tt:
                sub.to_tag = tt.group(1)
            em = _EXPIRES_RE.search(raw)
            if em:
                sub.expires = int(em.group(1))
            self._schedule_refresh(sub)
            return

        # Error definitivo (>=300): dejar registrado; se reintenta en el próximo
        # ciclo de registro (onRegState) vía ensure().
        bus.emit("log", level="warn",
                 msg=f"reg-event SUBSCRIBE {sub.line}: respuesta {code}")

    def _schedule_refresh(self, sub: _Sub) -> None:
        if sub.timer:
            sub.timer.cancel()
        if sub.expires <= 0:
            return
        delay = max(10, int(sub.expires * 0.9))

        def _refresh():
            with self._lock:
                if self._subs.get(sub.abonado_id) is not sub or sub.terminated:
                    return
            sub.auth_tries = 0
            self._send_subscribe(sub)

        sub.timer = threading.Timer(delay, _refresh)
        sub.timer.daemon = True
        sub.timer.start()

    def _handle_notify(self, raw: str, addr, sub: Optional[_Sub]) -> None:
        # 200 OK espejando Via/From/To/Call-ID/CSeq del NOTIFY (in-dialog).
        wanted = ("via:", "from:", "to:", "call-id:", "cseq:")
        echoed: List[str] = []
        for line in raw.replace("\r\n", "\n").split("\n"):
            if line == "":
                break
            if line.lower().startswith(wanted):
                echoed.append(line)
        resp = "\r\n".join(["SIP/2.0 200 OK", *echoed, "Content-Length: 0"]) + "\r\n\r\n"
        self._send(resp, addr)

        if sub is not None:
            # Si el NOTIFY marca la suscripción terminada, permitir re-suscribir
            # en el próximo onRegState.
            mst = re.search(r"^Subscription-State:\s*([^;\r\n]+)", raw,
                            re.IGNORECASE | re.MULTILINE)
            if mst and mst.group(1).strip().lower() == "terminated":
                sub.terminated = True
                if sub.timer:
                    sub.timer.cancel()
