"""Relay / ALG SIP para unificar el flow de origen del UE simulado.

Problema: un P-CSCF IMS ata el "flow" del abonado a la dupla (IP, puerto) de
origen que aprendió en el REGISTER (típ. :5060). Cualquier request originado
desde otro puerto (p.ej. el SUBSCRIBE reg-event, que PJSUA2 no puede emitir por
su propio transporte) lo descarta por anti-spoofing del GM.

Solución: este relay se queda con el puerto externo que ve el P-CSCF
(RELAY_PORT, :5060) y pjsua sale a través de él (pjsua bindea RELAY_PJSUA_PORT y
usa el relay como outbound proxy interno). Así REGISTER, INVITE inicial y el
SUBSCRIBE reg-event comparten un único flow de origen :5060, que el P-CSCF acepta.

Topología (host networking):

    pjsua (:5070) --UDP--> [INT 127.0.0.1:5062]  RELAY  [EXT 0.0.0.0:5060] --UDP--> P-CSCF
    reg-event subscriber ------------------------------> (envía por EXT)

Ruteo:
  - INT (viene de pjsua): se le quita el Route propio del relay y se reenvía al
    P-CSCF por EXT.
  - EXT (viene del P-CSCF): si el Call-ID pertenece al subscriber reg-event, se
    entrega al subscriber; si no, se reenvía a pjsua por INT.
  - El subscriber envía sus SUBSCRIBE/NOTIFY-200 por EXT (mismo flow :5060).

Es un forwarder mínimo (no agrega Via ni Record-Route): mantiene intacta la
cadena de Via de pjsua, así que pjsua casa las respuestas por branch. Solo hace
pop del Route que apunta a sí mismo. Opt-in por config.SIP_RELAY.
"""
import socket
import threading
from typing import Callable, Optional, Set, Tuple

from . import config
from .events import bus


def _call_id(raw: str) -> Optional[str]:
    for line in raw.split("\n"):
        s = line.strip()
        low = s.lower()
        if low.startswith("call-id:") or low.startswith("i:"):
            return s.split(":", 1)[1].strip()
        if s == "":
            break
    return None


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
        self._route_needle = f"127.0.0.1:{self._int_port}"

    # ---- ciclo de vida ----
    def start(self) -> None:
        ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ext.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ext.bind(("0.0.0.0", self._ext_port))
        ext.settimeout(1.0)
        inta = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        inta.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        inta.bind(("127.0.0.1", self._int_port))
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
                print(f"[relay] upstream P-CSCF = {self._upstream}", flush=True)

    def int_proxy_uri(self, transport: str = "udp") -> str:
        """URI del proxy interno que debe usar pjsua como outbound proxy."""
        return f"sip:127.0.0.1:{self._int_port};transport={transport};lr"

    def public_ip_for(self, dst_addr: str) -> str:
        if config.RELAY_PUBLIC_ADDR:
            return config.RELAY_PUBLIC_ADDR.split(":")[0]
        try:
            p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            p.connect((dst_addr, 5060))
            ip = p.getsockname()[0]
            p.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @property
    def public_port(self) -> int:
        return self._ext_port

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
        propio y reenviar por EXT."""
        with self._lock:
            self._pjsua_addr = addr
            up = self._upstream
            ext = self._ext
        if up is None or ext is None:
            return
        out = self._pop_self_route(raw)
        try:
            ext.sendto(out.encode("utf-8"), up)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"relay INT->EXT error: {e}")

    def _on_ext(self, raw: str, addr) -> None:
        """Del P-CSCF: reg-event al subscriber (por Call-ID); el resto a pjsua."""
        cid = _call_id(raw)
        with self._lock:
            is_reg = cid is not None and cid in self._reg_callids
            handler = self._reg_handler
            pj = self._pjsua_addr
            inta = self._int
        if is_reg and handler is not None:
            handler(raw, addr)
            return
        if pj is None or inta is None:
            return
        try:
            inta.sendto(raw.encode("utf-8"), pj)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"relay EXT->INT error: {e}")

    def _pop_self_route(self, raw: str) -> str:
        """Quita la primera línea Route que apunta al relay (proxy interno)."""
        crlf = "\r\n" if "\r\n" in raw else "\n"
        out = []
        popped = False
        for ln in raw.split(crlf):
            low = ln.lower()
            if not popped and (low.startswith("route:") or low.startswith("r:")) \
                    and self._route_needle in ln:
                popped = True
                continue
            out.append(ln)
        return crlf.join(out)
