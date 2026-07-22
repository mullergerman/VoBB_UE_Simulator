"""Configuración global de la plataforma, leída desde variables de entorno.

Todo tiene defaults sensatos para que la app arranque en el host (macOS) sin
Docker, o dentro de un contenedor, sin cambios de código.
"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Modo de operación: "local" (registrar Kamailio embebido) o "ims" (P-CSCF real).
MODE = os.environ.get("MODE", "local").lower()

# HTTP / Web
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = _int("HTTP_PORT", 8080)

# Base de datos (SQLite)
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "vobb.db"))

# Interfaz local de trabajo. En un host con varias interfaces, si se bindea a
# 0.0.0.0 PJSIP publica en Via/Contact/SDP la IP de la ruta por defecto, que
# puede no ser la interfaz por la que sale el SIP => SIP por una interfaz y RTP
# esperado por otra, y el P-CSCF descartando por anti-spoofing. Con esto TODO
# (SIP de pjsua, RTP, relay y reg-event) se bindea y se anuncia en una sola IP.
# Vacío => se autodetecta la IP de origen de la ruta hacia el P-CSCF.
BIND_ADDR = os.environ.get("BIND_ADDR", "").strip()      # IP local explícita
BIND_IFACE = os.environ.get("BIND_IFACE", "").strip()    # o nombre de interfaz

# SIP / PJSUA2
SIP_PORT = _int("SIP_PORT", 5060)            # puerto SIP local del endpoint
SIP_TRANSPORT = os.environ.get("SIP_TRANSPORT", "udp").lower()
RTP_PORT_START = _int("RTP_PORT_START", 4000)
# IP a anunciar en el c= del SDP si difiere de la de bind (NAT 1:1). Vacío =>
# se anuncia BIND_ADDR/la IP autodetectada.
MEDIA_PUBLIC_ADDR = os.environ.get("MEDIA_PUBLIC_ADDR", "").strip()
PJSUA_LOG_LEVEL = _int("PJSUA_LOG_LEVEL", 4)  # 4+ imprime mensajes SIP crudos
# Tope de cuentas/llamadas simultáneas. Debe ser <= los límites compilados en
# el Dockerfile (PJSUA_MAX_ACC/PJSUA_MAX_CALLS=512); dejamos margen.
MAX_ACCOUNTS = _int("MAX_ACCOUNTS", 500)
MAX_CALLS = _int("MAX_CALLS", 500)

# Suscripción al reg event package (RFC 3680 / TS 24.229). Un UE IMS real la
# envía tras el REGISTER. PJSUA2 no la implementa; la agregamos con un stack SIP
# propio (app/reg_subscribe.py). Por defecto activa solo en modo "ims" (en local
# el registrar Kamailio embebido no maneja reg-event). Puerto 0 = efímero.
REG_EVENT_SUBSCRIBE = _bool("REG_EVENT_SUBSCRIBE", MODE == "ims")
REG_EVENT_PORT = _int("REG_EVENT_PORT", 0)
REG_EVENT_EXPIRES = _int("REG_EVENT_EXPIRES", 600)

# Relay/ALG SIP: un forwarder propio se queda con el puerto que ve el P-CSCF
# (RELAY_PORT) y pjsua sale a través de él (proxy interno). Así TODO el tráfico
# del UE —REGISTER, INVITE y el SUBSCRIBE reg-event— comparte un único flow de
# origen, requisito del P-CSCF (ata el flow a la dupla IP:puerto del REGISTER).
# ACTIVO POR DEFECTO: sin él el reg-event nunca llega. SIP_RELAY=0 lo desactiva.
SIP_RELAY = _bool("SIP_RELAY", True)
RELAY_PORT = _int("RELAY_PORT", 5060)          # puerto externo que ve el P-CSCF
RELAY_INT_PORT = _int("RELAY_INT_PORT", 5062)  # puerto interno pjsua<->relay
RELAY_PJSUA_PORT = _int("RELAY_PJSUA_PORT", 5070)  # bind real de pjsua con relay
# Via/Contact que publica pjsua. Con relay debe ser el puerto EXTERNO (:5060),
# para que el P-CSCF vea una identidad coherente con el flow real y dirija ahí
# todo lo entrante (respuestas, INVITE terminante, NOTIFY). Vacío => se deriva
# solo como "<ip local>:RELAY_PORT"; "0"/"off" lo desactiva.
RELAY_PUBLIC_ADDR = os.environ.get("RELAY_PUBLIC_ADDR", "").strip()

# En modo local: dirección del registrar embebido (nombre de servicio compose).
# Se usa sólo para el seed inicial de abonados.
LOCAL_REGISTRAR = os.environ.get("LOCAL_REGISTRAR", "127.0.0.1")
LOCAL_DOMAIN = os.environ.get("LOCAL_DOMAIN", "vobb.test")

# Si es True, se desactiva el motor SIP (útil para probar sólo la web / CRUD
# en un host sin pjsua2 compilado). Se autodetecta si no se puede importar.
SIP_DISABLED = os.environ.get("SIP_DISABLED", "").lower() in ("1", "true", "yes")
