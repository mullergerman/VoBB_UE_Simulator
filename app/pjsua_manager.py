"""Gestor del motor SIP/media basado en PJSUA2.

Un único `Endpoint` aloja N `Account` (uno por abonado). Maneja:
  - registro Digest MD5 contra el P-CSCF/registrar,
  - llamada entrante: 180 Ringing -> espera alerting_delay_s -> 200 OK -> eco,
  - originar / colgar llamadas,
  - poll periódico de estadísticas RTP,
  - emisión de todos los eventos al EventBus (señalización SIP, estados, RTP).

Si pjsua2 no está disponible (host sin compilar), el manager entra en modo
"disabled" y la web sigue funcionando para el CRUD.
"""
import threading
import time
from typing import Dict, List, Optional

from . import config
from .events import bus
from .models import Abonado

try:
    import pjsua2 as pj
    from .sip_capture import SipLogWriter
    PJSUA_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    pj = None
    PJSUA_AVAILABLE = False
    _IMPORT_ERROR = str(_e)


# ----------------------------------------------------------------------------
# Objetos PJSUA2 (sólo se definen si la librería está disponible)
# ----------------------------------------------------------------------------
if PJSUA_AVAILABLE:

    class _Call(pj.Call):
        def __init__(self, account, manager, call_id=pj.PJSUA_INVALID_ID, incoming=False):
            super().__init__(account, call_id)
            self.manager = manager
            self.account = account
            self.incoming = incoming
            self._answer_timer: Optional[threading.Timer] = None
            self._answered = False

        # -- estado de la llamada --
        def onCallState(self, prm):
            if self.getId() < 0:
                return
            try:
                ci = self.getInfo()
            except Exception:
                return
            state = ci.stateText
            snap = {
                "abonado_id": self.account.abonado_id,
                "line": self.account.line_number,
                "call_id": ci.callIdString,
                "state": state,
                "state_code": ci.state,
                "remote": ci.remoteUri,
                "last_code": ci.lastStatusCode,
            }
            self.manager._update_call_snapshot(ci.callIdString, snap, ci.state)
            bus.emit("call", event="state", **snap)
            if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self.manager._register_active_call(self)
            if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                if self._answer_timer:
                    self._answer_timer.cancel()
                self.manager._remove_call(self)

        # -- media lista: montar el eco --
        def onCallMediaState(self, prm):
            if not self.account.abonado.echo_enabled or self.getId() < 0:
                return
            try:
                ci = self.getInfo()
            except Exception:
                return
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    try:
                        aud = self.getAudioMedia(i)
                        # Eco: transmitir el audio recibido de vuelta a sí mismo.
                        aud.startTransmit(aud)
                        bus.emit(
                            "call",
                            event="echo_on",
                            abonado_id=self.account.abonado_id,
                            line=self.account.line_number,
                            call_id=ci.callIdString,
                        )
                    except Exception as e:  # pragma: no cover
                        bus.emit("log", level="warn", msg=f"echo error: {e}")

        # -- contestación diferida (alerting) --
        def schedule_answer(self, delay_s: int):
            def _do_answer():
                try:
                    self.manager.ep.libRegisterThread("answer-timer")
                except Exception:
                    pass
                try:
                    op = pj.CallOpParam()
                    op.statusCode = 200
                    self.answer(op)
                    self._answered = True
                except Exception as e:  # pragma: no cover
                    bus.emit("log", level="warn", msg=f"answer error: {e}")

            self._answer_timer = threading.Timer(max(0, delay_s), _do_answer)
            self._answer_timer.daemon = True
            self._answer_timer.start()

    class _Account(pj.Account):
        def __init__(self, abonado: Abonado, manager):
            super().__init__()
            self.abonado = abonado
            self.abonado_id = abonado.id
            self.line_number = abonado.line_number
            self.manager = manager

        def onRegState(self, prm):
            try:
                ai = self.getInfo()
                st = {
                    "active": ai.regIsActive,
                    "code": prm.code,
                    "reason": prm.reason,
                    "line": self.line_number,
                }
                self.manager._reg_state[self.abonado_id] = st
                bus.emit("register", abonado_id=self.abonado_id, **st)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"regstate error: {e}")

        def onIncomingCall(self, prm):
            call = _Call(self, self.manager, call_id=prm.callId, incoming=True)
            self.manager._track_call(call)
            # 180 Ringing inmediato, luego 200 OK tras el delay de alerting.
            try:
                op = pj.CallOpParam()
                op.statusCode = 180
                call.answer(op)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"ringing error: {e}")
            call.schedule_answer(self.abonado.alerting_delay_s)


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------
class PjsuaManager:
    def __init__(self) -> None:
        self.available = PJSUA_AVAILABLE and not config.SIP_DISABLED
        self.ep = None
        self.accounts: Dict[int, "_Account"] = {}
        self.calls: List["_Call"] = []
        self._active_calls: Dict[str, "_Call"] = {}
        self._reg_state: Dict[int, dict] = {}   # snapshot para clientes nuevos
        self._call_state: Dict[str, dict] = {}  # snapshot de llamadas por call-id
        self._lock = threading.RLock()
        self._stats_thread: Optional[threading.Thread] = None
        self._running = False
        self._log_writer = None
        if not PJSUA_AVAILABLE:
            self._disabled_reason = f"pjsua2 no disponible: {_IMPORT_ERROR}"
        elif config.SIP_DISABLED:
            self._disabled_reason = "SIP deshabilitado por configuración (SIP_DISABLED)"
        else:
            self._disabled_reason = None

    # ---- ciclo de vida ----
    def start(self) -> None:
        if not self.available:
            bus.emit("log", level="warn", msg=self._disabled_reason)
            return
        self.ep = pj.Endpoint()
        self.ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = config.PJSUA_LOG_LEVEL
        ep_cfg.logConfig.consoleLevel = config.PJSUA_LOG_LEVEL
        ep_cfg.logConfig.msgLogging = True
        self._log_writer = SipLogWriter()
        ep_cfg.logConfig.writer = self._log_writer
        ep_cfg.uaConfig.maxCalls = 32
        self.ep.libInit(ep_cfg)

        # Transporte SIP.
        tcfg = pj.TransportConfig()
        tcfg.port = config.SIP_PORT
        ttype = pj.PJSIP_TRANSPORT_TCP if config.SIP_TRANSPORT == "tcp" else pj.PJSIP_TRANSPORT_UDP
        self.ep.transportCreate(ttype, tcfg)

        self.ep.libStart()

        # Sin hardware de audio (contenedor headless): usar el null audio device
        # para que el conference bridge funcione. El eco es media->media, no pasa
        # por ningún dispositivo físico.
        try:
            self.ep.audDevManager().setNullDev()
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"setNullDev error: {e}")

        # Prioridad de codecs: sólo G.711.
        self._configure_codecs("PCMU,PCMA")

        self._running = True
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        self._stats_thread.start()
        bus.emit("log", level="info", msg=f"PJSUA2 iniciado en puerto {config.SIP_PORT}/{config.SIP_TRANSPORT}")

    def stop(self) -> None:
        self._running = False
        if not self.available or self.ep is None:
            return
        try:
            self.ep.libDestroy()
        except Exception:
            pass
        self.ep = None

    def _ensure_thread(self) -> None:
        """Registra el thread actual en PJSUA2 (obligatorio para threads ajenos,
        p.ej. el threadpool de FastAPI que atiende REST)."""
        if self.ep is None:
            return
        try:
            if not self.ep.libIsThreadRegistered():
                self.ep.libRegisterThread(f"ext-{threading.get_ident()}")
        except Exception:
            pass

    def _configure_codecs(self, pref: str) -> None:
        wanted = [c.strip().upper() for c in pref.split(",") if c.strip()]
        try:
            for codec in self.ep.codecEnum2():
                cid = codec.codecId  # p.ej. "PCMU/8000/1"
                base = cid.split("/")[0].upper()
                prio = 255 - (wanted.index(base) if base in wanted else 200)
                self.ep.codecSetPriority(cid, prio if base in wanted else 0)
        except Exception as e:  # pragma: no cover
            bus.emit("log", level="warn", msg=f"codec cfg error: {e}")

    # ---- gestión de cuentas ----
    def add_account(self, abonado: Abonado) -> None:
        if not self.available or not abonado.enabled:
            return
        self._ensure_thread()
        with self._lock:
            if abonado.id in self.accounts:
                self.remove_account(abonado.id)
            acc = _Account(abonado, self)
            try:
                acc.create(self._account_config(abonado))
            except Exception as e:
                # Una config inválida (URI/proxy/realm) NO debe tumbar la
                # plataforma: se reporta el error y se sigue con el resto.
                reason = getattr(e, "reason", None) or getattr(e, "title", None) or str(e)
                msg = f"abonado {abonado.line_number}: config inválida ({reason})"
                print(f"[add_account] {msg}", flush=True)
                self._reg_state[abonado.id] = {
                    "active": False, "code": 400, "reason": reason,
                    "line": abonado.line_number,
                }
                bus.emit("register", abonado_id=abonado.id, active=False,
                         code=400, reason=reason, line=abonado.line_number)
                return
            self.accounts[abonado.id] = acc

    def update_account(self, abonado: Abonado) -> None:
        if not self.available:
            return
        self._ensure_thread()
        with self._lock:
            if not abonado.enabled:
                self.remove_account(abonado.id)
                return
            acc = self.accounts.get(abonado.id)
            if acc is None:
                self.add_account(abonado)
                return
            acc.abonado = abonado
            acc.line_number = abonado.line_number
            acc.modify(self._account_config(abonado))

    def remove_account(self, abonado_id: int) -> None:
        self._ensure_thread()
        with self._lock:
            acc = self.accounts.pop(abonado_id, None)
            self._reg_state.pop(abonado_id, None)
        if acc is not None:
            try:
                acc.shutdown()
            except Exception:
                pass
            bus.emit("register", abonado_id=abonado_id, active=False, code=0, reason="removed")

    def _account_config(self, ab: Abonado):
        cfg = pj.AccountConfig()
        # El realm de la credencial DEBE coincidir con el del challenge (WWW-
        # Authenticate) o PJSIP no responde el 401. En IMS el realm del HSS
        # suele diferir del dominio, así que por defecto usamos el comodín "*"
        # (responde a cualquier realm). Si el abonado fija un realm explícito,
        # se respeta.
        realm = ab.auth_realm.strip() if ab.auth_realm and ab.auth_realm.strip() else "*"
        cfg.idUri = f"sip:{ab.line_number}@{ab.domain}"
        # R-URI del REGISTER = home domain; el ruteo al P-CSCF va por el proxy.
        cfg.regConfig.registrarUri = f"sip:{ab.domain}"
        cfg.regConfig.timeoutSec = ab.reg_expires
        # P-CSCF como outbound proxy: el REGISTER/INVITE sale hacia él.
        cfg.sipConfig.proxies.append(
            f"sip:{ab.pcscf_addr}:{ab.pcscf_port};transport={ab.transport};lr"
        )
        cred = pj.AuthCredInfo("digest", realm, ab.auth_user or ab.line_number, 0, ab.auth_password)
        cfg.sipConfig.authCreds.append(cred)
        return cfg

    def register(self, abonado_id: int, renew: bool = True) -> None:
        self._ensure_thread()
        with self._lock:
            acc = self.accounts.get(abonado_id)
        if acc is not None:
            try:
                acc.setRegistration(renew)
            except Exception as e:
                reason = getattr(e, "reason", None) or str(e)
                print(f"[register] abonado {abonado_id}: {reason}", flush=True)
                bus.emit("log", level="warn", msg=f"register {abonado_id}: {reason}")

    # ---- llamadas ----
    def originate(self, from_id: int, to_number: str) -> Optional[str]:
        if not self.available:
            return None
        self._ensure_thread()
        with self._lock:
            acc = self.accounts.get(from_id)
        if acc is None:
            raise ValueError("Abonado origen no registrado en el motor SIP")
        ab = acc.abonado
        dest = f"sip:{to_number}@{ab.domain}"
        call = _Call(acc, self, incoming=False)
        prm = pj.CallOpParam(True)
        try:
            call.makeCall(dest, prm)
        except Exception as e:
            reason = getattr(e, "reason", None) or getattr(e, "title", None) or str(e)
            if getattr(e, "status", None):
                reason = f"{reason} (status={e.status})"
            print(f"[originate] makeCall a {dest} FALLÓ: {reason}", flush=True)
            bus.emit("log", level="warn", msg=f"makeCall a {dest} falló: {reason}")
            raise ValueError(f"No se pudo originar la llamada: {reason}")
        # Trackear sólo tras un makeCall exitoso (id válido).
        self._track_call(call)
        try:
            if call.getId() >= 0:
                return call.getInfo().callIdString
        except Exception:
            pass
        return None

    def hangup(self, call_id: str) -> None:
        if not self.available:
            return
        self._ensure_thread()
        with self._lock:
            call = self._active_calls.get(call_id)
            if call is None:
                for c in self.calls:
                    try:
                        if c.getId() >= 0 and c.getInfo().callIdString == call_id:
                            call = c
                            break
                    except Exception:
                        continue
        if call is not None:
            try:
                call.hangup(pj.CallOpParam(True))
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"hangup error: {e}")

    def hangup_all(self) -> None:
        if not self.available or self.ep is None:
            return
        self._ensure_thread()
        try:
            self.ep.hangupAllCalls()
        except Exception:
            pass

    # ---- snapshot de llamadas (para clientes que conectan tarde) ----
    def _update_call_snapshot(self, call_id: str, snap: dict, state_code) -> None:
        if not call_id:
            return
        with self._lock:
            if state_code == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._call_state.pop(call_id, None)
            else:
                self._call_state[call_id] = snap

    # ---- tracking interno de llamadas ----
    def _track_call(self, call) -> None:
        with self._lock:
            self.calls.append(call)

    def _register_active_call(self, call) -> None:
        if call.getId() < 0:
            return
        try:
            cid = call.getInfo().callIdString
        except Exception:
            return
        with self._lock:
            self._active_calls[cid] = call

    def _remove_call(self, call) -> None:
        with self._lock:
            if call in self.calls:
                self.calls.remove(call)
            for k, v in list(self._active_calls.items()):
                if v is call:
                    self._active_calls.pop(k, None)

    # ---- estadísticas RTP ----
    def _stats_loop(self) -> None:
        try:
            self.ep.libRegisterThread("stats")
        except Exception:
            pass
        while self._running:
            time.sleep(1.0)
            with self._lock:
                calls = list(self.calls)
            for call in calls:
                self._emit_stats(call)

    def _emit_stats(self, call) -> None:
        try:
            if call.getId() < 0:
                return
            ci = call.getInfo()
            if ci.state != pj.PJSIP_INV_STATE_CONFIRMED:
                return
            ss = call.getStreamStat(0)
            rtcp = ss.rtcp
            bus.emit(
                "rtp",
                abonado_id=call.account.abonado_id,
                line=call.account.line_number,
                call_id=ci.callIdString,
                tx_pkt=rtcp.txStat.pkt,
                rx_pkt=rtcp.rxStat.pkt,
                tx_bytes=rtcp.txStat.bytes,
                rx_bytes=rtcp.rxStat.bytes,
                rx_loss=rtcp.rxStat.loss,
                tx_loss=rtcp.txStat.loss,
                rx_jitter_us=int(getattr(rtcp.rxStat.jitterUsec, "mean", 0)),
                duration_s=ci.connectDuration.sec,
            )
        except Exception:
            return

    # ---- estado para la API ----
    def status(self) -> dict:
        return {
            "available": self.available,
            "reason": self._disabled_reason,
            "mode": config.MODE,
            "accounts": list(self.accounts.keys()),
            "registrations": {str(k): v for k, v in self._reg_state.items()},
            "calls": list(self._call_state.values()),
        }


# Instancia global.
manager = PjsuaManager()
