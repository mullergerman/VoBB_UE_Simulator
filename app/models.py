"""Modelo de datos de la plataforma.

`Abonado` es el registro persistido (config del UE simulado). Los campos de
runtime (estado de registro, llamadas) NO se persisten: viven en memoria dentro
del PjsuaManager y se exponen vía la API/WebSocket.

`Profile` agrupa los parámetros de red/comportamiento compartidos (dominio,
P-CSCF, realm, registrar-uri, codecs, alerting, eco, expires). Un abonado puede
referenciar un perfil (profile_id) y heredar esos campos; solo define sus datos
propios (línea, IMPI, password). Si no referencia perfil, usa sus campos propios.
"""
from typing import List, Optional

from sqlmodel import Field, SQLModel

# Campos compartidos (los aporta el perfil cuando el abonado lo referencia).
SHARED_FIELDS = [
    "domain", "pcscf_addr", "pcscf_port", "transport", "auth_realm",
    "registrar_uri", "codec_pref", "alerting_delay_s", "echo_enabled",
    "reg_expires", "reg_event_enabled", "reg_event_expires",
    "hdr_register", "hdr_invite", "hdr_subscribe",
]


class AbonadoBase(SQLModel):
    enabled: bool = True
    display_name: str = ""
    line_number: str = Field(index=True)          # user part del AOR, p.ej. "1001"
    short_number: str = ""                        # numeración corta a marcar (MT)
    domain: str = "vobb.test"                     # home domain / realm
    pcscf_addr: str = "127.0.0.1"                 # dirección del P-CSCF / registrar
    pcscf_port: int = 5060
    transport: str = "udp"                        # udp | tcp
    auth_user: str = ""                           # IMPI / username Digest
    auth_password: str = ""                       # password EN PLANO (Digest MD5)
    auth_realm: str = ""                          # vacío => se usa `domain`
    # Registrar/Request-URI del REGISTER (= digest-uri). Vacío => sip:<domain>
    # (estándar 3GPP). Para replicar equipos que usan la IP del P-CSCF como
    # Request-URI, fijar p.ej. "sip:100.103.6.12".
    registrar_uri: str = ""
    codec_pref: str = "PCMU,PCMA"                 # prioridad de codecs (G.711)
    alerting_delay_s: int = 3                     # segundos de 180 antes de atender
    echo_enabled: bool = True                     # devolver audio en modo eco
    reg_expires: int = 600                        # expires del REGISTER
    # reg-event (RFC 3680): suscripción tras el REGISTER, por línea.
    reg_event_enabled: bool = True                # emitir SUBSCRIBE Event: reg
    reg_event_expires: int = 600                  # Expires del SUBSCRIBE / base refresh
    # Customización de headers SIP por procedimiento (mini-DSL, ver
    # app/sip_headers.py). Vacío => headers por defecto (comportamiento actual).
    hdr_register: str = ""
    hdr_invite: str = ""
    hdr_subscribe: str = ""
    # Perfil del que hereda los campos compartidos (None => usa los propios).
    profile_id: Optional[int] = Field(default=None, index=True)


class Abonado(AbonadoBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class AbonadoCreate(AbonadoBase):
    pass


class AbonadoUpdate(SQLModel):
    enabled: Optional[bool] = None
    display_name: Optional[str] = None
    line_number: Optional[str] = None
    short_number: Optional[str] = None
    domain: Optional[str] = None
    pcscf_addr: Optional[str] = None
    pcscf_port: Optional[int] = None
    transport: Optional[str] = None
    auth_user: Optional[str] = None
    auth_password: Optional[str] = None
    auth_realm: Optional[str] = None
    registrar_uri: Optional[str] = None
    codec_pref: Optional[str] = None
    alerting_delay_s: Optional[int] = None
    echo_enabled: Optional[bool] = None
    reg_expires: Optional[int] = None
    reg_event_enabled: Optional[bool] = None
    reg_event_expires: Optional[int] = None
    hdr_register: Optional[str] = None
    hdr_invite: Optional[str] = None
    hdr_subscribe: Optional[str] = None
    profile_id: Optional[int] = None


# --------------------------------------------------------------------------
# Perfiles: parámetros de red/comportamiento compartidos entre abonados.
# --------------------------------------------------------------------------
class ProfileBase(SQLModel):
    name: str = Field(index=True)
    domain: str = "vobb.test"
    pcscf_addr: str = "127.0.0.1"
    pcscf_port: int = 5060
    transport: str = "udp"
    auth_realm: str = ""
    registrar_uri: str = ""
    codec_pref: str = "PCMU,PCMA"
    alerting_delay_s: int = 3
    echo_enabled: bool = True
    reg_expires: int = 600
    reg_event_enabled: bool = True
    reg_event_expires: int = 600
    hdr_register: str = ""
    hdr_invite: str = ""
    hdr_subscribe: str = ""


class Profile(ProfileBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(SQLModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    pcscf_addr: Optional[str] = None
    pcscf_port: Optional[int] = None
    transport: Optional[str] = None
    auth_realm: Optional[str] = None
    registrar_uri: Optional[str] = None
    codec_pref: Optional[str] = None
    alerting_delay_s: Optional[int] = None
    echo_enabled: Optional[bool] = None
    reg_expires: Optional[int] = None
    reg_event_enabled: Optional[bool] = None
    reg_event_expires: Optional[int] = None
    hdr_register: Optional[str] = None
    hdr_invite: Optional[str] = None
    hdr_subscribe: Optional[str] = None


# --------------------------------------------------------------------------
# Histórico de llamadas (persistente). Se completa al finalizar cada llamada.
# --------------------------------------------------------------------------
class CallRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    direction: str = "MO"            # MO (originada) | MT (terminada/entrante)
    abonado_id: Optional[int] = Field(default=None, index=True)
    local_line: str = Field(default="", index=True)  # línea del UE simulado
    remote: str = ""                 # otra punta (destino marcado o remoteUri)
    start_ms: int = 0                # inicio (epoch ms)
    connect_ms: int = 0             # atendida (0 si nunca contestó)
    end_ms: int = 0                 # fin (epoch ms)
    duration_s: int = 0             # duración de la conversación (post-answer)
    answered: bool = False
    last_code: int = 0              # último status SIP (200/486/408/...)
    reason: str = ""
    tx_pkt: int = 0
    rx_pkt: int = 0
    rx_loss: int = 0
    rx_jitter_us: int = 0


# --------------------------------------------------------------------------
# Usuarios (login + permisos) y su asignación de números/rangos.
# --------------------------------------------------------------------------
# Permisos disponibles para usuarios NO admin (el admin tiene todo + gestión
# de usuarios). El acceso a un abonado se determina por los rangos del usuario.
PERMISSIONS = ["edit_abonados", "control_calls", "manage_profiles"]


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    display_name: str = ""
    password_salt: str = ""
    password_hash: str = ""
    is_admin: bool = False
    enabled: bool = True
    permissions: str = "[]"          # JSON list de permisos (para no-admin)


class UserNumber(SQLModel, table=True):
    """Número o rango de numeración asignado a un usuario. Un abonado cuyo
    line_number caiga en algún rango de un usuario es visible/gestionable por él;
    si cae en rangos de varios usuarios, queda compartido entre ellos."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    start: str = ""                  # inicio del rango (o número único)
    end: str = ""                    # fin del rango (vacío => = start)


# ---- DTOs de la API ----
class NumberRange(SQLModel):
    start: str
    end: str = ""


class LoginRequest(SQLModel):
    username: str
    password: str


class UserCreate(SQLModel):
    username: str
    password: str
    display_name: str = ""
    is_admin: bool = False
    enabled: bool = True
    permissions: List[str] = []
    numbers: List[NumberRange] = []


class UserUpdate(SQLModel):
    username: Optional[str] = None
    password: Optional[str] = None       # vacío/ausente => no cambia
    display_name: Optional[str] = None
    is_admin: Optional[bool] = None
    enabled: Optional[bool] = None
    permissions: Optional[List[str]] = None
    numbers: Optional[List[NumberRange]] = None
