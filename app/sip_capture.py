"""Captura de la señalización SIP cruda desde PJSUA2.

PJSUA2 escribe los mensajes SIP (TX/RX) en su log a nivel >= 4. Subclasamos
`LogWriter` para interceptar cada línea, detectar los bloques de mensaje SIP,
parsear método/estado + Call-ID + From/To, y publicarlos en el EventBus para que
la web los muestre en tiempo real y los atribuya a cada abonado.
"""
import re
import time

try:
    import pjsua2 as pj
    _LOGWRITER_BASE = pj.LogWriter
except Exception:  # pjsua2 no disponible (modo sólo-web)
    _LOGWRITER_BASE = object

from .events import bus

# Marcadores que PJSIP imprime alrededor de cada mensaje SIP.
_TX_RE = re.compile(r"\.?\s*TX \d+ bytes (Request|Response) msg .* to")
_RX_RE = re.compile(r"\.?\s*RX \d+ bytes (Request|Response) msg .* from")
_REQ_LINE = re.compile(r"^(INVITE|ACK|BYE|CANCEL|REGISTER|OPTIONS|PRACK|UPDATE|INFO|SUBSCRIBE|NOTIFY|MESSAGE)\s+\S+\s+SIP/2\.0")
_STATUS_LINE = re.compile(r"^SIP/2\.0\s+(\d{3})\s+(.*)")
_CALLID = re.compile(r"^Call-ID:\s*(.+)$", re.IGNORECASE)
_CSEQ = re.compile(r"^CSeq:\s*\d+\s+(\w+)", re.IGNORECASE)


class SipLogWriter(_LOGWRITER_BASE):
    """Reensambla y emite los mensajes SIP que PJSIP vuelca al log."""

    def __init__(self) -> None:
        super().__init__()
        self._buf = []
        self._dir = None  # "tx" | "rx"

    def write(self, entry):  # noqa: A003 - firma impuesta por pjsua2
        try:
            msg = entry.msg
        except Exception:
            return
        # Eco a stdout para conservar los logs de PJSIP en `docker logs`.
        try:
            print(msg, flush=True)
        except Exception:
            pass
        for line in msg.splitlines():
            self._feed(line)

    def _feed(self, line: str) -> None:
        if _TX_RE.search(line):
            self._flush()
            self._dir = "tx"
            self._buf = []
            return
        if _RX_RE.search(line):
            self._flush()
            self._dir = "rx"
            self._buf = []
            return
        if self._dir is not None:
            # Fin de bloque: línea de guiones que PJSIP usa como separador.
            if line.strip().startswith("--end msg--"):
                self._flush()
                return
            self._buf.append(line)

    def _flush(self) -> None:
        if self._dir is None or not self._buf:
            self._dir = None
            self._buf = []
            return
        raw = "\n".join(self._buf).strip()
        if raw:
            summary, call_id = _summarize(raw)
            bus.emit(
                "sip",
                direction=self._dir,
                summary=summary,
                call_id=call_id,
                raw=raw,
                ts_ms=int(time.time() * 1000),
            )
        self._dir = None
        self._buf = []


def _summarize(raw: str):
    method = None
    call_id = None
    for line in raw.splitlines():
        m = _STATUS_LINE.match(line)
        if m and method is None:
            method = f"{m.group(1)} {m.group(2)}".strip()
            continue
        m = _REQ_LINE.match(line)
        if m and method is None:
            method = m.group(1)
            continue
        m = _CALLID.match(line)
        if m:
            call_id = m.group(1).strip()
        if method is None:
            m = _CSEQ.match(line)
            if m:
                method = m.group(1)
    return (method or "SIP", call_id)
