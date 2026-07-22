"""Relay / ALG SIP para unificar el flow de origen del UE simulado.

Problema: un P-CSCF IMS ata el "flow" del abonado a la dupla (IP, puerto) de
origen que aprendió en el REGISTER (típ. :5060). Cualquier request originado
desde otro puerto (p.ej. el SUBSCRIBE reg-event, que PJSUA2 no puede emitir por
su propio transporte) lo descarta por anti-spoofing del GM.

Solución: este relay se queda con el puerto externo que ve el P-CSCF
(RELAY_PORT, :5060) y pjsua sale a través de él (pjsua bindea RELAY_PJSUA_PORT y
usa el relay como outbound proxy interno). Así REGISTER, INVITE inicial y el
SUBSCRIBE reg-event comparten un único flow de origen :5060, que el P-CSCF acepta.

Topología (todo sobre la misma IP local, ver app/netutil.py):

    pjsua (:5070) --UDP--> [INT ip:5062]  RELAY  [EXT ip:5060] --UDP--> P-CSCF
    reg-event subscriber -----------------------> (envía por EXT)

Ruteo:
  - INT (viene de pjsua): se le quita el Route propio, se agrega Record-Route
    propio si es un request que abre diálogo, y se reenvía al P-CSCF por EXT.
  - EXT viniendo del P-CSCF: si el Call-ID es del subscriber reg-event, se le
    entrega; si es un request se le agrega Via + Record-Route propios y se pasa
    a pjsua por INT; si es una respuesta se le quita el Via propio (si está) y
    se pasa a pjsua.
  - EXT viniendo de pjsua (respuestas a requests terminantes y requests
    in-dialog, que pjsua rutea al relay por el Via/Record-Route que insertamos):
    se quita el Via/Route propio y se reenvía al P-CSCF por EXT.
  - El subscriber envía sus SUBSCRIBE/NOTIFY-200 por EXT (mismo flow :5060).

El Via y el Record-Route propios son lo que mantiene al relay en el camino: sin
ellos pjsua respondería y mandaría el ACK/BYE directo al P-CSCF desde :5070, es
decir fuera del flow registrado, que es justo lo que el P-CSCF descarta.
"""
import socket
import threading
from typing import Callable, Optional, Set, Tuple

from . import config, netutil
from .events import bus


_BRANCH_PREFIX = "z9hG4bK-relay-"
# Métodos que abren diálogo: sólo en ellos tiene sentido el Record-Route.
_DIALOG_METHODS = ("INVITE", "SUBSCRIBE", "REFER")


def _call_id(raw: str) -> Optional[str]:
    for line in raw.split("\n"):
        s = line.strip()
        low = s.lower()
        if low.startswith("call-id:") or low.startswith("i:"):
            return s.split(":", 1)[1].strip()
        if s == "":
            break
    return None


def _is_response(raw: str) -> bool:
    return raw[:8].upper().startswith("SIP/2.0")


def _method(raw: str) -> str:
    """Método del request (vacío si es una respuesta)."""
    if _is_response(raw):
        return ""
    return raw.split("\n", 1)[0].strip().split(" ", 1)[0].upper()


def _has_to_tag(raw: str) -> bool:
    for line in raw.split("\n"):
        s = line.strip()
        if s == "":
            break
        low = s.lower()
        if low.startswith("to:") or low.startswith("t:"):
            return ";tag=" in low
    return False


def _top_via_branch(raw: str) -> str:
    for line in raw.split("\n"):
        s = line.strip()
        if s == "":
            break
        low = s.lower()
        if low.startswith("via:") or low.startswith("v:"):
            i = low.find("branch=")
            if i < 0:
                return ""
            return s[i + 7:].split(";")[0].split(",")[0].strip()
    return ""


class SipRelay:
    def __init__(self) -> None:
        self._ext: Optional[socket.socket] = None
        self._int: Optional[socket.socket] = None
        self._upstream: Optional[Tuple[str, int]] = None
        self._pjsua_addr: Optional[Tuple[str, int]] = None
        self._reg_callids: Set[str] = set()
        self._reg_handler: Optional[Callable[[str, tuple], None]] = None
        self._running = False
        self._lock = threading.Lock()
        self._int_port = int(config.RELAY_INT_PORT)
        self._ext_port = int(config.RELAY_PORT)
        self._local_ip = "127.0.0.1"   # IP ruteable hacia el upstream (para el proxy)

    # ---- ciclo de vida ----
    def start(self) -> None:
        # Ambos sockets en la IP local unificada (ver app/netutil.py): así el
        # tráfico hacia el P-CSCF sale siempre por la misma interfaz que el
        # resto (RTP incluido). Sin IP resuelta, 0.0.0.0 como antes.
        host = netutil.bind_host()
        if netutil.bind_ip():
            self._local_ip = netutil.bind_ip()
        ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ext.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ext.bind((host, self._ext_port))
        ext.settimeout(1.0)
        inta = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        inta.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # INT no en loopback: pjsua le habla a la IP ruteable del relay, así
        # calcula un Contact/Via ruteable (no 127.0.0.1).
        inta.bind((host, self._int_port))
        inta.settimeout(1.0)
        self._ext, self._int = ext, inta
        self._running = True
        threading.Thread(target=self._loop, args=(ext, self._on_ext), daemon=True).start()
        threading.Thread(target=self._loop, args=(inta, self._on_int), daemon=True).start()
        print(f"[relay] EXT {ext.getsockname()}  INT {inta.getsockname()}", flush=True)
        bus.emit("log", level="info",
                 msg=f"SIP relay activo: EXT :{self._ext_port} / INT :{self._int_port}")

    def stop(self) -> None:
        self._running = False
        for s in (self._ext, self._int):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._ext = self._int = None

    # ---- configuración desde el manager ----
    def set_upstream(self, addr: str, port: int) -> None:
        with self._lock:
            if self._upstream is None:
                self._upstream = (addr, int(port))
                if not netutil.bind_ip():
                    self._local_ip = self.public_ip_for(addr)
                print(f"[relay] upstream P-CSCF = {self._upstream} / local {self._local_ip}",
                      flush=True)

    def int_proxy_uri(self, transport: str = "udp") -> str:
        """URI del proxy interno que debe usar pjsua como outbound proxy. Se
        expone la IP ruteable del relay (no 127.0.0.1) para que pjsua anuncie un
        Contact/Via alcanzable por el registrar/P-CSCF."""
        return f"sip:{self._local_ip}:{self._int_port};transport={transport};lr"

    def public_ip_for(self, dst_addr: str) -> str:
        return netutil.local_ip_for(dst_addr)

    @property
    def public_port(self) -> int:
        return self._ext_port

    def status(self) -> dict:
        with self._lock:
            up = self._upstream
            pj = self._pjsua_addr
        return {
            "local_ip": self._local_ip,
            "ext": f"{self._ext_sock_name()}",
            "int": f"{self._int_sock_name()}",
            "upstream": f"{up[0]}:{up[1]}" if up else "",
            "pjsua": f"{pj[0]}:{pj[1]}" if pj else "",
            "reg_subs": len(self._reg_callids),
        }

    def _ext_sock_name(self) -> str:
        try:
            a = self._ext.getsockname()
            return f"{a[0]}:{a[1]}"
        except Exception:
            return ""

    def _int_sock_name(self) -> str:
        try:
            a = self._int.getsockname()
            return f"{a[0]}:{a[1]}"
        except Exception:
            return ""

    # ---- integración con el subscriber reg-event ----
    def set_reg_handler(self, fn: Callable[[str, tuple], None]) -> None:
        self._reg_handler = fn

    def register_callid(self, call_id: str) -> None:
        with self._lock:
            self._reg_callids.add(call_id)

    def unregister_callid(self, call_id: str) -> None:
        with self._lock:
            self._reg_callids.discard(call_id)

    def send_to_pcscf(self, raw: str) -> bool:
        """Enviar por EXT (flow :5060) hacia el P-CSCF. Usado por el subscriber."""
        with self._lock:
            up = self._upstream
            ext = self._ext
        if up is None or ext is None:
            return False
        try:
            ext.sendto(raw.encode("utf-8"), up)
            return True
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"relay EXT->P-CSCF error: {e}")
            return False

    # ---- loop de recepción ----
    def _loop(self, sock: socket.socket, handler) -> None:
        while self._running:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                handler(data.decode("utf-8", errors="replace"), addr)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"relay handler error: {e}")

    def _on_int(self, raw: str, addr) -> None:
        """De pjsua hacia el P-CSCF: aprender addr de pjsua, quitar el Route
        propio, dejar Record-Route propio y reenviar por EXT."""
        with self._lock:
            self._pjsua_addr = addr
            up = self._upstream
            ext = self._ext
        if up is None or ext is None:
            return
        out = self._add_record_route(self._pop_self_route(raw))
        try:
            ext.sendto(out.encode("utf-8"), up)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"relay INT->EXT error: {e}")

    def _on_ext(self, raw: str, addr) -> None:
        """Puerto externo (:5060). Tres orígenes posibles:
        - pjsua (respuestas a requests terminantes / requests in-dialog que
          ruteamos vía nuestro Via/Record-Route) -> sacar lo nuestro y subir;
        - P-CSCF con Call-ID del reg-event -> al subscriber;
        - P-CSCF, el resto -> a pjsua por INT."""
        with self._lock:
            pj = self._pjsua_addr
            up = self._upstream
            ext = self._ext
            inta = self._int
            handler = self._reg_handler
            reg_ids = self._reg_callids

        if pj is not None and addr == pj:
            if up is None or ext is None:
                return
            out = self._strip_self_via(raw) if _is_response(raw) \
                else self._pop_self_route(raw)
            try:
                ext.sendto(out.encode("utf-8"), up)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"relay pjsua->EXT error: {e}")
            return

        cid = _call_id(raw)
        if cid is not None and cid in reg_ids and handler is not None:
            handler(raw, addr)
            return

        if pj is None or inta is None:
            return
        if _is_response(raw):
            out = self._strip_self_via(raw)
        else:
            # Request terminante: nuestro Via hace que pjsua nos devuelva la
            # respuesta (y no la mande directo al P-CSCF desde :5070), y el
            # Record-Route nos deja en la ruta del ACK/BYE. El Route que apunta
            # a nosotros (fruto de ese Record-Route) se consume acá.
            out = self._add_self_via(
                self._add_record_route(self._pop_self_route(raw)))
        try:
            inta.sendto(out.encode("utf-8"), pj)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"relay EXT->INT error: {e}")

    # ---- reescritura de cabeceras ----
    def _self_uris(self) -> Tuple[str, str]:
        return (f"{self._local_ip}:{self._ext_port}",
                f"{self._local_ip}:{self._int_port}")

    @staticmethod
    def _crlf(raw: str) -> str:
        return "\r\n" if "\r\n" in raw else "\n"

    @staticmethod
    def _insert_header(raw: str, header: str) -> str:
        """Inserta una cabecera arriba de todo (justo después de la start line),
        que es donde deben ir el Via y el Record-Route propios."""
        crlf = SipRelay._crlf(raw)
        lines = raw.split(crlf)
        if not lines:
            return raw
        return crlf.join([lines[0], header] + lines[1:])

    def _add_self_via(self, raw: str) -> str:
        ext_uri, _ = self._self_uris()
        branch = _BRANCH_PREFIX + (_top_via_branch(raw) or "0")
        via = f"Via: SIP/2.0/UDP {ext_uri};branch={branch}"
        return self._insert_header(raw, via)

    def _strip_self_via(self, raw: str) -> str:
        """Quita el primer Via si es el nuestro (respuestas que vuelven).

        Se identifica SÓLO por el branch: pjsua publica su Via con el mismo
        host:puerto que el relay (publicAddress = ip:RELAY_PORT), así que mirar
        el sent-by se llevaría puesto el Via propio de pjsua."""
        crlf = self._crlf(raw)
        lines = raw.split(crlf)
        for i, ln in enumerate(lines):
            if ln.strip() == "":
                break
            low = ln.lower()
            if low.startswith("via:") or low.startswith("v:"):
                if _BRANCH_PREFIX in ln:
                    return crlf.join(lines[:i] + lines[i + 1:])
                return raw
        return raw

    def _add_record_route(self, raw: str) -> str:
        """Record-Route propio en requests que abren diálogo. Sin él, pjsua
        mandaría el ACK/BYE directo al P-CSCF (fuera del flow :5060)."""
        if _is_response(raw) or _method(raw) not in _DIALOG_METHODS:
            return raw
        if _has_to_tag(raw):          # in-dialog: la ruta ya está establecida
            return raw
        ext_uri, _ = self._self_uris()
        if f"<sip:{ext_uri};lr>" in raw:
            return raw
        return self._insert_header(raw, f"Record-Route: <sip:{ext_uri};lr>")

    def _pop_self_route(self, raw: str) -> str:
        """Quita el/los Route que apuntan al relay (el proxy interno :5062 y el
        que viene del Record-Route :5060), respetando líneas con varias URIs."""
        crlf = self._crlf(raw)
        needles = [n for n in self._self_uris()]
        out = []
        for ln in raw.split(crlf):
            low = ln.lower()
            if (low.startswith("route:") or low.startswith("r:")) \
                    and any(n in ln for n in needles):
                name, _, value = ln.partition(":")
                kept = [u.strip() for u in value.split(",")
                        if u.strip() and not any(n in u for n in needles)]
                if kept:
                    out.append(f"{name}: {', '.join(kept)}")
                continue
            out.append(ln)
        return crlf.join(out)
