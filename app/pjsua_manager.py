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

from . import config, netutil
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
            if self.getId() < 0:
                return
            try:
                ci = self.getInfo()
            except Exception:
                return

            # Diagnóstico: adónde va a mandar RTP pjsip y en qué estado quedó
            # cada media. Si acá no hay ningún audio ACTIVE, no hay RTP por más
            # que la señalización haya terminado bien.
            active = 0
            for i, mi in enumerate(ci.media):
                if mi.type != pj.PJMEDIA_TYPE_AUDIO:
                    continue
                remote, codec, sdir = "?", "?", "?"
                try:
                    si = self.getStreamInfo(i)
                    remote = si.remoteRtpAddress
                    codec = f"{si.codecName}/{si.codecClockRate}"
                    sdir = str(si.dir)
                except Exception:
                    pass
                if mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    active += 1
                print(f"[media] {self.account.line_number} call={ci.callIdString} "
                      f"m[{i}] status={mi.status} dir={sdir} codec={codec} "
                      f"remote_rtp={remote}", flush=True)
            if active == 0:
                bus.emit("log", level="warn",
                         msg=f"{self.account.line_number}: ninguna media de audio "
                             f"quedó activa (no va a haber RTP)")

            if not self.account.abonado.echo_enabled:
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
                # Al confirmarse el registro, disparar/renovar la suscripción
                # reg-event (UE IMS clásico). Al des-registrarse, cerrarla.
                rs = self.manager._reg_subscriber
                if rs is not None:
                    if ai.regIsActive:
                        rs.ensure(self.abonado)
                    else:
                        rs.stop_for(self.abonado_id)
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
        self._reg_subscriber = None   # RegEventSubscriber (solo en modo ims)
        self._relay = None            # SipRelay (SIP_RELAY)
        self._sip_public = ""         # Via/Contact efectivo del transporte
        self._stats_warned = False    # para no repetir el error de stats RTP
        if not PJSUA_AVAILABLE:
            self._disabled_reason = f"pjsua2 no disponible: {_IMPORT_ERROR}"
        elif config.SIP_DISABLED:
            self._disabled_reason = "SIP deshabilitado por configuración (SIP_DISABLED)"
        else:
            self._disabled_reason = None

    # ---- ciclo de vida ----
    def _pcscf_hint(self):
        """(addr, port) del P-CSCF del primer abonado habilitado, para autodetectar
        por qué interfaz sale el tráfico. Devuelve (None, 5060) si no hay datos."""
        try:
            from sqlmodel import select
            from .db import get_session, resolve_abonado
            with get_session() as s:
                for ab in s.exec(select(Abonado)).all():
                    if not ab.enabled:
                        continue
                    r = resolve_abonado(ab, s)
                    if r.pcscf_addr:
                        return r.pcscf_addr, int(r.pcscf_port or 5060)
        except Exception as e:  # pragma: no cover
            print(f"[net] no se pudo leer el P-CSCF de la base: {e}", flush=True)
        return None, 5060

    def _relay_public_addr(self, bind: str) -> str:
        """Via/Contact que publica pjsua. Con relay debe apuntar al puerto
        EXTERNO del relay (lo que el P-CSCF ve y donde el relay escucha)."""
        if self._relay is None:
            return ""
        cfgv = config.RELAY_PUBLIC_ADDR
        if cfgv.lower() in ("0", "off", "no", "false"):
            return ""
        if cfgv:
            return cfgv if ":" in cfgv else f"{cfgv}:{config.RELAY_PORT}"
        return f"{bind}:{config.RELAY_PORT}" if bind else ""

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
        ep_cfg.uaConfig.maxCalls = min(config.MAX_CALLS, 512)
        self.ep.libInit(ep_cfg)

        # IP local unificada (SIP + RTP + relay + reg-event) para hosts con
        # varias interfaces. Se resuelve antes de crear nada que bindee.
        netutil.resolve(*self._pcscf_hint())

        # Relay/ALG SIP: se queda con el puerto que ve el P-CSCF; pjsua
        # bindea otro puerto y sale a través del relay. Arranca ANTES del
        # transporte para tener el :5060 tomado por el relay.
        if config.SIP_RELAY:
            try:
                from .sip_relay import SipRelay
                self._relay = SipRelay()
                self._relay.start()
            except Exception as e:  # pragma: no cover
                self._relay = None
                # Sin relay no hay flow único :5060: hay que verlo en el log.
                print(f"[relay] ERROR: no arrancó ({e}). Sin relay el reg-event "
                      f"y el in-dialog salen fuera del flow del REGISTER.",
                      flush=True)
                bus.emit("log", level="warn", msg=f"SIP relay no arrancó: {e}")

        # Transporte SIP. Con relay, pjsua bindea RELAY_PJSUA_PORT (el relay usa
        # el puerto externo). Sin relay, usa SIP_PORT como siempre.
        #  - boundAddress: fija la interfaz. Es clave: con 0.0.0.0 PJSIP publica
        #    en Via/Contact la IP de pj_gethostip() (ruta por defecto), que en un
        #    host multi-interfaz puede no ser por la que sale el SIP.
        #  - publicAddress: con relay, lo que ve el P-CSCF es el puerto EXTERNO
        #    del relay, así que se anuncia "<ip>:RELAY_PORT" y todo lo entrante
        #    (respuestas, INVITE terminante, NOTIFY) cae en el relay.
        ttype = pj.PJSIP_TRANSPORT_TCP if config.SIP_TRANSPORT == "tcp" else pj.PJSIP_TRANSPORT_UDP
        port = config.RELAY_PJSUA_PORT if self._relay is not None else config.SIP_PORT
        bind = netutil.bind_ip() or ""
        pub = self._relay_public_addr(bind)

        def _mk_transport(public_addr, bound_addr):
            tcfg = pj.TransportConfig()
            tcfg.port = port
            if bound_addr:
                tcfg.boundAddress = bound_addr
            if public_addr:
                tcfg.publicAddress = public_addr
            self.ep.transportCreate(ttype, tcfg)

        # Degradación progresiva: si una combinación falla, se reintenta con
        # menos overrides antes de darse por vencido (nunca tumbar el arranque
        # por un publicAddress/boundAddress que pjsip rechace).
        attempts = []
        for combo in ((pub, bind), ("", bind), ("", "")):
            if combo not in attempts:
                attempts.append(combo)
        last_err = None
        for i, (p, b) in enumerate(attempts):
            try:
                _mk_transport(p, b)
                print(f"[sip] transporte :{port} bound={b or '0.0.0.0'} "
                      f"public={p or '-'}", flush=True)
                self._sip_public = p or f"{b or '0.0.0.0'}:{port}"
                last_err = None
                break
            except Exception as e:
                last_err = e
                if i + 1 < len(attempts):
                    bus.emit("log", level="warn",
                             msg=f"transportCreate(bound={b or '-'}, public={p or '-'}) "
                                 f"falló ({e}); reintentando")
                    print(f"[sip] transportCreate falló ({b or '-'}/{p or '-'}): {e}",
                          flush=True)
        if last_err is not None:
            raise last_err

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

        # Suscripción reg-event (UE IMS clásico): stack SIP propio, independiente
        # de pjsua. Se dispara al confirmarse cada registro (onRegState activo).
        if config.REG_EVENT_SUBSCRIBE:
            try:
                from .reg_subscribe import RegEventSubscriber
                self._reg_subscriber = RegEventSubscriber(relay=self._relay)
                self._reg_subscriber.start()
                # Con relay, el subscriber envía/recibe por el flow :5060 del
                # relay (demux por Call-ID), no por un socket propio.
                if self._relay is not None:
                    self._relay.set_reg_handler(self._reg_subscriber.on_relay_rx)
            except Exception as e:  # pragma: no cover
                bus.emit("log", level="warn", msg=f"reg-event init error: {e}")

    def stop(self) -> None:
        self._running = False
        if self._reg_subscriber is not None:
            try:
                self._reg_subscriber.stop()
            except Exception:
                pass
            self._reg_subscriber = None
        if self._relay is not None:
            try:
                self._relay.stop()
            except Exception:
                pass
            self._relay = None
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
            # Guard: superar PJSUA_MAX_ACC aborta el proceso (assertion de C, no
            # atrapable). Rechazamos con gracia al llegar al tope.
            if len(self.accounts) >= config.MAX_ACCOUNTS:
                msg = f"límite de cuentas SIP alcanzado ({config.MAX_ACCOUNTS})"
                print(f"[add_account] abonado {abonado.line_number}: {msg}", flush=True)
                self._reg_state[abonado.id] = {"active": False, "code": 503, "reason": msg, "line": abonado.line_number}
                bus.emit("register", abonado_id=abonado.id, active=False, code=503, reason=msg, line=abonado.line_number)
                return
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
        if not abonado.enabled:
            self.remove_account(abonado.id)
            return
        # Recrear la cuenta (remove + add) en lugar de acc.modify(): el modify()
        # dispara un re-registro que aborta el proceso con el assertion de pjsua
        # `update_regc_contact: contact_hdr != NULL`. El alta fresca (add_account,
        # que ya hace remove-if-exists + create) usa el path de registro estable.
        self.add_account(abonado)

    def remove_account(self, abonado_id: int) -> None:
        self._ensure_thread()
        with self._lock:
            acc = self.accounts.pop(abonado_id, None)
            self._reg_state.pop(abonado_id, None)
        if self._reg_subscriber is not None:
            try:
                self._reg_subscriber.stop_for(abonado_id)
            except Exception:
                pass
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
        # R-URI del REGISTER (= digest-uri). Default = home domain (estándar
        # 3GPP); si el abonado fija registrar_uri se usa tal cual (p.ej. la IP
        # del P-CSCF, para replicar equipos que la usan como Request-URI).
        reg_uri = (ab.registrar_uri or "").strip()
        if reg_uri and not reg_uri.lower().startswith("sip:"):
            reg_uri = "sip:" + reg_uri
        cfg.regConfig.registrarUri = reg_uri or f"sip:{ab.domain}"
        cfg.regConfig.timeoutSec = ab.reg_expires
        # Outbound proxy. Con relay, pjsua sale hacia el relay (interno) y el
        # relay reenvía al P-CSCF real desde el flow :5060 (le fijamos el
        # upstream). Sin relay, apunta directo al P-CSCF como siempre.
        if self._relay is not None:
            self._relay.set_upstream(ab.pcscf_addr, ab.pcscf_port)
            cfg.sipConfig.proxies.append(self._relay.int_proxy_uri(ab.transport))
        else:
            cfg.sipConfig.proxies.append(
                f"sip:{ab.pcscf_addr}:{ab.pcscf_port};transport={ab.transport};lr"
            )
        cred = pj.AuthCredInfo("digest", realm, ab.auth_user or ab.line_number, 0, ab.auth_password)
        cfg.sipConfig.authCreds.append(cred)

        # --- Media/RTP en la MISMA interfaz que el SIP ---
        # Sin boundAddress, el socket RTP queda en 0.0.0.0 y pjsua anuncia en el
        # c= del SDP la IP de pj_gethostip() (ruta por defecto) => el remoto
        # manda/espera RTP por otra interfaz distinta a la del SIP.
        try:
            rtp = cfg.mediaConfig.transportConfig
            rtp.port = config.RTP_PORT_START
            bind = netutil.bind_ip()
            if bind:
                rtp.boundAddress = bind
            pub_media = config.MEDIA_PUBLIC_ADDR or bind or ""
            if pub_media:
                rtp.publicAddress = pub_media
            print(f"[rtp] {ab.line_number}: bound={bind or '0.0.0.0'} "
                  f"public={pub_media or '-'} port={config.RTP_PORT_START}", flush=True)
        except Exception as e:  # pragma: no cover
            print(f"[rtp] ERROR: config RTP no aplicada ({e}); el SDP puede "
                  f"anunciar otra interfaz", flush=True)
            bus.emit("log", level="warn", msg=f"config RTP (bind/public) no aplicada: {e}")

        # --- Perfil "UA IMS clásico" ---
        # Desactivar RFC 5626 outbound: elimina el ";ob" del Contact y las
        # opciones outbound/path del Supported, que algunos P-CSCF rechazan.
        cfg.natConfig.sipOutboundUse = 0
        # Contact con transporte explícito, como los UE reales:
        #   <sip:user@ip:port;transport=udp>
        cfg.regConfig.contactUriParams = f";transport={ab.transport}"
        # Headers típicos de un UE IMS en el REGISTER (igualan al equipo real).
        for name, val in (
            ("Supported", "100rel,replaces,timer,privacy,in-dialog"),
            ("Accept", "application/sdp,application/simservs+xml"),
        ):
            h = pj.SipHeader()
            h.hName = name
            h.hValue = val
            cfg.regConfig.headers.append(h)
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
        # Destino flexible: número suelto => sip:<num>@<dominio-del-origen>;
        # "num@host" => se le antepone sip:; una URI sip:/sips: se usa tal cual.
        dest = (to_number or "").strip()
        low = dest.lower()
        if low.startswith("sip:") or low.startswith("sips:"):
            pass
        elif "@" in dest:
            dest = "sip:" + dest
        else:
            dest = f"sip:{dest}@{ab.domain}"
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
        except Exception as e:
            # No enmascarar el motivo: sin stats no se puede distinguir "no
            # mandamos" de "mandamos y no vuelve".
            if not self._stats_warned:
                self._stats_warned = True
                print(f"[rtp] sin estadísticas de stream: {e}", flush=True)
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
            "net": self.net_status(),
        }

    def net_status(self) -> dict:
        """Estado de red efectivo: qué IP quedó, de dónde salió, si el relay
        está vivo y con qué puertos. Es lo que hay que mirar cuando el tráfico
        sale por una interfaz que no es la esperada."""
        st = {
            "bind_addr_env": config.BIND_ADDR or "",
            "bind_iface_env": config.BIND_IFACE or "",
            "bind_ip": netutil.bind_ip() or "",
            "local_addrs": [{"iface": n, "ip": a} for n, a in netutil.local_addrs()],
            "sip_port": config.RELAY_PJSUA_PORT if self._relay is not None else config.SIP_PORT,
            "sip_public": self._sip_public or "",
            "rtp_port_start": config.RTP_PORT_START,
            "rtp_public": config.MEDIA_PUBLIC_ADDR or netutil.bind_ip() or "",
            "relay_enabled": bool(config.SIP_RELAY),
            "relay_running": self._relay is not None,
        }
        if self._relay is not None:
            st["relay"] = self._relay.status()
        return st


# Instancia global.
manager = PjsuaManager()
